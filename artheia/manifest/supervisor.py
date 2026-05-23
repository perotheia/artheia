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

from artheia.manifest.transform import Identifiable, identifiable_dataclass


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
    """One supervisor declared in the manifest.

    Distinct from :class:`SupervisorSpec`: a :class:`SupervisorNode`
    references its children *by name*, leaving resolution (to either
    another :class:`SupervisorNode` or a :class:`Process` from the rig's
    execution_manifests) to :func:`build_supervisor_tree`.

    The order of names in :attr:`children` is the spec order — meaningful
    for ``rest_for_one`` (which kills children declared after the
    failing one).

    Root inference: the supervisor whose name appears in no other
    supervisor's ``children`` list. Exactly one must qualify.

    Special child names:

    - ``"<auto:apps>"`` — at build time, expand into ``ChildSpec``
      entries for every non-FC SwComponent on the rig (one
      ``vendor/apps/<name>/daemon.sh`` per component). Provides the
      "app_sup gets populated from SwComponents" behaviour without
      restating every vendor app in the layer file.

    Per-machine projection:

    The optional :attr:`machine` field pins this SupervisorNode to a
    specific :class:`Machine.name`. When :func:`build_supervisor_tree`
    is called with a machine filter, only SupervisorNodes whose
    :attr:`machine` is None (workspace-wide) OR equal to the requested
    machine survive. This is what enables per-machine
    ``execution.yaml`` emission — each ECU runs only the supervisor
    sub-tree relevant to its locally-hosted Processes.

    Leaves (Process references) are filtered the same way via
    ``Process.host_machine`` if set.
    """

    name: str
    strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE
    max_restarts: int = 3
    max_seconds: int = 5
    children: list[str] = field(default_factory=list)
    tombstone_dir: str = ""
    machine: "str | None" = None


# Sentinel child-name that expands into "every non-FC SwComponent as a
# leaf ChildSpec" at build time. Used in the canonical FcLayer for app_sup.
AUTO_APPS_CHILDREN = "<auto:apps>"


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

    from artheia.manifest.clusters import CLUSTERS
    from artheia.manifest.platform import PLATFORM_SERVICES_ROOT
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


def build_supervisor_tree(rig, *, machine: "str | None" = None) -> SupervisorSpec:
    """Compose the executor's supervisor tree from a :class:`Rig`.

    Walks ``rig.supervisors`` (a list of :class:`SupervisorNode` carrying
    children-by-name) and materializes a :class:`SupervisorSpec` tree
    with concrete :class:`ChildSpec` leaves. Single root is inferred as
    the supervisor named in no other supervisor's children list.

    Child-name resolution:

    - Match against another :class:`SupervisorNode` first.
    - Otherwise match against :class:`Process` in
      ``rig.execution_manifests`` — emits a leaf :class:`ChildSpec`
      pointing at ``services/<name>/daemon.sh``.
    - :data:`AUTO_APPS_CHILDREN` expands into one leaf per non-FC
      :class:`SwComponent` on the rig (``vendor/apps/<name>/daemon.sh``).
    - Unknown names are quietly dropped — a layer can :class:`Remove` a
      Process while leaving a supervisor that listed it untouched.

    Process-to-machine affinity (``shall_run_on`` / ``shall_not_run_on``)
    is lifted onto each :class:`ChildSpec` for FC children.

    :param machine: If given, return only the sub-tree relevant to that
        machine — SupervisorNodes pinned to a different machine are
        dropped, and Process leaves whose owning machine is something
        else are dropped too. A sub-supervisor with no surviving
        children after filtering is also dropped. Set ``None`` (default)
        for the whole-tree view used by single-machine setups.

        Process→machine resolution (priority order):

          1. ``rig.process_to_machine_mappings`` entry whose
             ``process`` matches the Process name (spec-aligned).
          2. ``ApplicationManifest.host_machine`` of the AA that owns
             the matching SwComponent.
          3. None — unpinned, included on every machine.
    """
    if not rig.supervisors:
        raise ValueError(
            "rig has no supervisors declared — populate Rig.supervisors "
            "(or set add_supervisors on a Layer) before calling "
            "build_supervisor_tree"
        )

    # FC short → its Process. Process.name == FC short by convention.
    process_by_short = {p.name: p for p in rig.execution_manifests}

    # ProcessToMachineMapping lookup for core-affinity refs.
    ptm_by_process: dict[str, "ProcessToMachineMapping"] = {  # noqa: F821
        m.process: m for m in getattr(rig, "process_to_machine_mappings", [])
    }

    # Process-name → machine resolver. Used only when ``machine`` is set.
    # Process→machine resolution order:
    #   1. PTM entry (spec-aligned, strict).
    #   2. ApplicationManifest.host_machine of the AA that lists the
    #      SwComponent with this Process's name.
    #   3. None (unpinned).
    app_host_by_component: dict[str, str] = {}
    for app in getattr(rig, "applications", []) or []:
        host = getattr(app, "host_machine", "") or ""
        for comp in getattr(app, "components", []) or []:
            if comp.name not in app_host_by_component:
                app_host_by_component[comp.name] = host

    def _process_host(name: str) -> "str | None":
        ptm = ptm_by_process.get(name)
        if ptm and ptm.machine:
            return ptm.machine
        host = app_host_by_component.get(name)
        return host if host else None

    # Supervisor name → SupervisorNode.
    sup_by_name: dict[str, SupervisorNode] = {s.name: s for s in rig.supervisors}

    def _ids_from_refs(refs: list[str]) -> list[int]:
        out = []
        for r in refs:
            try:
                out.append(int(r))
            except ValueError:
                # TODO: resolve named ProcessorCore via machine manifest.
                continue
        return out

    def _fc_child(short: str) -> ChildSpec | None:
        """Build a leaf ChildSpec for an FC (Process in execution_manifests).

        ``start_cmd`` comes from ``Process.start_cmd`` on the matching
        execution manifest entry. Empty is OK — emitted as an empty
        list in executor.yaml, which the C++ supervisor rejects at
        load with a clear "no start command for child <name>" error.
        Setting start_cmd is the rig layer's job (or the FC's own
        manifest/executor.py).
        """
        if short not in process_by_short:
            return None
        proc = process_by_short[short]
        ptm = ptm_by_process.get(short)
        start_cmd = list(getattr(proc, "start_cmd", []) or [])
        if not start_cmd:
            import warnings
            warnings.warn(
                f"FC {short!r} has no start_cmd set on its Process — "
                f"the supervisor will refuse to launch it. Set "
                f"start_cmd in the rig overlay or in "
                f"manifest/services/{short}/executor.py.",
                stacklevel=3,
            )
        return ChildSpec(
            name=short,
            start_cmd=start_cmd,
            restart=RestartType.PERMANENT,
            shutdown=5000,
            type=ChildType.WORKER,
            modules=[f"services/{short}"],
            shall_run_on=_ids_from_refs(ptm.shall_run_on) if ptm else [],
            shall_not_run_on=_ids_from_refs(ptm.shall_not_run_on) if ptm else [],
        )

    def _auto_app_children() -> list[ChildSpec]:
        """Expand AUTO_APPS_CHILDREN into one leaf per non-FC SwComponent.

        When ``machine`` is set, filter to apps whose owning AA's
        ``host_machine`` matches.
        """
        out: list[ChildSpec] = []
        for app in rig.applications:
            host = getattr(app, "host_machine", "") or ""
            if machine is not None and host and host != machine:
                continue
            for comp in app.components:
                if comp.bazel_target.startswith("//services/"):
                    continue
                out.append(
                    ChildSpec(
                        name=comp.name,
                        start_cmd=[f"vendor/apps/{comp.name}/daemon.sh"],
                        restart=RestartType.PERMANENT,
                        shutdown=5000,
                        type=ChildType.WORKER,
                        modules=[comp.bazel_target],
                    )
                )
        return out

    # Detect cycles in the supervisor graph; bail early on detection.
    visiting: set[str] = set()

    def _leaf_matches_machine(short: str) -> bool:
        """True if the FC leaf should be included on the target machine.

        Unpinned (no PTM / no AA host_machine) = include on every
        machine. Pinned = include only when matching.
        """
        if machine is None:
            return True
        host = _process_host(short)
        if not host:
            return True   # unpinned → workspace-wide
        return host == machine

    def _materialize(name: str) -> "SupervisorSpec | None":
        if name in visiting:
            raise ValueError(
                f"supervisor cycle through '{name}'; check Rig.supervisors"
            )
        visiting.add(name)
        try:
            node = sup_by_name[name]
            # Per-supervisor machine pin. None = workspace-wide.
            if machine is not None and node.machine and node.machine != machine:
                return None
            kids: list[ChildSpec | SupervisorSpec] = []
            for child_name in node.children:
                if child_name == AUTO_APPS_CHILDREN:
                    kids.extend(_auto_app_children())
                    continue
                if child_name in sup_by_name:
                    sub = _materialize(child_name)
                    if sub is not None:
                        kids.append(sub)
                    continue
                if not _leaf_matches_machine(child_name):
                    continue
                ch = _fc_child(child_name)
                if ch is not None:
                    kids.append(ch)
                # else: quietly drop — name didn't resolve to either kind.

            # A sub-supervisor that pruned all its children disappears
            # from its parent's child list. Exception: the root is
            # always returned (an empty root is a valid result —
            # "this machine runs nothing" — caller decides what to do).
            if not kids and name != roots[0]:
                return None

            return SupervisorSpec(
                name=node.name,
                strategy=node.strategy,
                max_restarts=node.max_restarts,
                max_seconds=node.max_seconds,
                children=kids,
                tombstone_dir=node.tombstone_dir,
            )
        finally:
            visiting.discard(name)

    # Root inference: the supervisor named in no other's children list.
    all_named_children: set[str] = set()
    for s in rig.supervisors:
        all_named_children.update(s.children)
    roots = [s.name for s in rig.supervisors if s.name not in all_named_children]
    if len(roots) == 0:
        raise ValueError(
            "no supervisor root found — every declared supervisor is also "
            "named as a child somewhere (cycle?)"
        )
    if len(roots) > 1:
        raise ValueError(
            f"multiple supervisor roots found ({sorted(roots)}); the "
            "supervisor tree must have exactly one root"
        )

    return _materialize(roots[0])
