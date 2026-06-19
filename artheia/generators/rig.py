"""Generate a rig.py scaffold from an artheia composition.

Given a top-level composition like:

    package system.demo
    composition Demo3Way {
        prototype CounterNode counter_p1 on process P1
        prototype DriverNode  driver_p1  on process P1
        prototype TickerNode  ticker_p1  on process P1
        prototype ObserverNode observer_p2 on process P2
        prototype IncrementerNode incrementer_p3 on process P3
        ...
    }

The generator emits a Python module exporting:

- ``<Vehicle>Host: MachineManifest`` — placeholder host machine
  (the user fills in IP / arch / endpoint).
- ``<VEHICLE>_COMPONENTS: list[SwComponent]`` — one per distinct
  ``on process X`` value in the composition.
- ``<VEHICLE>_PROCESSES: list[Process]`` — sensible defaults; user
  edits scheduling / priority / affinity in the Layer.
- ``<Vehicle>SpecLayer: SoftwareSpecification`` — the structured-DSL
  delta layer.
- ``<Vehicle>Software = ServicesSoftware.mappend(<Vehicle>SpecLayer)`` — the
  composed spec the CLI consumes.

Bootstrap, not round-trip — once the user edits the generated file,
regeneration would clobber their work. The generator is for first
draft; from there, vendor-side maintenance is hand-edits.

See ``docs/tasks/PROGRESS/generate-rig-from-system.md`` for the full
spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from artheia.model import flatten_composition, parse_file


# ---------------------------------------------------------------------------
# Data extracted from the .art composition.
# ---------------------------------------------------------------------------


@dataclass
class _ProcessSlot:
    """One process binary derived from a group of `on process X`
    prototypes inside the composition."""

    art_process: str                # The `P1` / `P2` from `on process P1`.
    prototypes: list[str]           # Prototype names hosted on this process.
    node_types: list[str]           # NodeDecl names referenced as prototype types.


@dataclass
class _CompositionInfo:
    package: str                    # e.g. "system.demo"
    name: str                       # e.g. "Demo3Way"
    processes: list[_ProcessSlot]   # in deterministic order


def _extract_composition_info(
    art_path: Path,
    composition_name: str,
) -> _CompositionInfo:
    """Parse the .art file and pull out the composition's process
    groups via ``flatten_composition``."""
    model = parse_file(str(art_path))
    package = getattr(model, "name", "") or ""

    comp = None
    for el in model.elements:
        if (
            type(el).__name__ == "CompositionDecl"
            and el.name == composition_name
        ):
            comp = el
            break
    if comp is None:
        raise ValueError(
            f"composition {composition_name!r} not found in {art_path}"
        )

    proto_decls, _connects = flatten_composition(comp)

    # Group prototypes by their `on process X` annotation.
    by_process: dict[str, _ProcessSlot] = {}
    for p in proto_decls:
        proc = getattr(p, "process", None) or "default"
        node_type = p.type.name
        slot = by_process.setdefault(
            proc, _ProcessSlot(art_process=proc, prototypes=[], node_types=[])
        )
        slot.prototypes.append(p.name)
        if node_type not in slot.node_types:
            slot.node_types.append(node_type)

    # Preserve declaration order — `flatten_composition` already
    # walks the source in textual order.
    seen = set()
    ordered: list[_ProcessSlot] = []
    for p in proto_decls:
        proc = getattr(p, "process", None) or "default"
        if proc in seen:
            continue
        seen.add(proc)
        ordered.append(by_process[proc])

    return _CompositionInfo(
        package=package,
        name=composition_name,
        processes=ordered,
    )


# ---------------------------------------------------------------------------
# Naming conventions.
# ---------------------------------------------------------------------------


def _process_name(vehicle: str, art_proc: str) -> str:
    """`P1` + vehicle=demo → `demo_p1`. Lowercase the process token
    and prefix with the vehicle name."""
    return f"{vehicle}_{art_proc.lower()}"


def _bazel_target(bazel_package: str, vehicle: str, art_proc: str) -> str:
    """`//apps` + vehicle=demo + art_proc=P1 → `//apps:p1_main`. The
    `_main` suffix is the rig-manifest convention; `//apps:p1_main`
    aliases the per-composition fc app binary
    (//apps/Demo3WayP1/main:demo — see apps/BUILD.bazel). The bazel
    target name is just the art_proc token lowercased (no vehicle prefix
    — the package already says `//apps`)."""
    pkg = bazel_package.rstrip(":/")
    return f"{pkg}:{art_proc.lower()}_main"


def _composition_class(comp_name: str) -> str:
    """`Demo3Way` → `Demo3WayComposition` (the composition's C++ class
    name)."""
    return f"{comp_name}Composition"


def _per_process_class(comp_name: str, art_proc: str) -> str:
    """For `Demo3Way` + `P1` → `DemoP1Composition`. Today the demo
    uses ``DemoP1Composition``, ``DemoP2Composition``, etc. as the
    per-process root types — derive matching names by capitalizing
    the art_proc token."""
    # Drop trailing digits from comp_name's stem (Demo3Way → Demo).
    # Pragmatic: just take alpha prefix.
    stem = ""
    for ch in comp_name:
        if ch.isalpha():
            stem += ch
        else:
            break
    if not stem:
        stem = comp_name
    return f"{stem}{art_proc.capitalize()}Composition"


def _vehicle_capitalize(vehicle: str) -> str:
    """`demo` → `Demo`. `multi_word` → `MultiWord`."""
    return "".join(p.capitalize() for p in vehicle.split("_"))


# ---------------------------------------------------------------------------
# Code emission.
# ---------------------------------------------------------------------------


_HEADER_TEMPLATE = '''\
"""Generated by ``artheia gen-rig`` from composition ``{composition_fqn}``.

THIS FILE IS A BOOTSTRAP SCAFFOLD. Once you edit it, the generator
won't re-run safely — round-trip regen would clobber your edits.

The composition's prototypes were grouped by their ``on process X``
annotation; each group becomes a process binary built from the
per-composition fc app (``artheia gen-app --kind fc --composition
<Name>``).

TODO markers below flag the deployment-specific decisions the
generator cannot infer:
- ``{Vehicle}Host`` hardware + network endpoint
- per-process CPU affinity / scheduling priority
- supervisor tree shape (the default inherits ``ServicesSoftware``'s)
"""

from __future__ import annotations

from ipaddress import IPv4Address
from typing import cast

from artheia.manifest import (
    ApplicationManifest,
    CpuArchitecture,
    HardwareResource,
    MachineManifest,
    SwComponent,
    VehicleIdentity,
)
from artheia.manifest.application import (
    BuildTypeEnum,
    Executable,
    ExecutionStateReportingBehaviorEnum,
    RootSwComponentPrototype,
)
from artheia.manifest.execution import (
    Process,
    SchedulingPolicy,
    StartupConfig,
    StateDependentStartupConfig,
    TerminationBehaviorEnum,
)
from artheia.manifest.machine import CpuResource, IpEndpoint
from artheia.manifest.rig import SoftwareSpecification
from artheia.manifest.applicative import Append, SetTransformTypes
from services.manifest.service import ServicesSoftware


# ---------------------------------------------------------------------------
# Host machine — TODO: set CPU arch, IP, gRPC endpoint, hardware resources.
# ---------------------------------------------------------------------------

{Vehicle}Host = MachineManifest(
    name="{machine_name}",
    hardware=HardwareResource(
        cpu=CpuResource(architecture=CpuArchitecture.X86_64),  # TODO: real arch
    ),
    com_endpoint=IpEndpoint(
        address=IPv4Address("127.0.0.1"),                       # TODO: real IP
        port={grpc_port},
    ),
)
'''


_PROCESS_TABLE_TEMPLATE = '''
# ---------------------------------------------------------------------------
# Process binaries — one per distinct ``on process X`` group in
# ``{composition_fqn}``.
# ---------------------------------------------------------------------------

_PROCESSES = [
    # (process_name, art_class, bazel_target, [prototype, ...])
{process_table_rows}
]


{vehicle_upper}_COMPONENTS: list[SwComponent] = [
    SwComponent(
        name=name,
        bazel_target=target,
        owner="platform",
        art_node=f"{composition_pkg}/{{art_class}}",
    )
    for (name, art_class, target, _) in _PROCESSES
]


def _executable_for(name: str, art_class: str) -> Executable:
    return Executable(
        name=name,
        category="APPLICATION_LEVEL",
        build_type=BuildTypeEnum.BUILD_TYPE_RELEASE,
        reporting_behavior=(
            ExecutionStateReportingBehaviorEnum.REPORTING_BEHAVIOR_INDIVIDUAL
        ),
        root_sw_component_prototype=RootSwComponentPrototype(
            name=f"{{name}}_root",
            application_type=art_class,
        ),
    )


{vehicle_upper}_EXECUTABLES: list[Executable] = [
    _executable_for(name, art_class)
    for (name, art_class, _, _) in _PROCESSES
]


def _process_for(name: str) -> Process:
    return Process(
        name=name,
        executable=name,
        # TODO: set function_cluster_affiliation if this rig is a
        # platform-level layer.
        function_cluster_affiliation="",
        state_dependent_startup_config=[
            StateDependentStartupConfig(
                function_group_state=["Default.Running"],
                startup_config=StartupConfig(
                    name=f"{{name}}_startup",
                    # TODO: per-process scheduling — adjust here, or
                    # add NodeToCPUMapping entries to the Layer below.
                    scheduling_policy=SchedulingPolicy.SCHED_OTHER,
                    scheduling_priority=0,
                    termination_behavior=(
                        TerminationBehaviorEnum.PROCESS_IS_NOT_SELF_TERMINATING
                    ),
                ),
            ),
        ],
    )


{vehicle_upper}_PROCESSES: list[Process] = [
    _process_for(name) for (name, _, _, _) in _PROCESSES
]
'''


_LAYER_TEMPLATE = '''
# ---------------------------------------------------------------------------
# {Vehicle}SpecLayer — structured-DSL delta over ServicesSoftware.
# Same-identity Append (name="platform_app") merges via Layer.mappend,
# so {Vehicle}'s binaries land alongside FC components on {Vehicle}Host.
# ---------------------------------------------------------------------------

_{Vehicle}PlatformApp = ApplicationManifest(
    name="platform_app",
    host_machine={Vehicle}Host.name,
    components=list({vehicle_upper}_COMPONENTS),
)

{Vehicle}SpecLayer = SoftwareSpecification(
    vehicle=VehicleIdentity(
        name="{vehicle}",
        make="theia",
        model="{composition_fqn}",
    ),
    machines=cast(set[SetTransformTypes], {{
        Append({Vehicle}Host),
    }}),
    applications=cast(set[SetTransformTypes], {{
        Append(_{Vehicle}PlatformApp),
    }}),
    execution_manifests=cast(set[SetTransformTypes], {{
        Append(p) for p in {vehicle_upper}_PROCESSES
    }}),
    # TODO: NodeToCPUMapping entries here if per-thread affinity /
    # scheduling priority is needed.
)


# ---------------------------------------------------------------------------
# Final spec — combine onto ServicesSoftware. The CLI auto-picks this
# (it prefers ``*Software`` exports).
# ---------------------------------------------------------------------------

{Vehicle}Software: SoftwareSpecification = ServicesSoftware.mappend({Vehicle}SpecLayer)
'''


def generate_rig_py(
    art_path: Path,
    composition_name: str,
    vehicle_name: str,
    machine_name: str,
    bazel_package: str,
    grpc_port: int = 7700,
) -> str:
    """Build the rig.py source string. Caller writes it where they want."""

    info = _extract_composition_info(art_path, composition_name)

    Vehicle = _vehicle_capitalize(vehicle_name)
    VEHICLE = vehicle_name.upper()
    composition_fqn = (
        f"{info.package}.{info.name}" if info.package else info.name
    )

    header = _HEADER_TEMPLATE.format(
        composition_fqn=composition_fqn,
        Vehicle=Vehicle,
        machine_name=machine_name,
        grpc_port=grpc_port,
    )

    # Build the _PROCESSES tuple table.
    process_rows: list[str] = []
    for slot in info.processes:
        proc_name = _process_name(vehicle_name, slot.art_process)
        art_class = _per_process_class(info.name, slot.art_process)
        bazel_target = _bazel_target(bazel_package, vehicle_name, slot.art_process)
        protos_repr = ", ".join(repr(p) for p in slot.prototypes)
        process_rows.append(
            f"    ({proc_name!r}, {art_class!r},\n"
            f"     {bazel_target!r},\n"
            f"     [{protos_repr}]),"
        )

    proc_table = _PROCESS_TABLE_TEMPLATE.format(
        composition_fqn=composition_fqn,
        composition_pkg=info.package,
        vehicle_upper=VEHICLE,
        process_table_rows="\n".join(process_rows),
    )

    layer = _LAYER_TEMPLATE.format(
        Vehicle=Vehicle,
        vehicle=vehicle_name,
        vehicle_upper=VEHICLE,
        composition_fqn=composition_fqn,
    )

    return header + proc_table + layer


def write_rig_py(
    art_path: Path,
    composition_name: str,
    out_path: Path,
    vehicle_name: str,
    machine_name: str,
    bazel_package: str,
    grpc_port: int = 7700,
    force: bool = False,
) -> None:
    """Write the generated source to ``out_path``. Refuses to
    overwrite an existing non-empty file unless ``force`` is set."""
    if out_path.exists() and out_path.stat().st_size > 0 and not force:
        raise FileExistsError(
            f"{out_path} exists and is non-empty; pass force=True to overwrite"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = generate_rig_py(
        art_path=art_path,
        composition_name=composition_name,
        vehicle_name=vehicle_name,
        machine_name=machine_name,
        bazel_package=bazel_package,
        grpc_port=grpc_port,
    )
    out_path.write_text(text)
