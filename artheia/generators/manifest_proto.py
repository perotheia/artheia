"""Generate ``services/manifest/service.py`` from a system ``.art``.

The manifest module is emitted from the ``.art`` spec, not a hand-kept
Python list — the ``.art`` is the source of truth. AUTOSAR Adaptive
(ARA) splits a deployment into four manifest kinds; this generator
emits a module organised around them:

  * **Application** — one section per ``cluster`` in the ``.art``. Each
    cluster contributes its members as SwComponent + Executable +
    Process triples, under a ``<Cluster>_*`` group named LITERALLY after
    the cluster (``Services``, ``Applications``, ``Platform``, …). The
    generator assigns no meaning to a cluster name — it just emits one
    section per cluster found.
  * **Machine** — empty here. Machines are a deploy-time concern filled
    in by rig layers (``demo/manifest/rig.py``), never by the spec.
  * **Service** — the ServiceManifest instances; derived by the loader
    from the same cluster members (kept in ``platform.py``).
  * **Execution** — the Processes (one per cluster member).

A cluster member's handle (the ``ident`` in ``composition Com com``) is
the FC/app short name; ``ClusterMember.name`` carries it. Members that
aren't in any cluster (daemon-less placeholders: crypto, idsm, …) never
reach the manifest.

Everything except the per-cluster member lists is fixed boilerplate
emitted verbatim, so the generated file stays diff-stable.
"""

from __future__ import annotations

from pathlib import Path

from artheia.model import parse_file


def _cluster_members(art_file: str) -> "list[tuple[str, list[str]]]":
    """Return ``[(cluster_name, [member_short, ...]), ...]`` for every
    ``cluster`` declared in *art_file*, in source order.

    A member's short is ``ClusterMember.name`` (the ``ident`` in
    ``composition Com com``), with ``instance_name`` as a fallback for
    older grammars. Empty-body clusters yield an empty member list.
    Raises ``ValueError`` if the file declares no clusters at all.
    """
    model = parse_file(art_file)
    out: list[tuple[str, list[str]]] = []
    for el in getattr(model, "elements", []):
        if type(el).__name__ != "ClusterDecl":
            continue
        members: list[str] = []
        for mem in getattr(el, "elements", []):
            if type(mem).__name__ != "ClusterMember":
                continue
            short = getattr(mem, "name", None) or getattr(
                mem, "instance_name", None
            )
            if short:
                members.append(short)
        out.append((el.name, members))
    if not out:
        raise ValueError(
            f"{art_file}: no `cluster` declaration found — nothing to "
            f"generate"
        )
    return out


def _py_ident(cluster_name: str) -> str:
    """Uppercase, identifier-safe prefix for a cluster's section vars."""
    return "".join(c if c.isalnum() else "_" for c in cluster_name).upper()


# ---------------------------------------------------------------------------
# File template. Two substitution points:
#   {source}   — provenance (the .art path)
#   {sections} — the per-cluster member-short blocks + the aggregate
#                lists, rendered by _render_sections().
# Everything else is fixed boilerplate.
# ---------------------------------------------------------------------------

_HEADER = '''\
"""Adaptive Platform manifest — GENERATED from {source}.

Do not edit by hand. Edit the ``cluster`` declarations in the source
``.art`` and regenerate:

    artheia gen-manifest-proto {source} <this file>

ARA manifest sections (see docs/autosar/manifest.md):

  * Application — one ``<Cluster>_*`` group per ``cluster`` in the .art
                  (SwComponent + Executable + Process per member).
  * Machine     — empty; rig layers (demo/manifest/rig.py) fill it.
  * Service     — ServiceManifest instances (loader-derived in
                  platform.py from the same cluster members).
  * Execution   — Processes (one per cluster member).

Upper layers patch this base by name (:class:`Override`) — see
``demo/manifest/rig.py``.
"""

from __future__ import annotations

# Import from submodules directly (not from artheia.manifest) so this
# module can be imported by artheia.manifest.platform without creating
# a circular dependency through artheia/manifest/__init__.py.
from artheia.manifest.application import (
    BuildTypeEnum,
    Executable,
    ExecutionStateReportingBehaviorEnum,
    RootSwComponentPrototype,
    SwComponent,
)
from artheia.manifest.execution import (
    Process,
    SchedulingPolicy,
    StartupConfig,
    StateDependentStartupConfig,
    TerminationBehaviorEnum,
)
from artheia.manifest.layer import Layer


def _component_for(short: str) -> SwComponent:
    """One bazel-buildable handle per cluster member."""
    daemon_class = "".join(p.capitalize() for p in short.split("_")) + "Daemon"
    return SwComponent(
        name=short,
        bazel_target=f"//services/{{short}}",
        owner="platform",
        art_node=f"services.{{short}}/{{daemon_class}}",
    )


def _executable_for(short: str) -> Executable:
    """Adaptive Application Manifest Executable entry (§3.18)."""
    daemon_class = "".join(p.capitalize() for p in short.split("_")) + "Daemon"
    return Executable(
        name=short,
        category="PLATFORM_LEVEL",
        build_type=BuildTypeEnum.BUILD_TYPE_RELEASE,
        reporting_behavior=(
            ExecutionStateReportingBehaviorEnum.REPORTING_BEHAVIOR_INDIVIDUAL
        ),
        root_sw_component_prototype=RootSwComponentPrototype(
            name=f"{{short}}_root",
            application_type=daemon_class,
        ),
    )


def _process_for(short: str) -> Process:
    """Execution Manifest Process (§8.2).

    Tries to import ``PROCESS`` from ``manifest.services.<short>.executor``
    (hand-edited, survives ``artheia gen-app``). Falls back to an empty
    start_cmd for members without an executor.py — the supervisor
    refuses to launch those.
    """
    import importlib

    try:
        mod = importlib.import_module(f"manifest.services.{{short}}.executor")
    except ImportError:
        mod = None

    if mod is not None and hasattr(mod, "PROCESS"):
        return mod.PROCESS

    return Process(
        name=short,
        executable=short,
        function_cluster_affiliation=short,
        start_cmd=[],
        state_dependent_startup_config=[
            StateDependentStartupConfig(
                function_group_state=["Default.Running"],
                startup_config=StartupConfig(
                    name=f"{{short}}_startup",
                    scheduling_policy=SchedulingPolicy.SCHED_OTHER,
                    scheduling_priority=0,
                    termination_behavior=(
                        TerminationBehaviorEnum.PROCESS_IS_NOT_SELF_TERMINATING
                    ),
                ),
            ),
        ],
    )


'''

_FOOTER = '''\

# ---------------------------------------------------------------------------
# Machine section — EMPTY. Machines are a deploy-time concern; rig layers
# (demo/manifest/rig.py) add MachineManifests. The spec declares none.
# ---------------------------------------------------------------------------
MACHINES: list = []


# ---------------------------------------------------------------------------
# Supervisor tree — SIDECARED in services/manifest/executor.py.
#
# The supervisor hierarchy (restart strategies + child grouping) is
# hand-authored and has NO .art declaration, so it must survive any
# regeneration of THIS file. It lives in the executor.py sidecar; we
# re-export it here so existing consumers keep reading
# ``service.SUPERVISORS`` unchanged. Edit the tree in executor.py.
# ---------------------------------------------------------------------------

from services.manifest.executor import SUPERVISORS  # noqa: E402,F401


# The Layer instance upper layers compose against (aggregate of all
# clusters). A rig layer (demo/manifest/rig.py) wraps it via
# :func:`merge_layers`; Removals / Overrides reach in by short-name.
FcLayer = Layer(
    name="services.fc",
    add_components=COMPONENTS,
    add_executions=PROCESSES,
    add_supervisors=SUPERVISORS,
)


# ---------------------------------------------------------------------------
# Structured-DSL counterpart — :data:`FcSoftware`.
# ---------------------------------------------------------------------------

from typing import cast

from artheia.manifest.application import ApplicationManifest
from artheia.manifest.rig import SoftwareSpecification, VehicleIdentity
from artheia.manifest.transform import Append, SetTransformTypes  # noqa: E402

_PlatformApplication = ApplicationManifest(
    name="platform_app",
    host_machine="",  # rig layers fill in
    components=list(COMPONENTS),
)

FcSoftware: SoftwareSpecification = SoftwareSpecification(
    vehicle=VehicleIdentity(name=""),  # rig layers override
    applications=cast(set[SetTransformTypes], {{
        Append(_PlatformApplication),
    }}),
    execution_manifests=cast(set[SetTransformTypes], {{
        Append(p) for p in PROCESSES
    }}),
    supervisors=cast(set[SetTransformTypes], {{
        Append(s) for s in SUPERVISORS
    }}),
)


__all__ = [
{exports}
    "MACHINES",
    "COMPONENTS",
    "EXECUTABLES",
    "PROCESSES",
    "SUPERVISORS",
    "FcLayer",
    "FcSoftware",
]
'''


def _render_sections(clusters: "list[tuple[str, list[str]]]") -> tuple[str, str]:
    """Render the per-cluster Application sections + the aggregate
    COMPONENTS/EXECUTABLES/PROCESSES lists.

    Returns ``(sections_block, exports_block)``.
    """
    lines: list[str] = []
    export_names: list[str] = []
    all_prefixes: list[str] = []

    for name, members in clusters:
        pfx = _py_ident(name)
        all_prefixes.append(pfx)
        shorts_lit = ", ".join(f'"{m}"' for m in members)
        lines.append(
            f"# ---------------------------------------------------------"
            f"------------------\n"
            f"# Application section — cluster `{name}`.\n"
            f"# ---------------------------------------------------------"
            f"------------------\n"
            f"{pfx}_SHORTS: list[str] = [{shorts_lit}]\n"
            f"{pfx}_COMPONENTS = [_component_for(s) for s in {pfx}_SHORTS]\n"
            f"{pfx}_EXECUTABLES = [_executable_for(s) for s in {pfx}_SHORTS]\n"
            f"{pfx}_PROCESSES = [_process_for(s) for s in {pfx}_SHORTS]\n"
        )
        for suffix in ("SHORTS", "COMPONENTS", "EXECUTABLES", "PROCESSES"):
            export_names.append(f"{pfx}_{suffix}")

    # Aggregate lists span every cluster (consumers import these).
    comp = " + ".join(f"{p}_COMPONENTS" for p in all_prefixes) or "[]"
    exe = " + ".join(f"{p}_EXECUTABLES" for p in all_prefixes) or "[]"
    proc = " + ".join(f"{p}_PROCESSES" for p in all_prefixes) or "[]"
    lines.append(
        "# ---------------------------------------------------------"
        "------------------\n"
        "# Aggregate across all Application clusters (what consumers "
        "import).\n"
        "# ---------------------------------------------------------"
        "------------------\n"
        f"COMPONENTS = {comp}\n"
        f"EXECUTABLES = {exe}\n"
        f"PROCESSES = {proc}\n"
    )

    exports = "\n".join(f'    "{n}",' for n in export_names)
    return "\n".join(lines), exports


def generate_manifest_proto(art_file: str, out_file: str) -> Path:
    """Render ``service.py`` from *art_file*'s clusters and write it to
    *out_file*. Returns the written path."""
    clusters = _cluster_members(art_file)
    sections, exports = _render_sections(clusters)
    rendered = (
        _HEADER.format(source=art_file)
        + sections
        + _FOOTER.format(exports=exports)
    )
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)
    return out
