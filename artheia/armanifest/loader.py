"""Load Adaptive Platform manifests from ``.art`` sources.

Walks the canonical platform layout:

    <root>/<short>/package.art

— one file per Functional Cluster — parses each through the artheia
textX metamodel and synthesises the Python-side manifests:

- One :class:`ServiceManifest` (named ``platform_services``) containing
  a :class:`ServiceInterface` + :class:`ServiceInstance` per FC. The
  service interface today carries a single ``GetStatus()`` stub; the
  service instance is :data:`TransportBinding.TIPC` with the TIPC
  address declared in the ``.art`` file.
- A list of :class:`ExecutionManifest` (one per FC) with sensible
  defaults: :data:`SchedulingPolicy.OTHER`, no affinity, no memory
  cap. Layered vendor files :class:`Override` these per-FC.

The loader doesn't *configure* the platform — it derives the manifest
shape from authoritative ``.art`` sources so the Python model stays in
sync with what the textX grammar describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from artheia.armanifest.clusters import CLUSTERS, BY_SHORT, FunctionalCluster
from artheia.armanifest.execution import (
    ExecutionManifest,
    Process,
    SchedulingPolicy,
    StartupConfig,
    StateDependentStartupConfig,
)
from artheia.armanifest.service import (
    ClientServerOperation,
    DataType,
    ServiceInstance,
    ServiceInterface,
    ServiceManifest,
    TipcAddress,
    TransportBinding,
    VariableDataPrototype,
)
from artheia.model.loader import parse_file


@dataclass(frozen=True)
class LoadedFc:
    """One Functional Cluster as parsed from its ``.art`` source."""

    cluster: FunctionalCluster
    node_name: str            # e.g. "CryptoDaemon"
    tipc_type: int            # u32
    tipc_instance: int        # u32
    art_path: Path


def _parse_tipc(value: object) -> int:
    """Coerce textX's tipc field to int (it arrives as a hex string)."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise TypeError(f"unexpected tipc value type: {type(value).__name__}")


def load_fc(short: str, art_root: Path) -> LoadedFc:
    """Parse one FC's ``package.art`` under ``<art_root>/<short>/``."""
    cluster = BY_SHORT.get(short)
    if cluster is None:
        raise ValueError(f"{short!r} is not a known Functional Cluster short name")

    path = art_root / short / "package.art"
    if not path.exists():
        raise FileNotFoundError(f"no .art file for FC {short!r}: {path}")

    model = parse_file(path)
    node = next(
        (el for el in model.elements if type(el).__name__ == "NodeDecl"),
        None,
    )
    if node is None:
        raise ValueError(f"{path} declares no atomic node")

    return LoadedFc(
        cluster=cluster,
        node_name=node.name,
        tipc_type=_parse_tipc(node.tipc.type),
        tipc_instance=_parse_tipc(node.tipc.instance),
        art_path=path,
    )


def load_all(
    art_root: Path,
    shorts: Iterable[str] | None = None,
) -> list[LoadedFc]:
    """Load every FC found under ``art_root``. Order matches :data:`CLUSTERS`."""
    targets = list(shorts) if shorts is not None else [fc.short for fc in CLUSTERS]
    out: list[LoadedFc] = []
    for short in targets:
        out.append(load_fc(short, art_root))
    return out


# ---------------------------------------------------------------------------
# Manifest synthesis
# ---------------------------------------------------------------------------


_STATUS_REPORT = DataType(name="StatusReport", base_type="struct")


def _interface_for(fc: LoadedFc) -> ServiceInterface:
    name = f"{fc.cluster.short.capitalize()}If"
    return ServiceInterface(
        name=name,
        major_version=1,
        minor_version=0,
        method=[
            ClientServerOperation(
                name="GetStatus",
                arguments=[],  # returns StatusReport — modelled at the wire level
            ),
        ],
        event=[
            # Each FC emits a periodic health beacon; payload TBD.
            VariableDataPrototype(name="HealthBeacon", payload=_STATUS_REPORT),
        ],
    )


def _instance_for(fc: LoadedFc, iface: ServiceInterface) -> ServiceInstance:
    return ServiceInstance(
        name=fc.cluster.short,
        interface=iface,
        instance_id=fc.tipc_type & 0xFFFF,  # low 16 bits = stable id
        binding=TransportBinding.TIPC,
        tipc=TipcAddress(type=fc.tipc_type, instance=fc.tipc_instance),
    )


def _execution_for(fc: LoadedFc) -> Process:
    """Synthesise the per-FC :class:`Process` (a.k.a. ExecutionManifest).

    AUTOSAR-spec mapping (§8):
    - ``Process.executable`` ← node name from the FC's ``.art`` file
      (e.g. ``CryptoDaemon``).
    - ``Process.functionClusterAffiliation`` ← FC short name uppercased
      (e.g. ``CRYPTO``). The spec standardises a few values
      (``STATE_MANAGEMENT``, ``PLATFORM_HEALTH_MANAGEMENT``); for
      others a project-specific value is allowed.
    - ``Process.stateDependentStartupConfig`` ← one entry binding the
      process to the ``Running`` state of the ``Default`` FunctionGroup
      with a default ``StartupConfig`` (SCHED_OTHER, priority 0).
    """
    return Process(
        name=fc.cluster.short,
        executable=fc.node_name,
        function_cluster_affiliation=fc.cluster.short.upper(),
        state_dependent_startup_config=[
            StateDependentStartupConfig(
                function_group_state=["Default.Running"],
                startup_config=StartupConfig(
                    name=f"{fc.cluster.short}_default_startup",
                    scheduling_policy=SchedulingPolicy.SCHED_OTHER,
                    scheduling_priority=0,
                ),
            ),
        ],
    )


def build_platform_manifests(
    loaded: Iterable[LoadedFc],
) -> tuple[ServiceManifest, list[Process]]:
    """Synthesise the platform ``ServiceManifest`` + per-FC ``ExecutionManifest`` list."""
    loaded = list(loaded)

    interfaces = [_interface_for(fc) for fc in loaded]
    instances = [_instance_for(fc, iface) for fc, iface in zip(loaded, interfaces)]
    execs = [_execution_for(fc) for fc in loaded]

    svc = ServiceManifest(
        name="platform_services",
        data_types=[_STATUS_REPORT],
        interfaces=interfaces,
        instances=instances,
    )
    return svc, execs


def load_platform_services(
    art_root: Path,
    shorts: Iterable[str] | None = None,
) -> tuple[ServiceManifest, list[Process]]:
    """End-to-end: walk ``art_root``, return ServiceManifest + ExecutionManifest list."""
    loaded = load_all(art_root, shorts=shorts)
    return build_platform_manifests(loaded)
