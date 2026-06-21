"""Erlang-style supervisor specification for the executor.

Models OTP supervisor semantics on top of the manifest. References:

- https://erlang.org/documentation/doc-4.9.1/doc/design_principles/sup_princ.html
- https://www.erlang.org/docs/20/man/supervisor

The supervisor binary at ``supervisor/`` consumes the YAML emitted from
this dataclass tree and fork/exec's the child commands, honouring the
restart strategy and bounded-restart budgets.

The AUTOSAR :class:`Process` / Execution-Manifest world separately
describes *what* runs; this module describes *how supervision behaves*
when things crash. The two are intentionally orthogonal — different
deployments can pick different restart policies for the same Process
set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union

from artheia.manifest.algebra import Identifiable, identifiable_dataclass


# ---------------------------------------------------------------------------
# Strategy + restart types (Erlang OTP supervisor docs)
# ---------------------------------------------------------------------------


class RestartStrategy(str, Enum):
    """Supervisor restart strategy.

    - ``one_for_one`` — only the failed child is restarted.
    - ``one_for_all`` — all children are terminated and restarted when
      any single child terminates abnormally.
    - ``rest_for_one`` — the failed child and any child started *after*
      it in the spec are terminated and restarted; earlier children
      stay running.
    - ``simple_one_for_one`` — like ``one_for_one`` but children are
      dynamically added at runtime from a single child template; we
      keep the literal for completeness but don't yet exercise it.
    """

    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"
    SIMPLE_ONE_FOR_ONE = "simple_one_for_one"


class RestartType(str, Enum):
    """When a terminated child gets restarted.

    - ``permanent`` — always restart.
    - ``transient`` — restart only on *abnormal* exit (exit code != 0
      and != SIGTERM/SIGINT in response to graceful shutdown).
    - ``temporary`` — never restart.
    """

    PERMANENT = "permanent"
    TRANSIENT = "transient"
    TEMPORARY = "temporary"


class ChildType(str, Enum):
    """OTP child type: a worker or a nested supervisor."""

    WORKER = "worker"
    SUPERVISOR = "supervisor"


# Shutdown is either an integer milliseconds, ``"brutal_kill"``, or
# ``"infinity"``. We keep it as a free-form string|int in the dataclass
# and validate in the supervisor binary.
ShutdownSpec = Union[int, str]


# ---------------------------------------------------------------------------
# Child + supervisor specs
# ---------------------------------------------------------------------------


@dataclass
class Supervisor:
    """The machine's supervisor — the node that IMPLEMENTS the AUTOSAR Execution
    Management (ARA Executor) spec for this machine.

    Identity = its TIPC instance. Multiple machines on one TIPC namespace
    (network_mode: host) run the same supervisor binary, so each binds a distinct
    instance (central=0, compute=1) to avoid collision. This flows into the
    machine's execution.json as ``supervisor_instance``; the supervisor binary
    reads it (THEIA_SUPERVISOR_INSTANCE) and run-supervisor.sh sets it from the
    manifest — no hardcoded env. Models the .art ComputeSupervisor prototype
    (which declares the SupervisorCtl/Worker nodes at instance=1).
    """
    instance: int = 0


@dataclass
class NodeInfo:
    """One artheia node hosted inside a child process.

    A child process can host one OR more nodes (the latter via a
    composition). The supervisor needs per-node metadata to:

    - synthesise ``<child>.node_sup`` rows in TreeSnapshot (#364)
    - decide which nodes to watchdog (only reporting=true ones
      send HeartbeatReport)
    - locate each node's NodeTraceCtl TIPC server (#363) for the
      trace config push

    Fields:
      name           artheia node-type-name ("SmDaemon", "CounterNode")
      reporting      AUTOSAR Reporting/Non-Reporting (true = expects
                     heartbeat + can receive trace push)
      tipc_type      "0x...." hex string, copied from the NodeDecl
      tipc_instance  "0" / "1" / ..., copied from the NodeDecl
    """

    name: str
    reporting: bool = True
    tipc_type: str = ""
    tipc_instance: str = "0"

    # Per-node CPU affinity + scheduler (from the rig's NodeToCPUMapping). The
    # supervisor serializes these into THEIA_NODE_CFG; the hosting process's
    # main.cc applies them to the node thread (apply_node_affinity). Empty =
    # leave the node unpinned (inherits its process's affinity).
    cpus: list[int] = field(default_factory=list)
    sched: str = ""           # "fifo"|"rr"|"other"|"batch"|"idle"|"deadline"|""
    sched_prio: int = 0       # rtprio for fifo/rr


@identifiable_dataclass
class ChildSpec(Identifiable):
    """One supervised child.

    OTP child-spec fields:

    - :attr:`name` (``id`` in OTP) — unique within the parent supervisor.
    - :attr:`start_cmd` — the command line the supervisor exec's. We use
      a list-of-strings (argv) rather than OTP's ``{M, F, A}`` because
      our children are POSIX processes, not Erlang modules.
    - :attr:`restart` — :class:`RestartType`.
    - :attr:`shutdown` — milliseconds before SIGKILL, or ``"brutal_kill"``
      (immediate SIGKILL), or ``"infinity"`` (wait forever; appropriate
      for supervisors).
    - :attr:`type` — :class:`ChildType`.
    - :attr:`modules` — informational. OTP uses this for hot code
      upgrades; we surface it as a free-form list of source paths or
      package names for debuggability.
    - :attr:`nodes` — per-artheia-node metadata for the C++ supervisor's
      node_sup synthesis (#364) and trace push (#361). Populated by
      the FC's package.art. Empty for non-FC children (vendor apps with
      no .art declaration).
    """

    name: str
    start_cmd: list[str] = field(default_factory=list)
    restart: RestartType = RestartType.PERMANENT
    shutdown: ShutdownSpec = 5000  # ms
    type: ChildType = ChildType.WORKER
    modules: list[str] = field(default_factory=list)
    # Project extensions:
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str = ""
    # AUTOSAR ProcessToMachineMapping flavour (§9.4). Mutually exclusive.
    shall_run_on: list[int] = field(default_factory=list)
    shall_not_run_on: list[int] = field(default_factory=list)
    nodes: list[NodeInfo] = field(default_factory=list)
    # Per-process memory cap, BYTES (from Process.mem_limit). The C++ supervisor
    # applies it as RLIMIT_AS in the fork. 0 = no cap.
    mem_limit_bytes: int = 0


@identifiable_dataclass
class SupervisorSpec(Identifiable):
    """A supervisor — owns children and a restart strategy.

    OTP supervisor flags map directly:

    - :attr:`strategy` — :class:`RestartStrategy`.
    - :attr:`max_restarts` (OTP: ``intensity``) — max restarts allowed
      within :attr:`max_seconds` (OTP: ``period``) before the
      supervisor itself terminates abnormally.
    - :attr:`max_seconds` — the sliding-window period for
      ``max_restarts``.

    :attr:`children` is a list of :class:`ChildSpec` *or* nested
    :class:`SupervisorSpec` — that's how OTP trees compose, and how
    our "services tree + vendor apps tree under one root" works.
    """

    name: str
    strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE
    max_restarts: int = 3
    max_seconds: int = 5
    children: list["ChildSpec | SupervisorSpec"] = field(default_factory=list)
    # Project extension (root-only convention): where the supervisor
    # binary should look for libtombstone-emitted tombstone files when a
    # child dies from a fatal signal. Empty = no surfacing.
    tombstone_dir: str = ""


# ---------------------------------------------------------------------------
# SupervisorNode — declarative supervisor entry on a Layer/Rig
# ---------------------------------------------------------------------------


@identifiable_dataclass
class SupervisorNode(Identifiable):
    """One supervisor declared in the manifest (an executor.py sidecar entry).

    A :class:`SupervisorNode` references its children *by name*: another
    SupervisorNode (a nested supervisor) or a process name in the
    deployment's execution axis (a leaf). gen-manifest emits these into the
    write-once ``executor.py`` sidecar, and ``serialize-manifest`` reads the
    module's ``SUPERVISORS`` list and slices it per machine into
    ``executor.json`` (a leaf survives on a machine when its process is
    bound there).

    The order of names in :attr:`children` is the spec order — meaningful
    for ``rest_for_one`` (which kills children declared after the
    failing one).

    Root inference: the supervisor whose name appears in no other
    supervisor's ``children`` list. Exactly one must qualify.

    The optional :attr:`machine` field pins this SupervisorNode to a
    specific machine name (None = workspace-wide).
    """

    name: str
    strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE
    max_restarts: int = 3
    max_seconds: int = 5
    children: list[str] = field(default_factory=list)
    tombstone_dir: str = ""
    machine: "str | None" = None

