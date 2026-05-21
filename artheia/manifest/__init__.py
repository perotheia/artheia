"""The artheia manifest ADT.

Ports the mosaic ``tools.syscomp`` data model so vehicle configurations can
be authored in plain Python and serialized to the YAML rig manifest the
runtime consumes.

See ``docs/autosar/manifest.md`` for the conceptual model.
"""

from artheia.manifest.core import (  # noqa: F401
    AAOSBuildType,
    AndroidComputeElement,
    AWSSecretsItem,
    CPUArchitecture,
    GatewayGroundStationConfig,
    HardwareConnection,
    HardwareElement,
    HardwareElementInstance,
    HardwareSpecification,
    HostCompute,
    MosaicComputeElement,
    MosaicPackage,
    MosaicPackageInstance,
    RigClass,
    SoftwareSpecification,
    SSHAWSTunnelConfig,
    SystemState,
    VehicleInstance,
    VirtualMachine,
)
from artheia.manifest.datatypes import (  # noqa: F401
    BazelTarget,
    Developer,
    DoipAddress,
    HostOSContentHash,
    HostOSVersion,
    Identity,
    IPAddress,
    IPv4Address,
    IPv6Address,
    ImageTag,
    MACAddress,
)
from artheia.manifest.transform import (  # noqa: F401
    Append,
    Default,
    Defer,
    Identifiable,
    Layer,
    Remove,
    SetTransformTypes,
    Undefined,
)
