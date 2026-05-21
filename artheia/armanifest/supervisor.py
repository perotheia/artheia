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

from artheia.armanifest.transform import Identifiable


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


@dataclass
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
# Tree derivation from a Rig
# ---------------------------------------------------------------------------


def _topo_sort_services(rig: "object") -> list[str]:
    """Return FC short-names in start order (deps first).

    Reads dependencies from each FC's .art file via the artheia textX
    parser — every ``client … requires <Iface>`` port whose required
    interface matches another FC's provided interface becomes an
    inbound edge.

    Falls back to the hardcoded tier order from the .art generator if
    parsing fails (e.g. during early bring-up).
    """
    # Lazy imports keep this module light if only the dataclass is wanted.
    from pathlib import Path

    from artheia.armanifest.clusters import CLUSTERS
    from artheia.armanifest.platform import PLATFORM_SERVICES_ROOT
    from artheia.model.loader import parse_file

    fc_shorts = [fc.short for fc in CLUSTERS]

    # First pass: gather each FC's *provided* interface name.
    provides: dict[str, str] = {}
    requires: dict[str, list[str]] = {s: [] for s in fc_shorts}

    for short in fc_shorts:
        path = Path(PLATFORM_SERVICES_ROOT) / short / "package.art"
        if not path.exists():
            continue
        model = parse_file(path)
        node = next(
            (el for el in model.elements if type(el).__name__ == "NodeDecl"),
            None,
        )
        if node is None:
            continue
        for port in node.ports:
            iface_obj = getattr(port, "iface", None)
            iface_name = getattr(iface_obj, "name", None) if iface_obj else None
            if not iface_name:
                continue
            kind = type(port).__name__
            if kind == "ServerPort":
                provides[short] = iface_name
            elif kind == "ClientPort":
                requires[short].append(iface_name)

    # Resolve required-interface names back to provider FC shorts.
    iface_to_short = {iface: short for short, iface in provides.items()}
    edges: dict[str, set[str]] = {s: set() for s in fc_shorts}
    for short, iface_list in requires.items():
        for iface in iface_list:
            owner = iface_to_short.get(iface)
            if owner and owner != short:
                edges[short].add(owner)

    # Kahn topological sort.
    indeg = {s: 0 for s in fc_shorts}
    for s, deps in edges.items():
        for _ in deps:
            indeg[s] += 1

    out: list[str] = []
    # Sort by name within each level for deterministic output.
    while True:
        ready = sorted(s for s, d in indeg.items() if d == 0)
        if not ready:
            break
        # Pop them all, then process in order.
        for s in ready:
            out.append(s)
            indeg[s] = -1  # mark visited
            for other, deps in edges.items():
                if s in deps:
                    indeg[other] = max(0, indeg[other] - 1)

    # Anything left (cycles) appended at the end, complaint-free.
    leftover = [s for s, d in indeg.items() if d >= 0]
    return out + leftover


def build_supervisor_tree(rig) -> SupervisorSpec:
    """Compose the executor's supervisor tree from a :class:`Rig`.

    Layout:

    - Root supervisor ``root`` with :class:`RestartStrategy.ONE_FOR_ALL`
      so a catastrophic services failure pulls the apps down too.
    - First child: ``services`` supervisor with
      :class:`RestartStrategy.REST_FOR_ONE` and FC children in
      topological order (``core`` first). When ``core`` dies, everything
      after it in the list restarts; localised crashes only affect
      downstream FCs.
    - Second child: ``apps`` supervisor with
      :class:`RestartStrategy.ONE_FOR_ONE`. Each vendor SwComponent
      is one child; a single app crash is local.

    Bash daemon paths follow convention:
    - FC: ``services/<short>/daemon.sh``
    - App: ``vendor/apps/<name>/daemon.sh`` (created by the
      build/install pipeline; the executor expects them on disk).
    """
    # Reorder FCs by dependency.
    fc_order = _topo_sort_services(rig)

    # FC short → its process_name (from the loaded ExecutionManifest /
    # Process — that's the canonical handle).
    process_by_short = {p.name: p for p in rig.execution_manifests}

    # Look up ProcessToMachineMapping by process name so we can lift
    # core-affinity refs (shall_run_on / shall_not_run_on) onto each
    # ChildSpec. The PTM fields are name refs into ProcessorCore in the
    # full AUTOSAR model; until our machine manifests carry named cores
    # the demo treats them as stringified integer IDs.
    ptm_by_process: dict[str, "ProcessToMachineMapping"] = {  # noqa: F821
        m.process: m for m in getattr(rig, "process_to_machine_mappings", [])
    }

    def _ids_from_refs(refs: list[str]) -> list[int]:
        out = []
        for r in refs:
            try:
                out.append(int(r))
            except ValueError:
                # TODO: resolve named ProcessorCore via machine manifest.
                continue
        return out

    services_children: list[ChildSpec | SupervisorSpec] = []
    for short in fc_order:
        proc = process_by_short.get(short)
        if proc is None:
            continue  # FC removed by a layer (e.g. Macan drops fw/shwa)
        ptm = ptm_by_process.get(short)
        services_children.append(
            ChildSpec(
                name=short,
                start_cmd=[f"services/{short}/daemon.sh"],
                restart=RestartType.PERMANENT,
                shutdown=5000,
                type=ChildType.WORKER,
                modules=[f"services/{short}"],
                shall_run_on=_ids_from_refs(ptm.shall_run_on) if ptm else [],
                shall_not_run_on=_ids_from_refs(ptm.shall_not_run_on) if ptm else [],
            )
        )

    # Vendor apps: every SwComponent whose bazel target isn't under
    # //services/ goes into the apps tree (those are the FC components).
    apps_children: list[ChildSpec | SupervisorSpec] = []
    for app in rig.applications:
        for comp in app.components:
            if comp.bazel_target.startswith("//services/"):
                continue
            apps_children.append(
                ChildSpec(
                    name=comp.name,
                    start_cmd=[f"vendor/apps/{comp.name}/daemon.sh"],
                    restart=RestartType.PERMANENT,
                    shutdown=5000,
                    type=ChildType.WORKER,
                    modules=[comp.bazel_target],
                )
            )

    apps_sup = SupervisorSpec(
        name="apps",
        strategy=RestartStrategy.ONE_FOR_ONE,
        max_restarts=3,
        max_seconds=5,
        children=apps_children,
    )

    # The "core fails → apps restart" requirement maps to rest_for_one
    # semantics with a single supervisor: declare core first, then the
    # other FCs in dependency order, and the apps sub-supervisor *last*.
    # When core dies, every later child (including the apps subtree)
    # restarts. When a tier-3 FC dies, only it + apps restart (still
    # cheaper than blowing away core). When an individual app dies,
    # the apps sub-supervisor handles it locally (one_for_one).
    root_children: list[ChildSpec | SupervisorSpec] = services_children + [apps_sup]

    return SupervisorSpec(
        name="root",
        strategy=RestartStrategy.REST_FOR_ONE,
        max_restarts=3,
        max_seconds=5,
        children=root_children,
        tombstone_dir="/tmp/tombstones",
    )
