"""Erlang-style supervisor declaration for the executor.

Models OTP supervisor semantics on top of the manifest. References:

- https://erlang.org/documentation/doc-4.9.1/doc/design_principles/sup_princ.html
- https://www.erlang.org/docs/20/man/supervisor

The live model is :class:`SupervisorNode`: gen-manifest emits a
``SUPERVISORS: list[SupervisorNode]`` sidecar (executor.py), and
``serialize-manifest`` slices it per machine into ``executor.json`` —
the worker/child dicts in that JSON are built directly in cli.py (the
C++ supervisor's spec.cpp parses them). The AUTOSAR :class:`Process` /
Execution-Manifest world separately describes *what* runs; this module
describes *how supervision behaves* when things crash. The two are
intentionally orthogonal — different deployments can pick different
restart policies for the same Process set.

(A fuller OTP dataclass layer — ChildSpec/SupervisorSpec/NodeInfo with
RestartType/ChildType enums — used to live here but was never
instantiated; the hand-built executor.json dicts superseded it and it
was removed in the 2026-07 dead-code sweep. The C++ side's RestartType
lives in the supervisor binary's spec.h, parsed from the JSON strings.)
"""

from __future__ import annotations

from dataclasses import field
from enum import Enum

from artheia.manifest.algebra import Identifiable, identifiable_dataclass


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
