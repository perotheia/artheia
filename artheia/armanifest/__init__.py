"""Adaptive-AUTOSAR-compliant manifest model.

Four manifest kinds following AUTOSAR's split:

================ =========================================================
:mod:`.application`  Application Manifest — per Adaptive Application.
:mod:`.machine`      Machine Manifest — per machine (ECU / VM).
:mod:`.service`      Service Manifest — per process SOA bindings.
:mod:`.execution`    Execution Manifest — per process deployment.
================ =========================================================

Plus three supporting modules:

- :mod:`.rig` — :class:`Rig` bundles a vehicle identity with N machines
  + M applications + service manifests. The vendor-side top-level.
- :mod:`.layer` — :class:`Layer` + :func:`merge_layers` compose deltas
  (platform → vehicle-family → concrete rig) into a final :class:`Rig`.
- :mod:`.transform` — identity-keyed :class:`Add` / :class:`Remove` /
  :class:`Override` primitives the layer system runs on.
- :mod:`.clusters` — :data:`CLUSTERS` catalogue of the 18 Adaptive
  Platform Functional Clusters by short name.

See ``docs/autosar/manifest.md`` for the conceptual model.
"""

from artheia.armanifest.application import (  # noqa: F401
    ApplicationManifest,
    SwComponent,
)
from artheia.armanifest.clusters import (  # noqa: F401
    BY_SHORT as CLUSTER_BY_SHORT,
    CLUSTERS,
    FunctionalCluster,
)
from artheia.armanifest.execution import ExecutionManifest  # noqa: F401
from artheia.armanifest.layer import Layer, apply_layer, merge_layers  # noqa: F401
from artheia.armanifest.machine import (  # noqa: F401
    CpuArchitecture,
    HardwareResource,
    MachineManifest,
)
from artheia.armanifest.platform import (  # noqa: F401
    PlatformApplication,
    PlatformBase,
    PlatformServices,
)
from artheia.armanifest.rig import Rig, VehicleIdentity  # noqa: F401
from artheia.armanifest.service import (  # noqa: F401
    InetEndpoint,
    ServiceInstance,
    ServiceInterface,
    ServiceManifest,
    TipcAddress,
    TransportBinding,
)
from artheia.armanifest.transform import (  # noqa: F401
    Add,
    Identifiable,
    Override,
    Remove,
    apply_ops,
)

__all__ = [
    "Add",
    "ApplicationManifest",
    "CLUSTERS",
    "CLUSTER_BY_SHORT",
    "CpuArchitecture",
    "ExecutionManifest",
    "FunctionalCluster",
    "HardwareResource",
    "Identifiable",
    "InetEndpoint",
    "Layer",
    "MachineManifest",
    "Override",
    "PlatformApplication",
    "PlatformBase",
    "PlatformServices",
    "Remove",
    "Rig",
    "ServiceInstance",
    "ServiceInterface",
    "ServiceManifest",
    "SwComponent",
    "TipcAddress",
    "TransportBinding",
    "VehicleIdentity",
    "apply_layer",
    "apply_ops",
    "merge_layers",
]
