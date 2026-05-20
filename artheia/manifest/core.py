from __future__ import annotations

import dataclasses
from abc import ABC
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import PosixPath
from typing import (
    Any,
    Generic,
    Optional,
    TypeVar,
    Union,
    cast,
)

from artheia.manifest.stubs import rig_pb2
from artheia.manifest.transform import (
    Default,
    Defer,
    Identifiable,
    IsProtobuf,
    Layer,
    SetTransformTypes,
    SimpleSetTransformTypes,
    Undefined,
    hash_with_protos,
    import_config,
)
from artheia.manifest.datatypes import (
    BazelTarget,
    Developer,
    DoipAddress,
    HostOSContentHash,
    HostOSVersion,
    Identity,
    ImageTag,
    IPAddress,
    MACAddress,
)

T = TypeVar("T")


# Identity of a Hardware Element
class HostCompute(Identity):
    pass


class HWRef(Generic[T]):
    def __init__(self, *args: Any) -> None:
        pass

    def __call__(self, hw_spec: HardwareSpecification) -> T:
        raise NotImplementedError


#
# Hardware Specification
#


class HardwareCompRoot:
    def __hash__(self) -> int:
        return hash(id(self))


class CPUArchitecture(Enum):
    x86_64 = "x86_64"
    aarch64 = "aarch64"


@dataclass
class HardwareSpecification:
    """
    Specifies a hardware configuration for a vehicle configuration.

    This includes the compute hardware, wiring, and other peripherals.
    The expectation is that items within this configuration can only be changed
    by physically modifying the vehicle.
    """

    hardware_elements: set["HardwareElementInstance"]
    connections: set["HardwareConnection"]

    def hardware_element_by_name(self, name: HostCompute) -> "HardwareElementInstance":
        for ce in self.hardware_elements:
            if ce.name == name:
                return ce
        raise KeyError(
            f'Compute element "{name}" not found in list: {[ce.name for ce in self.hardware_elements]}'
        )


#
# Hardware Ports
#


@dataclass
class HardwarePort:
    """
    Base type for defining a port on a hardware element and the information needed to connect to it.

    This information should include the physical connection information, such as the pinout.
    It should also include information about the capabilities of the port necessary to determine compatibility.
    """

    name: Identity

    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class CANPort(HardwarePort):
    # Controller Area Network Flexible Data-Rate (CAN FD) capable
    fd_capable: bool

    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class LINPort(HardwarePort):
    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class EthernetPort(HardwarePort):
    def __hash__(self) -> int:
        return super().__hash__()


#
# Hardware Elements
#


@dataclass
class CPUHardwareSpecification:
    """
    The hardware specification of a CPU as needed to compile code for it.

    This information may be used by Bazel to determine the correct target platform.
    """

    architecture: CPUArchitecture


@dataclass
class HardwareElement(CPUHardwareSpecification):
    """
    The physical hardware element that is part of the vehicle.
    """

    ports: set[HardwarePort]


@dataclass
class HardwareElementInstance(HardwareCompRoot):
    name: HostCompute
    element: HardwareElement
    # Metadata about the instance

    def __hash__(self) -> int:
        return super().__hash__()


#
# Hardware Connections
#


@dataclass
class HardwareConnection:
    def is_valid(self) -> bool:
        raise NotImplementedError

    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class CANNetwork(HardwareConnection):
    connects: set[CANPort | HWRef[CANPort]]

    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class LINNetwork(HardwareConnection):
    master: LINPort
    connects: set[LINPort]

    def __hash__(self) -> int:
        return super().__hash__()


@dataclass
class EthernetLink(HardwareConnection):
    connects: tuple[EthernetPort, EthernetPort]

    def __hash__(self) -> int:
        return super().__hash__()


#
# Software Specification
#
# Note that this section follows a convention of using a "Layer" subclass and
# a plain dataclass of the same name with an "_" prepended.  The dataclass is
# used to provide access to only concrete defined fields, while the Layer
# supports more complex datatypes to support layering and deferring values.
#


class SoftwareCompRoot:
    def __hash__(self) -> int:
        return hash(id(self))


class GatewayConfig(ABC, SoftwareCompRoot):
    pass


@dataclass(frozen=True)
class _Hive(SoftwareCompRoot):
    cloud_provider_region: str  # The cloud provider region that the cluster uses (e.g. "us-west-2", "eu-north-1")
    cloud_namespace: (
        str  # Applied namespace of the cluster (e.g. "prod", "staging", "dx")
    )
    grpc_config: _HiveGRPCConfig
    mqtt_config: _HiveMQTTConfig
    offboard_tools_config: _OffboardToolsConfig
    google_cloud_config: _GoogleCloudConfig
    data_logging_config: _DataLoggingConfig


@dataclass()
class Hive(SoftwareCompRoot, Layer[_Hive]):
    cloud_provider_region: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    cloud_namespace: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    grpc_config: Union[
        HiveGRPCConfig,  # Base type
        Defer[VehicleCtx, HiveGRPCConfig],
        Undefined[HiveGRPCConfig | Defer[VehicleCtx, HiveGRPCConfig]],
    ] = Undefined()

    mqtt_config: Union[
        HiveMQTTConfig,  # Base type
        Defer[VehicleCtx, HiveMQTTConfig],
        Undefined[HiveMQTTConfig | Defer[VehicleCtx, HiveMQTTConfig]],
    ] = Undefined()

    offboard_tools_config: Union[
        OffboardToolsConfig,  # Base type
        Defer[VehicleCtx, OffboardToolsConfig],
        Undefined[OffboardToolsConfig | Defer[VehicleCtx, OffboardToolsConfig]],
    ] = Undefined()

    google_cloud_config: Union[
        GoogleCloudConfig,  # Base type
        Defer[VehicleCtx, GoogleCloudConfig],
        Undefined[GoogleCloudConfig | Defer[VehicleCtx, GoogleCloudConfig]],
    ] = Undefined()

    data_logging_config: Union[
        DataLoggingConfig,  # Base type
        Defer[VehicleCtx, DataLoggingConfig],
        Undefined[DataLoggingConfig | Defer[VehicleCtx, DataLoggingConfig]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_Hive]:
        return _Hive


@dataclass(frozen=True)
class _S3Config(SoftwareCompRoot):
    aws_creds: _AWSSecretsItem


@dataclass()
class S3Config(SoftwareCompRoot, Layer[_S3Config]):
    aws_creds: Union[
        AWSSecretsItem,  # Base type
        Defer[VehicleCtx, AWSSecretsItem],
        Undefined[AWSSecretsItem | Defer[VehicleCtx, AWSSecretsItem]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_S3Config]:
        return _S3Config


@dataclass(frozen=True)
class _BuildFlag:
    name: str
    value: str


@dataclass
class BuildFlag(SoftwareCompRoot, Identifiable[_BuildFlag]):
    name: str
    value: Union[
        str,
        Defer[ComputeCtx, str],
    ]

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_BuildFlag]:
        return _BuildFlag

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _AWSSecretsItem:
    item_id: str


@dataclass()
class AWSSecretsItem(SoftwareCompRoot, Layer[_AWSSecretsItem]):
    """ARN of the AWS Secrets Manager item."""

    item_id: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_AWSSecretsItem]:
        return _AWSSecretsItem


@dataclass(frozen=True)
class _SSHAWSTunnelConfig(SoftwareCompRoot):
    server_user: str
    server_hostname: str
    ssh_identity_key: _AWSSecretsItem
    remote_user: Optional[str] = None
    remote_password: Optional[str] = None
    remote_login: Optional[_AWSSecretsItem] = None


@dataclass()
class SSHAWSTunnelConfig(SoftwareCompRoot, Layer[_SSHAWSTunnelConfig]):
    remote_user: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    remote_password: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    remote_login: Union[
        Optional[AWSSecretsItem],  # Base type
        Defer[VehicleCtx, Optional[AWSSecretsItem]],
        Undefined[Optional[AWSSecretsItem]],
        Undefined[Defer[VehicleCtx, Optional[AWSSecretsItem]]],
    ] = Default[Optional[AWSSecretsItem]](None)

    server_user: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    server_hostname: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    ssh_identity_key: Union[
        AWSSecretsItem,  # Base type
        Defer[VehicleCtx, AWSSecretsItem],
        Undefined[AWSSecretsItem | Defer[VehicleCtx, AWSSecretsItem]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_SSHAWSTunnelConfig]:
        return _SSHAWSTunnelConfig


class VisualizationType(Enum):
    ANDROID_AUTO = "ANDROID_AUTO"
    CARPLAY_CLASSIC = "CARPLAY_CLASSIC"
    CARPLAY_PLUS_PLUS = "CARPLAY_PLUS_PLUS"
    UNREAL_CLUSTER = "UNREAL_CLUSTER"


@dataclass(frozen=True)
class _Infotainment(SoftwareCompRoot):
    visualizations: set[VisualizationType]


@dataclass()
class Infotainment(SoftwareCompRoot, Layer[_Infotainment]):
    visualizations: Union[
        set[VisualizationType],
        set[SimpleSetTransformTypes],
        Undefined[set[VisualizationType]],
    ] = Undefined[set[VisualizationType]]()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_Infotainment]:
        return _Infotainment


class McuModel(Enum):
    s32k344 = "MCU_MODEL_S32K344"
    s32k388 = "MCU_MODEL_S32K388"
    s32z280 = "MCU_MODEL_S32Z280"
    rtk_switch = "MCU_MODEL_RTK_SWITCH"


class EmbeddedBoardModel(Enum):
    palm = "EMBEDDED_BOARD_MODEL_PALM"
    maple = "EMBEDDED_BOARD_MODEL_MAPLE"


@dataclass(frozen=True)
class _PeripheralEcu:
    name: HostCompute
    doip_target_id: Optional[DoipAddress]
    build_flags: frozenset[_BuildFlag]
    target_or_tag: Optional[Union[BazelTarget, ImageTag]]


@dataclass
class PeripheralEcu(SoftwareCompRoot, Identifiable[_PeripheralEcu]):
    name: HostCompute
    doip_target_id: Optional[DoipAddress] = None
    target_or_tag: str = ""
    build_flags: Union[
        set[BuildFlag],
        set[SetTransformTypes],
        Undefined[set[BuildFlag]],
    ] = Default[set[BuildFlag]](cast(set[BuildFlag], frozenset()))

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_PeripheralEcu]:
        return _PeripheralEcu

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _EmbeddedComputeElement(_PeripheralEcu):
    mcu_model: McuModel
    ip_address: IPAddress  # IP address of the zonal compute
    port: int
    mac_address: MACAddress  # MAC address of the zonal compute
    peripheral_ecus: frozenset[
        _PeripheralEcu
    ]  # ECUs connected downstream of this board


@dataclass(frozen=True)
class _EmbeddedComputeGroup:
    name: str
    board_model: EmbeddedBoardModel
    ecus: frozenset[_EmbeddedComputeElement]


@dataclass()
class EmbeddedComputeElement(PeripheralEcu, Identifiable[_EmbeddedComputeElement]):
    mcu_model: Union[
        McuModel,  # Base type
        Defer[None, McuModel],
        Undefined[McuModel],
        Undefined[Defer[None, McuModel]],
    ] = Undefined[McuModel]()

    ip_address: Union[
        IPAddress,  # Base type
        Defer[None, IPAddress],
        Undefined[IPAddress],
        Undefined[Defer[None, IPAddress]],
    ] = Undefined[IPAddress]()

    port: Union[
        int,  # Base type
        Defer[None, int],
        Undefined[int],
        Undefined[Defer[None, int]],
    ] = Undefined[int]()

    mac_address: Union[
        MACAddress,  # Base type
        Defer[None, MACAddress],
        Undefined[MACAddress],
        Undefined[Defer[None, MACAddress]],
    ] = Undefined[MACAddress]()

    peripheral_ecus: Union[
        set[PeripheralEcu],
        set[SetTransformTypes],
        Undefined[set[PeripheralEcu]],
    ] = Default[set[PeripheralEcu]](cast(set[PeripheralEcu], frozenset()))

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_EmbeddedComputeElement]:
        return _EmbeddedComputeElement

    @property
    def _set_identify(self) -> int:
        x: str = self.name
        return hash(x)


@dataclass()
class EmbeddedComputeGroup(SoftwareCompRoot, Identifiable[_EmbeddedComputeGroup]):
    name: str
    board_model: Union[
        EmbeddedBoardModel,
        Defer[None, EmbeddedBoardModel],
        Undefined[EmbeddedBoardModel],
        Undefined[Defer[None, EmbeddedBoardModel]],
    ] = Undefined[EmbeddedBoardModel]()

    ecus: Union[
        set[EmbeddedComputeElement],
        set[SetTransformTypes],
        Undefined[set[EmbeddedComputeElement]],
    ] = Default[set[EmbeddedComputeElement]](
        cast(set[EmbeddedComputeElement], frozenset())
    )

    @property
    def _resolver(self) -> type[_EmbeddedComputeGroup]:
        return _EmbeddedComputeGroup

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _SsmConfig(SoftwareCompRoot):
    """
    The SSM config is used to connect to the vehicle over the AWS SSM service.
    See the AWS SSM documentation for more information:
    - https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-hybrid-multicloud.html
    - https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-enable-ssh-connections.html
    """

    ssm_hostname: (
        str  # The hostname for the SSM instance. For example: mi-0856f265a7d467a76
    )
    aws_region: str  # The AWS region where the SSM instance is registered. For example: us-west-2


@dataclass()
class SsmConfig(SoftwareCompRoot, Layer[_SsmConfig]):
    ssm_hostname: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    aws_region: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_SsmConfig]:
        return _SsmConfig


@dataclass(frozen=True)
class _GatewayRouterConfig(GatewayConfig):
    ip_address: Union[IPAddress, _AWSSecretsItem]
    port: Optional[int]
    username: Optional[str] = None
    password: Optional[str] = None
    login: Optional[_AWSSecretsItem] = None
    vpn_ip: Optional[IPAddress] = None


@dataclass()
class GatewayRouterConfig(GatewayConfig, Layer[_GatewayRouterConfig]):
    ip_address: Union[
        IPAddress,  # Direct IP address
        AWSSecretsItem,  # AWS secrets reference
        Defer[VehicleCtx, Union[IPAddress, AWSSecretsItem]],
        Undefined[
            Union[
                IPAddress,
                AWSSecretsItem,
                Defer[VehicleCtx, Union[IPAddress, AWSSecretsItem]],
            ]
        ],
    ] = Undefined()

    username: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str] | Defer[VehicleCtx, Optional[str]]],
    ] = Default(None)

    password: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str] | Defer[VehicleCtx, Optional[str]]],
    ] = Default(None)

    port: Union[
        Optional[int],  # Base type
        Defer[VehicleCtx, Optional[int]],
        Undefined[int | Defer[VehicleCtx, Optional[int]]],
    ] = Default(23)

    login: Union[
        Optional[AWSSecretsItem],  # Base type
        Defer[VehicleCtx, Optional[AWSSecretsItem]],
        Undefined[Optional[AWSSecretsItem]],
        Undefined[Defer[VehicleCtx, Optional[AWSSecretsItem]]],
    ] = Default[Optional[AWSSecretsItem]](None)

    """
    IP address of the vehicle on the VPN.

    This is used to connect to the vehicle over the VPN.
    """
    vpn_ip: Union[
        Optional[IPAddress],  # Base type
        Defer[VehicleCtx, Optional[IPAddress]],
        Undefined[Optional[IPAddress] | Defer[VehicleCtx, Optional[IPAddress]]],
    ] = Default(None)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_GatewayRouterConfig]:
        return _GatewayRouterConfig


@dataclass(frozen=True)
class _GatewayGroundStationConfig(GatewayConfig):
    ip_address: Union[IPAddress, _AWSSecretsItem]
    rig_interface: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    identity_file: Optional[PosixPath] = None
    login: Optional[_AWSSecretsItem] = None
    vpn_ip: Optional[IPAddress] = None
    ssm_config: Optional[_SsmConfig] = None

    def __post_init__(self) -> None:
        if self.password is None and self.identity_file is None and self.login is None:
            raise ValueError("'password' or 'identity_file' or 'login' is required")

        if self.identity_file is not None and not self.identity_file.exists():
            raise ValueError(f"identity_file {self.identity_file} does not exist")


@dataclass()
class GatewayGroundStationConfig(GatewayConfig, Layer[_GatewayGroundStationConfig]):
    ip_address: Union[
        IPAddress,  # Direct IP address
        AWSSecretsItem,  # AWS secrets reference
        Defer[VehicleCtx, Union[IPAddress, AWSSecretsItem]],
        Undefined[
            Union[
                IPAddress,
                AWSSecretsItem,
                Defer[VehicleCtx, Union[IPAddress, AWSSecretsItem]],
            ]
        ],
    ] = Undefined()

    username: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str] | Defer[VehicleCtx, Optional[str]]],
    ] = Default(None)

    """
    At least one of password or identity_file or login need to be specified.

    All can be specified, based on the SSH library, the private key will be
    tried first.
    """
    password: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str] | Defer[VehicleCtx, Optional[str]]],
    ] = Default(None)

    """
    Absolute path to the SSH private key file on the computer initiating the connection.
    """
    identity_file: Union[
        Optional[PosixPath],
        Defer[VehicleCtx, Optional[PosixPath]],
        Undefined[Optional[PosixPath] | Defer[VehicleCtx, Optional[PosixPath]]],
    ] = Default(None)

    # nic connected to rig lan
    rig_interface: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str] | Defer[VehicleCtx, Optional[str]]],
    ] = Default(None)

    port: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Default(22)

    login: Union[
        Optional[AWSSecretsItem],  # Base type
        Defer[VehicleCtx, Optional[AWSSecretsItem]],
        Undefined[Optional[AWSSecretsItem]],
        Undefined[Defer[VehicleCtx, Optional[AWSSecretsItem]]],
    ] = Default[Optional[AWSSecretsItem]](None)

    ssm_config: Union[
        Optional[SsmConfig],  # Base type
        Defer[VehicleCtx, Optional[SsmConfig]],
        Undefined[Optional[SsmConfig] | Defer[VehicleCtx, Optional[SsmConfig]]],
    ] = Default(None)

    """
    IP address of the vehicle on the VPN.

    This is used to connect to the vehicle over the VPN.
    """
    vpn_ip: Union[
        Optional[IPAddress],  # Base type
        Defer[VehicleCtx, Optional[IPAddress]],
        Undefined[Optional[IPAddress] | Defer[VehicleCtx, Optional[IPAddress]]],
    ] = Default(None)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_GatewayGroundStationConfig]:
        return _GatewayGroundStationConfig


@dataclass(frozen=True)
class _ServicePorts:
    campaign_manager: int
    data_logging: int
    vehicle_ai: int
    vehicle_command: int
    vehicle_ota: int
    vehicle_query: int


@dataclass()
class ServicePorts(SoftwareCompRoot, Layer[_ServicePorts]):
    campaign_manager: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Undefined()

    data_logging: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Undefined()

    vehicle_ai: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Undefined()

    vehicle_command: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Undefined()

    vehicle_ota: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Undefined()

    vehicle_query: Union[
        int,  # Base type
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_ServicePorts]:
        return _ServicePorts


@dataclass(frozen=True)
class _HiveGRPCConfig(SoftwareCompRoot):
    endpoint: str
    aws_auth_token: Optional[_AWSSecretsItem]
    secure_channel: Optional[bool]
    service_ports: Optional[_ServicePorts]


@dataclass()
class HiveGRPCConfig(SoftwareCompRoot, Layer[_HiveGRPCConfig]):
    endpoint: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    aws_auth_token: Union[
        Optional[AWSSecretsItem],  # Base type
        Defer[VehicleCtx, Optional[AWSSecretsItem]],
        Undefined[Optional[AWSSecretsItem]],
        Undefined[Defer[VehicleCtx, Optional[AWSSecretsItem]]],
    ] = Default[Optional[AWSSecretsItem]](None)

    secure_channel: Union[
        Optional[bool],  # Base type
        Optional[Defer[VehicleCtx, bool]],
        Undefined[Optional[bool]],
        Undefined[Defer[VehicleCtx, Optional[bool]]],
    ] = Default[Optional[bool]](None)

    service_ports: Union[
        Optional[ServicePorts],  # Base type
        Optional[Defer[VehicleCtx, ServicePorts]],
        Undefined[Optional[ServicePorts]],
        Undefined[Defer[VehicleCtx, Optional[ServicePorts]]],
    ] = Default[Optional[ServicePorts]](None)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_HiveGRPCConfig]:
        return _HiveGRPCConfig


@dataclass(frozen=True)
class _HiveMQTTConfig(SoftwareCompRoot):
    mosquitto_broker_host_domain_name: str
    aws_password: _AWSSecretsItem


@dataclass()
class HiveMQTTConfig(SoftwareCompRoot, Layer[_HiveMQTTConfig]):
    mosquitto_broker_host_domain_name: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    aws_password: Union[
        AWSSecretsItem,  # Base type
        Defer[VehicleCtx, AWSSecretsItem],
        Undefined[AWSSecretsItem | Defer[VehicleCtx, AWSSecretsItem]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_HiveMQTTConfig]:
        return _HiveMQTTConfig


@dataclass(frozen=True)
class _GoogleCloudConfig(SoftwareCompRoot):
    credential_arn: _AWSSecretsItem


@dataclass()
class GoogleCloudConfig(SoftwareCompRoot, Layer[_GoogleCloudConfig]):
    credential_arn: Union[
        AWSSecretsItem,  # Base type
        Defer[VehicleCtx, AWSSecretsItem],
        Undefined[AWSSecretsItem | Defer[VehicleCtx, AWSSecretsItem]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_GoogleCloudConfig]:
        return _GoogleCloudConfig


@dataclass(frozen=True)
class _DataLoggingConfig(SoftwareCompRoot):
    """
    This class is used to configure the Generic Data Logger MPK.
    """

    # todo: use Hive cloud_provider_region instead.
    s3_region: str  # AWS S3 region to upload data logging MCAP files to.
    s3_bucket: str  # AWS S3 bucket to upload data logging MCAP files to.


@dataclass()
class DataLoggingConfig(SoftwareCompRoot, Layer[_DataLoggingConfig]):
    s3_region: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    s3_bucket: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_DataLoggingConfig]:
        return _DataLoggingConfig


@dataclass(frozen=True)
class _OffboardToolsConfig(SoftwareCompRoot):
    components_bucket: str


@dataclass
class OffboardToolsConfig(SoftwareCompRoot, Layer[_OffboardToolsConfig]):
    components_bucket: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_OffboardToolsConfig]:
        return _OffboardToolsConfig


#
# Compute Elements
#


@dataclass(frozen=True)
class _ComputeElementBuildEnvironment(SoftwareCompRoot):
    bazel_platform: BazelTarget
    build_flags: frozenset[_BuildFlag]


@dataclass()
class ComputeElementBuildEnvironment(
    SoftwareCompRoot, Layer[_ComputeElementBuildEnvironment]
):
    bazel_platform: Union[
        BazelTarget,  # Base type
        Defer[ComputeCtx, BazelTarget],
        Undefined[BazelTarget],
        Undefined[Defer[ComputeCtx, BazelTarget]],
    ] = Undefined[BazelTarget]()

    build_flags: Union[
        set[BuildFlag],
        set[SetTransformTypes],
        Undefined[set[BuildFlag]],
    ] = Default[set[BuildFlag]](cast(set[BuildFlag], frozenset()))

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_ComputeElementBuildEnvironment]:
        return _ComputeElementBuildEnvironment


@dataclass(frozen=True)
class _VirtualMachine(SoftwareCompRoot):
    name: str
    ip_address: IPAddress
    interface_name: str
    build: _ComputeElementBuildEnvironment


@dataclass()
class VirtualMachine(SoftwareCompRoot, Identifiable[Any]):
    name: str

    ip_address: Union[
        IPAddress,  # Base type
        Defer[None, IPAddress],
        Undefined[IPAddress],
        Undefined[Defer[ComputeCtx, IPAddress]],
    ] = Undefined[IPAddress]()

    interface_name: Union[
        str,  # Base type
        Defer[None, str],
        Undefined[str],
        Undefined[Defer[ComputeCtx, str]],
    ] = Undefined[str]()

    build: Union[
        ComputeElementBuildEnvironment,  # Base type
        Defer[None, ComputeElementBuildEnvironment],
        Undefined[ComputeElementBuildEnvironment],
        Undefined[Defer[ComputeCtx, ComputeElementBuildEnvironment]],
    ] = Undefined[ComputeElementBuildEnvironment]()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[Any]:
        return _VirtualMachine

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _ComputeElement(SoftwareCompRoot):
    name: HostCompute
    ip_address: Union[IPAddress, _AWSSecretsItem]
    interface_name: str

    build: _ComputeElementBuildEnvironment
    virtual_machines: frozenset[_VirtualMachine]


@dataclass()
class ComputeElement(SoftwareCompRoot, Identifiable[_ComputeElement]):
    name: HostCompute

    ip_address: Union[
        IPAddress,  # Base type
        Defer[VehicleCtx, IPAddress],
        AWSSecretsItem,  # Base type
        Defer[VehicleCtx, AWSSecretsItem],
        Undefined[IPAddress | AWSSecretsItem | Defer[VehicleCtx, IPAddress]],
    ] = Undefined()

    interface_name: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str | Defer[VehicleCtx, str]],
    ] = Undefined()

    build: Union[
        ComputeElementBuildEnvironment,  # Base type
        Defer[VehicleCtx, ComputeElementBuildEnvironment],
        Undefined[
            ComputeElementBuildEnvironment
            | Defer[VehicleCtx, ComputeElementBuildEnvironment]
        ],
    ] = Undefined()

    virtual_machines: Union[
        set[VirtualMachine],  # Base type
        set[SetTransformTypes],
        Undefined[set[VirtualMachine]],
    ] = Default[set[VirtualMachine]](cast(set[VirtualMachine], frozenset()))

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _set_identify(self) -> int:
        return hash(self.name)

    @property
    def _resolver(self) -> type[_ComputeElement]:
        return _ComputeElement


class BazelCompileConfiguration(Enum):
    """Bazel compile configuration"""

    NORMAL = "NORMAL"  # None
    DEBUG = "DEBUG"  # -c dbg
    OPTIMIZED = "OPTIMIZED"  # -c opt


@dataclass(frozen=True)
class _MosaicPackage(SoftwareCompRoot):
    bazel_build_target: BazelTarget
    compiler_configuration: BazelCompileConfiguration
    sends_dds_heartbeats: bool
    is_service_v2: bool
    owner: Developer


@dataclass()
class MosaicPackage(SoftwareCompRoot, Layer[_MosaicPackage]):
    bazel_build_target: Union[
        BazelTarget,  # Base type
        Defer[ComputeCtx, BazelTarget],
        Undefined[BazelTarget],
        Undefined[Defer[ComputeCtx, BazelTarget]],
    ] = Undefined[BazelTarget]()

    compiler_configuration: Union[
        BazelCompileConfiguration,  # Base type
        Defer[ComputeCtx, BazelCompileConfiguration],
        Undefined[BazelCompileConfiguration],
        Undefined[Defer[ComputeCtx, BazelCompileConfiguration]],
    ] = Default[BazelCompileConfiguration](BazelCompileConfiguration.OPTIMIZED)

    # owner is the DRI (directly responsible indivisual) for this MPK.
    owner: Union[
        Developer,  # Base type
        Defer[None, Developer],
        Undefined[Developer],
        Undefined[Defer[None, Developer]],
    ] = Default[Developer](Developer(""))

    # Set default here to true since its what we expected when this was added.
    sends_dds_heartbeats: Union[
        bool,  # Base type,
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Default[bool](True)

    # Set default to false since most mpks are not services v2.
    is_service_v2: Union[
        bool,  # Base type,
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Default[bool](False)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_MosaicPackage]:
        return _MosaicPackage


@dataclass(frozen=True)
class _ConfigurationArtifact(SoftwareCompRoot):
    name: str
    onboard_artifact_path: str


@dataclass()
class ConfigurationArtifact(SoftwareCompRoot, Identifiable[Any]):
    name: str
    onboard_artifact_path: str

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[Any]:
        return _ConfigurationArtifact

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _S3DirectoryArtifact(_ConfigurationArtifact):
    region_name: str
    bucket_name: str
    s3_prefix: str


@dataclass()
class S3DirectoryArtifact(ConfigurationArtifact, Identifiable[_S3DirectoryArtifact]):
    """
    Configuration artifact that represents an entire S3 directory.
    This artifact is pulled at build time, and creates
    a cloud directory mapping that gets resolved by the RigTransferManager.
    """

    region_name: str
    bucket_name: str
    s3_prefix: str  # Directory prefix from which all artifacts will be retrieved.

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _set_identify(self) -> int:
        return hash(self.s3_prefix)

    @property
    def _resolver(self) -> type[_S3DirectoryArtifact]:
        return _S3DirectoryArtifact


class SystemState(Enum):
    DEEP_SLEEP = "DEEP_SLEEP"
    SLEEP = "SLEEP"
    ACCESSORY_READY = "ACCESSORY_READY"
    ACCESSORY = "ACCESSORY"
    DRIVE_READY = "DRIVE_READY"
    DRIVE = "DRIVE"
    SLEEP_ACTIVE_OTA = "SLEEP_ACTIVE_OTA"
    SLEEP_ACTIVE_SENTRY = "SLEEP_ACTIVE_SENTRY"
    SLEEP_ACTIVE_REMOTE = "SLEEP_ACTIVE_REMOTE"


# tag::syscomp_dataclass_example[]
@dataclass(frozen=True)
class _MosaicPackageInstance(SoftwareCompRoot):
    name: str
    mpk: _MosaicPackage
    build_flags: frozenset[_BuildFlag]
    suspended_states: frozenset[SystemState]
    exclude_from_ota: (
        bool  # This is explicitly for legacy support and should be removed
    )
    is_orchestrator: bool
    custom_configuration: Optional[IsProtobuf]
    configuration_artifacts: frozenset[_ConfigurationArtifact]

    system_service_name: str

    def __hash__(self) -> int:
        return hash_with_protos(self, ["custom_configuration"])  # type: ignore[arg-type]


@dataclass()
class MosaicPackageInstance(SoftwareCompRoot, Identifiable[_MosaicPackageInstance]):
    name: str

    mpk: Union[
        MosaicPackage,  # Base type
        Defer[ComputeCtx, MosaicPackage],
        Undefined[MosaicPackage],
        Undefined[Defer[ComputeCtx, MosaicPackage]],
    ] = Undefined[MosaicPackage]()

    build_flags: Union[
        set[BuildFlag],
        set[SetTransformTypes],
        Undefined[set[BuildFlag]],
    ] = Default[set[BuildFlag]](cast(set[BuildFlag], frozenset()))

    suspended_states: Union[
        set[SystemState],  # Base Type
        set[SetTransformTypes],
        Undefined[set[SystemState]],
    ] = Default[set[SystemState]](cast(set[SystemState], frozenset()))

    exclude_from_ota: Union[  # This is explicitly for legacy support and should be removed
        bool,  # Base type
        Defer[None, bool],
        Undefined[bool],
        Undefined[Defer[None, bool]],
    ] = Default[bool](False)

    is_orchestrator: Union[
        bool,  # Base type
        Defer[None, bool],
        Undefined[bool],
        Undefined[Defer[None, bool]],
    ] = Default[bool](False)

    custom_configuration: Union[
        Optional[IsProtobuf],  # Base type
        Defer[ComputeCtx, Optional[IsProtobuf]],
        Undefined[Optional[IsProtobuf]],
        Undefined[Defer[ComputeCtx, Optional[IsProtobuf]]],
    ] = Default[Optional[IsProtobuf]](None)

    configuration_artifacts: Union[
        set[ConfigurationArtifact],
        Undefined[set[ConfigurationArtifact]],
    ] = Default[set[ConfigurationArtifact]](
        cast(set[ConfigurationArtifact], frozenset())
    )

    system_service_name: Union[
        str,  # Base type
        Defer[ComputeCtx, str],
        Undefined[str],
        Undefined[Defer[ComputeCtx, str]],
    ] = Default[str]("")

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_MosaicPackageInstance]:
        return _MosaicPackageInstance

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


# end::syscomp_dataclass_example[]


@dataclass(frozen=True)
class _EnvironmentVariable(SoftwareCompRoot):
    """
    Represents an environment variable to be set in a shell session.

    Attributes:
        name (str): The name of the environment variable (e.g. 'PATH').
        value (str): The value to assign to the variable.
        append (bool): If False, the variable will be set as export name=value.
                       If True, the variable will be set as export name=value:${name},
                       effectively appending to the existing value. This is mostly
                       useful for PATH-like variables, e.g. export PATH=$PATH:/opt/bin.
                       If False, the variable will be set to 'value' only.
    """

    name: str
    value: str
    append: bool


@dataclass()
class EnvironmentVariable(SoftwareCompRoot, Identifiable[_EnvironmentVariable]):
    name: str

    value: Union[
        str,  # Base type
        Defer[ComputeCtx, str],
        Undefined[str],
        Undefined[Defer[ComputeCtx, str]],
    ] = Undefined[str]()

    append: Union[
        bool,  # Base type
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Undefined[bool]()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_EnvironmentVariable]:
        return _EnvironmentVariable

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _SupportingFile(SoftwareCompRoot):
    name: str
    runfiles_path: PosixPath


@dataclass()
class SupportingFile(SoftwareCompRoot, Identifiable[_SupportingFile]):
    name: str

    runfiles_path: Union[
        PosixPath,  # Base type
        Defer[ComputeCtx, PosixPath],
        Undefined[PosixPath],
        Undefined[Defer[ComputeCtx, PosixPath]],
    ] = Undefined[PosixPath]()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_SupportingFile]:
        return _SupportingFile

    @property
    def _set_identify(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class _MosaicComputeElement(_ComputeElement):
    mosaic_packages: frozenset[_MosaicPackageInstance]
    host_os_version: Optional[HostOSVersion]
    host_os_content_hash: Optional[HostOSContentHash]
    ssh_port: int
    supports_rsync: bool
    mount_dir: PosixPath
    mosaic_package_root: PosixPath
    persistent_storage_path: Optional[PosixPath]
    ota_download_path: Optional[PosixPath]
    user_lock_path: Optional[PosixPath]
    ci_lock_path: Optional[PosixPath]
    ssh_env_vars: frozenset[_EnvironmentVariable]
    supporting_files: frozenset[_SupportingFile]
    username: Optional[str] = None
    password: Optional[str] = None
    login: Optional[_AWSSecretsItem] = None
    qnx_display_config: Optional[str] = None

    def __post_init__(self) -> None:
        if self.login is not None:
            if self.username is not None or self.password is not None:
                raise ValueError("Provide 'login' or both 'username' and 'password'")
        else:
            if self.username is None or self.password is None:
                raise ValueError(
                    "Either 'login' or 'username' and 'password' must be set."
                )


@dataclass()
class MosaicComputeElement(ComputeElement, Identifiable[_MosaicComputeElement]):
    mosaic_packages: Union[
        set[MosaicPackageInstance],  # Base type
        set[SetTransformTypes],
        Undefined[set[MosaicPackageInstance]],
    ] = Default[set[MosaicPackageInstance]](
        cast(set[MosaicPackageInstance], frozenset())
    )

    username: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    password: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    host_os_version: Union[
        Optional[HostOSVersion],  # Base type
        Defer[VehicleCtx, Optional[HostOSVersion]],
        Undefined[Optional[HostOSVersion]],
    ] = Default[Optional[HostOSVersion]](None)

    host_os_content_hash: Union[
        Optional[HostOSContentHash],  # Base type
        Defer[VehicleCtx, Optional[HostOSContentHash]],
        Undefined[Optional[HostOSContentHash]],
    ] = Default[Optional[HostOSContentHash]](None)

    ssh_port: Union[
        int,
        Defer[VehicleCtx, int],
        Undefined[int | Defer[VehicleCtx, int]],
    ] = Default(22)

    mount_dir: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath | Defer[VehicleCtx, PosixPath]],
    ] = Undefined()

    mosaic_package_root: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath | Defer[VehicleCtx, PosixPath]],
    ] = Undefined()

    persistent_storage_path: Union[
        Optional[PosixPath],  # Base type
        Defer[VehicleCtx, Optional[PosixPath]],
        Undefined[Optional[PosixPath]],
        Undefined[Defer[VehicleCtx, Optional[PosixPath]]],
    ] = Default[Optional[PosixPath]](None)

    ota_download_path: Union[
        Optional[PosixPath],  # Base type
        Defer[VehicleCtx, Optional[PosixPath]],
        Undefined[Optional[PosixPath]],
        Undefined[Defer[VehicleCtx, Optional[PosixPath]]],
    ] = Default[Optional[PosixPath]](None)

    user_lock_path: Union[
        Optional[PosixPath],  # Base type
        Defer[VehicleCtx, Optional[PosixPath]],
        Undefined[Optional[PosixPath]],
        Undefined[Defer[VehicleCtx, Optional[PosixPath]]],
    ] = Default[Optional[PosixPath]](None)

    ci_lock_path: Union[
        Optional[PosixPath],  # Base type
        Defer[VehicleCtx, Optional[PosixPath]],
        Undefined[Optional[PosixPath]],
        Undefined[Defer[VehicleCtx, Optional[PosixPath]]],
    ] = Default[Optional[PosixPath]](None)

    ssh_env_vars: Union[
        set[EnvironmentVariable],  # Base type
        set[SetTransformTypes],
        Undefined[set[EnvironmentVariable]],
    ] = Default[set[EnvironmentVariable]](cast(set[EnvironmentVariable], frozenset()))

    supports_rsync: Union[
        bool,  # Base type
        Defer[None, bool],
        Undefined[bool],
        Undefined[Defer[None, bool]],
    ] = Default[bool](False)

    supporting_files: Union[
        set[SupportingFile],  # Base type
        set[SetTransformTypes],
        Undefined[set[SupportingFile]],
    ] = Undefined[set[SupportingFile]]()

    login: Union[
        Optional[AWSSecretsItem],  # Base type
        Defer[VehicleCtx, Optional[AWSSecretsItem]],
        Undefined[Optional[AWSSecretsItem]],
        Undefined[Defer[VehicleCtx, Optional[AWSSecretsItem]]],
    ] = Default[Optional[AWSSecretsItem]](None)

    qnx_display_config: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_MosaicComputeElement]:
        return _MosaicComputeElement


@dataclass(frozen=True)
class _ADB(SoftwareCompRoot):
    server_ip: Optional[IPAddress]
    local_network: bool
    served_on_gateway: Optional[bool]
    username: Optional[str] = None
    password: Optional[str] = None
    login: Optional[_AWSSecretsItem] = None


@dataclass()
class ADB(SoftwareCompRoot, Layer[_ADB]):
    server_ip: Union[
        Optional[IPAddress],  # Base type
        Defer[ComputeCtx, Optional[IPAddress]],
        Undefined[Optional[IPAddress]],
        Undefined[Defer[ComputeCtx, Optional[IPAddress]]],
    ] = Default[Optional[IPAddress]](None)

    username: Union[
        Optional[str],  # Base type
        Defer[ComputeCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[ComputeCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    password: Union[
        Optional[str],  # Base type
        Defer[ComputeCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[ComputeCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    local_network: Union[
        bool,  # Base type
        Defer[None, bool],
        Undefined[bool],
        Undefined[Defer[None, bool]],
    ] = Undefined[bool]()

    served_on_gateway: Union[
        Optional[bool],  # Base type
        Defer[ComputeCtx, Optional[bool]],
        Undefined[Optional[bool]],
        Undefined[Defer[ComputeCtx, Optional[bool]]],
    ] = Default[Optional[bool]](False)

    login: Union[
        Optional[AWSSecretsItem],  # Base type
        Defer[VehicleCtx, Optional[AWSSecretsItem]],
        Undefined[Optional[AWSSecretsItem]],
        Undefined[Defer[VehicleCtx, Optional[AWSSecretsItem]]],
    ] = Default[Optional[AWSSecretsItem]](None)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_ADB]:
        return _ADB


class AAOSDisplayType(Enum):
    DISPLAY_TYPE_UNKNOWN = "UNKNOWN"
    DISPLAY_TYPE_MAIN = "MAIN"
    DISPLAY_TYPE_INSTRUMENT_CLUSTER = "INSTRUMENT_CLUSTER"
    DISPLAY_TYPE_HUD = "HUD"
    DISPLAY_TYPE_INPUT = "INPUT"
    DISPLAY_TYPE_AUXILIARY = "AUXILIARY"


AAOSBuildType = rig_pb2.AAOSBuildType


class AAOSOccupantZone(Enum):
    """
    Reference: device/applied/boards/msmnile_gvmq/overlay/packages/services/Car/service/res/values/shared_configs.xml
    """

    DRIVER = 0
    FRONT_PASSENGER_RIGHT = 1
    REAR_PASSENGER_LEFT = 2
    REAR_PASSENGER_RIGHT = 3
    FRONT_PASSENGER_CENTER = 4


@dataclass(frozen=True)
class _AAOSDisplayConfig(SoftwareCompRoot):
    """
    This class is used to express the display configuration of an AAOS device.

    Reference: https://cs.android.com/android/platform/superproject/+/android14-qpr3-release:device/google/cuttlefish/shared/auto/rro_overlay/CarServiceOverlay/res/values/config.xml;l=82;drc=52a18348f3088b406a9f8845e2b97420c6e5f6d3

    Here's what an extected output xml should look like

    display_configs.xml

    <item>displayPort=129,displayType=MAIN,occupantZoneId=0</item>
    <item>displayPort=131,displayType=INSTRUMENT_CLUSTER,occupantZoneId=0</item>
    <item>displayPort=2,displayType=MAIN,occupantZoneId=1</item>

    display_settings.xml

    <display name="port:129" shouldShowSystemDecors="true" shouldShowIme="true" />
    <display name="port:131" shouldShowSystemDecors="false" shouldShowIme="false" />
    <display name="port:2" shouldShowSystemDecors="true" shouldShowIme="true" />

    input-port-associations.xml

    <port display="129" input="usb-xhci-hcd.1.auto-4/input0" />
    <port display="2" input="usb-xhci-hcd.1.auto-3/input0" />
    """

    display_port: int  # the display port on the AAOS device
    display_type: AAOSDisplayType  # the type of display
    occupant_zone: AAOSOccupantZone  # the occupant zone of this display
    should_show_system_decors: bool  # should show system decors (aka system bars)
    should_show_ime: (
        bool  # should show ime (input method editor, aka software keyboard)
    )
    input_device_id: Optional[str]  # the input device id


@dataclass()
class AAOSDisplayConfig(SoftwareCompRoot, Identifiable[_AAOSDisplayConfig]):
    display_port: Union[
        int,  # Base type
        Defer[ComputeCtx, int],
        Undefined[int],
        Undefined[Defer[ComputeCtx, int]],
    ] = Undefined[int]()

    display_type: Union[
        AAOSDisplayType,  # Base type
        Defer[ComputeCtx, AAOSDisplayType],
        Undefined[AAOSDisplayType],
        Undefined[Defer[ComputeCtx, AAOSDisplayType]],
    ] = Undefined[AAOSDisplayType]()

    occupant_zone: Union[
        AAOSOccupantZone,  # Base type
        Defer[ComputeCtx, AAOSOccupantZone],
        Undefined[AAOSOccupantZone],
        Undefined[Defer[ComputeCtx, AAOSOccupantZone]],
    ] = Undefined[AAOSOccupantZone]()

    should_show_system_decors: Union[
        bool,  # Base type
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Undefined[bool]()

    should_show_ime: Union[
        bool,  # Base type
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Undefined[bool]()

    input_device_id: Union[
        Optional[str],  # Base type
        Defer[ComputeCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[ComputeCtx, Optional[str]]],
    ] = None

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _set_identify(self) -> int:
        return hash(self.display_port)

    @property
    def _resolver(self) -> type[_AAOSDisplayConfig]:
        return _AAOSDisplayConfig


@dataclass(frozen=True)
class _AAOSInfo(SoftwareCompRoot):
    """
    This class is used to express AAOS configuration such as the version of AAOS, display configuration, input mappings, etc.
    """

    version: int  # Android version, either 12 or 14
    build_type: rig_pb2.AAOSBuildType  # The build type of AAOS
    display_config: set[_AAOSDisplayConfig]  # Display configuration


@dataclass()
class AAOSInfo(SoftwareCompRoot, Layer[_AAOSInfo]):
    version: Union[
        int,  # Base type
        Defer[ComputeCtx, int],
        Undefined[int],
        Undefined[Defer[ComputeCtx, int]],
    ] = Undefined[int]()
    build_type: Union[
        rig_pb2.AAOSBuildType,  # Base type
        Defer[ComputeCtx, rig_pb2.AAOSBuildType],
        Undefined[rig_pb2.AAOSBuildType],
        Undefined[Defer[ComputeCtx, rig_pb2.AAOSBuildType]],
    ] = Undefined[rig_pb2.AAOSBuildType]()
    display_config: Union[
        set[AAOSDisplayConfig],  # Base type
        set[SetTransformTypes],
        Undefined[set[AAOSDisplayConfig]],
    ] = Default[set[AAOSDisplayConfig]](cast(set[AAOSDisplayConfig], []))

    @property
    def _resolver(self) -> type[_AAOSInfo]:
        return _AAOSInfo


@dataclass(frozen=True)
class _AndroidComputeElement(_VirtualMachine):
    adb: Optional[_ADB]
    mosaic_packages: set[_MosaicPackageInstance]
    host_os_version: Optional[HostOSVersion]
    host_os_content_hash: Optional[HostOSContentHash]
    aaos_info: Optional[_AAOSInfo]
    requires_unreal_assets: bool


@dataclass()
class AndroidComputeElement(VirtualMachine, Layer[_AndroidComputeElement]):
    adb: Union[
        Optional[ADB],  # Base type
        Defer[None, Optional[ADB]],
        Undefined[Optional[ADB]],
        Undefined[Defer[ComputeCtx, Optional[ADB]]],
    ] = Default[Optional[ADB]](None)

    mosaic_packages: Union[
        set[MosaicPackageInstance],  # Base type
        set[SetTransformTypes],
        Undefined[set[MosaicPackageInstance]],
    ] = Default[set[MosaicPackageInstance]](
        cast(set[MosaicPackageInstance], frozenset())
    )

    host_os_version: Union[
        Optional[HostOSVersion],  # Base type
        Defer[VehicleCtx, Optional[HostOSVersion]],
        Undefined[Optional[HostOSVersion]],
    ] = Default[Optional[HostOSVersion]](None)

    host_os_content_hash: Union[
        Optional[HostOSContentHash],  # Base type
        Defer[VehicleCtx, Optional[HostOSContentHash]],
        Undefined[Optional[HostOSContentHash]],
    ] = Default[Optional[HostOSContentHash]](None)

    aaos_info: Union[
        Optional[AAOSInfo],  # Base type
        Defer[None, Optional[AAOSInfo]],
        Undefined[Optional[AAOSInfo]],
        Undefined[Defer[ComputeCtx, Optional[AAOSInfo]]],
    ] = Default[Optional[AAOSInfo]](None)

    requires_unreal_assets: Union[
        bool,  # Base type,
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Default[bool](True)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_AndroidComputeElement]:
        return _AndroidComputeElement


@dataclass(frozen=True)
class _DebugLaptopComputeElement(SoftwareCompRoot):
    ip_address: IPAddress
    user: str
    password: str


@dataclass()
class DebugLaptopComputeElement(SoftwareCompRoot, Layer[_DebugLaptopComputeElement]):
    """Special case compute element for the debug laptop."""

    ip_address: Union[
        IPAddress,  # Base type
        Defer[VehicleCtx, IPAddress],
        Undefined[IPAddress],
        Undefined[Defer[VehicleCtx, IPAddress]],
    ] = Undefined[IPAddress]()

    user: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    password: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    @property
    def _resolver(self) -> type[_DebugLaptopComputeElement]:
        return _DebugLaptopComputeElement


@dataclass(frozen=True)
class _Container(SoftwareCompRoot, Identifiable):
    name: str
    network_driver: str


@dataclass()
class Container(SoftwareCompRoot, Identifiable[_Container]):
    name: str

    network_driver: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _set_identify(self) -> int:
        return hash(self.name)

    @property
    def _resolver(self) -> type[_Container]:
        return _Container


@dataclass(frozen=True)
class _QEMUContainer(SoftwareCompRoot, Identifiable):
    name: str
    docker_container_ip: IPAddress
    qemu_start_script: PosixPath


@dataclass()
class QEMUContainer(SoftwareCompRoot, Identifiable[_QEMUContainer]):
    name: str

    docker_container_ip: Union[
        IPAddress,  # Base type
        Defer[VehicleCtx, IPAddress],
        Undefined[IPAddress],
        Undefined[Defer[VehicleCtx, IPAddress]],
    ] = Undefined[IPAddress]()

    qemu_start_script: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath],
        Undefined[Defer[VehicleCtx, PosixPath]],
    ] = Undefined[PosixPath]()

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _set_identify(self) -> int:
        return hash(self.name)

    @property
    def _resolver(self) -> type[_QEMUContainer]:
        return _QEMUContainer


@dataclass(frozen=True)
class _DockerVirtualECUConfig(SoftwareCompRoot):
    """
    Configuration for Docker-based Virtual ECU implementations.
    This class holds configuration parameters for creating and managing
    Docker-based virtual ECUs (Electronic Control Units) that simulate hardware devices.
    It defines container specifications, networking information, and Docker Compose
    configuration settings required to launch and manage these virtualized components.

    Attributes:
        containers set[_Container]: Set of container configurations to be instantiated for the virtual ECU.

        rig_docker_name (str): Base name used for Docker containers that will run the virtual ECU.

        compose_command (str): Docker Compose command to use (e.g., "docker-compose", "docker compose").
        Can also be a wrapper script, e.g. "docker_compose_wrapper.sh". Is executed in the root of the repo,
        so when passing a script path it should be relative to the root of the repo.

        compose_yaml_path (PosixPath): Path to the Docker Compose YAML configuration file.

        docker_buildkit (bool): Whether to enable Docker Buildkit for improved build performance.

        env_file (PosixPath): Path to environment file used by Docker Compose.

        extra_compose_args (str): Optional additional arguments to pass to Docker Compose commands.
    """

    containers: set[_Container]

    rig_docker_name: str

    compose_command: str

    compose_yaml_path: PosixPath

    docker_buildkit: bool

    env_file: PosixPath

    extra_compose_args: Optional[str] = ""

    docker_network: Optional[str] = ""


@dataclass()
class DockerVirtualECUConfig(SoftwareCompRoot, Layer[_DockerVirtualECUConfig]):
    containers: Union[
        set[Container],  # Base type
        set[SetTransformTypes],
        Undefined[set[Container]],
    ] = Default[set[Container]](cast(set[Container], frozenset()))

    rig_docker_name: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    compose_command: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    compose_yaml_path: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath],
        Undefined[Defer[VehicleCtx, PosixPath]],
    ] = Undefined[PosixPath]()

    env_file: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath],
        Undefined[Defer[VehicleCtx, PosixPath]],
    ] = Undefined[PosixPath]()

    docker_buildkit: Union[
        bool,  # Base type
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Undefined[bool]()

    extra_compose_args: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    docker_network: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    @property
    def _resolver(self) -> type[_DockerVirtualECUConfig]:
        return _DockerVirtualECUConfig


@dataclass(frozen=True)
class _QEMUVirtualECUConfig(SoftwareCompRoot):
    """
    Configuration for QEMU-based Virtual ECU implementations.
    This class holds configuration parameters for creating and managing
    QEMU-based virtual ECUs (Electronic Control Units) that simulate hardware devices.
    It defines container specifications, networking information, and Docker Compose
    configuration settings required to launch and manage these virtualized components.

    Attributes:
        containers set[_Container]: Set of container configurations to be instantiated for the virtual ECU.

        rig_docker_name (str): Base name used for Docker containers that will run the virtual ECU.

        compose_command (str): Docker Compose command to use (e.g., "docker-compose", "docker compose").
        Can also be a wrapper script, e.g. "docker_compose_wrapper.sh". Is executed in the root of the repo,
        so when passing a script path it should be relative to the root of the repo.

        compose_yaml_path (PosixPath): Path to the Docker Compose YAML configuration file.

        docker_buildkit (bool): Whether to enable Docker Buildkite for improved build performance.

        env_file (PosixPath): Path to environment file used by Docker Compose.

        extra_compose_args (str): Optional additional arguments to pass to Docker Compose commands.

        docker_network_gateway_ip (str): IP address of the docker network gateway.

        network_interface (str): The docker network interface used for routing to the QEMU VM.
    """

    containers: set[QEMUContainer]

    rig_docker_name: str

    compose_command: str

    compose_yaml_path: PosixPath

    docker_buildkit: bool

    env_file: PosixPath

    ecr_url: Optional[str] = ""

    extra_compose_args: Optional[str] = ""


@dataclass()
class QEMUVirtualECUConfig(SoftwareCompRoot, Layer[_QEMUVirtualECUConfig]):
    containers: Union[
        set[QEMUContainer],  # Base type
        set[SetTransformTypes],
        Undefined[set[QEMUContainer]],
    ] = Default[set[QEMUContainer]](cast(set[QEMUContainer], frozenset()))

    rig_docker_name: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    compose_command: Union[
        str,  # Base type
        Defer[VehicleCtx, str],
        Undefined[str],
        Undefined[Defer[VehicleCtx, str]],
    ] = Undefined[str]()

    compose_yaml_path: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath],
        Undefined[Defer[VehicleCtx, PosixPath]],
    ] = Undefined[PosixPath]()

    env_file: Union[
        PosixPath,  # Base type
        Defer[VehicleCtx, PosixPath],
        Undefined[PosixPath],
        Undefined[Defer[VehicleCtx, PosixPath]],
    ] = Undefined[PosixPath]()

    docker_buildkit: Union[
        bool,  # Base type
        Defer[ComputeCtx, bool],
        Undefined[bool],
        Undefined[Defer[ComputeCtx, bool]],
    ] = Undefined[bool]()

    ecr_url: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    extra_compose_args: Union[
        Optional[str],  # Base type
        Defer[VehicleCtx, Optional[str]],
        Undefined[Optional[str]],
        Undefined[Defer[VehicleCtx, Optional[str]]],
    ] = Default[Optional[str]](None)

    @property
    def _resolver(self) -> type[_QEMUVirtualECUConfig]:
        return _QEMUVirtualECUConfig


class RigClass(Enum):
    PROD = "prod"
    DEV = "dev"
    TEST = "test"


@dataclass(frozen=True)
class _SoftwareSpecification(SoftwareCompRoot):
    embedded_compute_groups: Optional[set[EmbeddedComputeGroup]]
    compute_elements: set[_ComputeElement]
    build_flags: frozenset[_BuildFlag]
    gateway: Optional[GatewayConfig]
    hive: _Hive
    s3_config: Optional[_S3Config]
    ssh_aws_tunnel_config: Optional[_SSHAWSTunnelConfig]
    infotainment: Optional[_Infotainment]
    virtual_ecu_config: Optional[_DockerVirtualECUConfig]
    qemu_virtual_ecu_config: Optional[_QEMUVirtualECUConfig]
    debug_laptop: Optional[_DebugLaptopComputeElement]
    TEMPORARY_disable_network_config: bool
    rig_class: Optional[RigClass]


@dataclass()
class SoftwareSpecification(SoftwareCompRoot, Layer[_SoftwareSpecification]):
    embedded_compute_groups: Union[
        Optional[set[EmbeddedComputeGroup]],  # Base type
        Optional[set[SetTransformTypes]],
        Defer[VehicleCtx, Optional[set[EmbeddedComputeGroup]]],
        Undefined[Optional[set[EmbeddedComputeGroup]]],
        Undefined[Defer[VehicleCtx, Optional[set[EmbeddedComputeGroup]]]],
    ] = Default[Optional[set[EmbeddedComputeGroup]]](None)

    compute_elements: Union[
        set[ComputeElement],  # Base Type
        set[SetTransformTypes],
        Undefined[set[ComputeElement]],
    ] = Undefined()

    build_flags: Union[
        set[BuildFlag],
        set[SetTransformTypes],
        Undefined[set[BuildFlag]],
    ] = Default[set[BuildFlag]](cast(set[BuildFlag], frozenset()))

    gateway: Union[
        Optional[GatewayConfig],  # Base type
        Defer[VehicleCtx, Optional[GatewayConfig]],
        Undefined[Optional[GatewayConfig]],
        Undefined[Defer[VehicleCtx, Optional[GatewayConfig]]],
    ] = Default[Optional[GatewayConfig]](None)

    hive: Union[
        Optional[Hive],  # Base type
        Defer[VehicleCtx, Optional[Hive]],
        Undefined[Optional[Hive]],
        Undefined[Defer[VehicleCtx, Optional[Hive]]],
    ] = Default[Optional[Hive]](None)

    s3_config: Union[
        Optional[S3Config],  # Base type
        Defer[VehicleCtx, Optional[S3Config]],
        Undefined[Optional[S3Config]],
        Undefined[S3Config | Defer[VehicleCtx, Optional[S3Config]]],
    ] = Default[Optional[S3Config]](None)

    ssh_aws_tunnel_config: Union[
        Optional[SSHAWSTunnelConfig],  # Base type
        Defer[VehicleCtx, Optional[SSHAWSTunnelConfig]],
        Undefined[Optional[SSHAWSTunnelConfig]],
        Undefined[Defer[VehicleCtx, Optional[SSHAWSTunnelConfig]]],
    ] = Default[Optional[SSHAWSTunnelConfig]](None)

    infotainment: Union[
        Optional[Infotainment],  # Base type
        Defer[VehicleCtx, Optional[Infotainment]],
        Undefined[Optional[Infotainment]],
        Undefined[Infotainment | Defer[VehicleCtx, Optional[Infotainment]]],
    ] = Default[Optional[Infotainment]](None)

    virtual_ecu_config: Union[
        Optional[DockerVirtualECUConfig],  # Base type
        Defer[VehicleCtx, Optional[DockerVirtualECUConfig]],
        Undefined[Optional[DockerVirtualECUConfig]],
        Undefined[Defer[VehicleCtx, Optional[DockerVirtualECUConfig]]],
    ] = Default[Optional[DockerVirtualECUConfig]](None)

    qemu_virtual_ecu_config: Union[
        Optional[QEMUVirtualECUConfig],  # Base type
        Defer[VehicleCtx, Optional[QEMUVirtualECUConfig]],
        Undefined[Optional[QEMUVirtualECUConfig]],
        Undefined[Defer[VehicleCtx, Optional[QEMUVirtualECUConfig]]],
    ] = Default[Optional[QEMUVirtualECUConfig]](None)

    debug_laptop: Union[
        Optional[DebugLaptopComputeElement],  # Base type
        Defer[VehicleCtx, Optional[DebugLaptopComputeElement]],
        Undefined[Optional[DebugLaptopComputeElement]],
        Undefined[Defer[VehicleCtx, Optional[DebugLaptopComputeElement]]],
    ] = Default[Optional[DebugLaptopComputeElement]](None)

    TEMPORARY_disable_network_config: Union[
        bool,  # Base type
        Defer[VehicleCtx, bool],
        Undefined[bool],
        Undefined[Defer[VehicleCtx, bool]],
    ] = Default[bool](False)

    rig_class: Union[
        Optional[RigClass],  # Base type
        Defer[VehicleCtx, Optional[RigClass]],
        Undefined[Optional[RigClass]],
        Undefined[Defer[VehicleCtx, Optional[RigClass]]],
    ] = Default[Optional[RigClass]](None)

    def __hash__(self) -> int:
        return super().__hash__()

    @property
    def _resolver(self) -> type[_SoftwareSpecification]:
        return _SoftwareSpecification


#  Vehicle Instance
#


@dataclass
class VehicleInstance:
    name: str  # The name of the rig.
    make: str  # The make of the vehicle.
    model: str  # The model of the vehicle.
    hardware_specification: HardwareSpecification
    pcan_eth_gateway_ip: Optional[IPAddress] = None
    is_adas_available: bool = False  # Whether the vehicle has ADAS system. TODO(Tianyu): this is a temporary flag and we need to move it to a proper place.
    rotation_override: str = "0"  # Override overall Android rotation. TODO(kmliou): this is a temporary flag and we need to move it to a proper place.


#
# Apply context to resolve deferred values
#


def update_context(
    ctx: Union[VehicleCtx, ComputeCtx],
    parent_node: Any,
    attr_name: str,
    hardware_specification: HardwareSpecification,
) -> VehicleCtx:
    """Creates an updated Vehicle Context based on the input parameters"""

    # Creates a copy of the context with the updated path
    ctx = replace(ctx, path=f"{ctx.path}.{attr_name}")

    # Update the context if the parent node is a ComputeElement
    if issubclass(type(parent_node), ComputeElement):
        ctx = ComputeCtx(
            compute_element=hardware_specification.hardware_element_by_name(
                parent_node.name
            ),
            **dataclasses.asdict(ctx),
        )

    return ctx


def run_deferred(
    value: Defer[VehicleCtx | ComputeCtx, T],
    ctx: Union[VehicleCtx, ComputeCtx],
) -> T:
    """Runs a deferred function with the given context"""
    return value(ctx)


def _do_configure_set(
    ctx: VehicleCtx, value: set[T] | frozenset[T], vehicle: VehicleInstance
) -> frozenset[T]:
    new_set: set[T] = set()
    for index, item in enumerate(value):
        item_context = update_context(
            ctx, value, f"[{index}]", vehicle.hardware_specification
        )
        new_set.add(_do_configure(item_context, item, vehicle))
    return frozenset(new_set)


def _do_configure_software_comp_root(
    ctx: VehicleCtx, value: SoftwareCompRoot, vehicle: VehicleInstance
) -> SoftwareCompRoot:
    new_attrs = {}
    for attr_name in asdict(value):  # type: ignore[call-overload]
        attr: Any = getattr(value, attr_name)
        field_context = update_context(
            ctx, value, attr_name, vehicle.hardware_specification
        )
        new_attrs[attr_name] = _do_configure(field_context, attr, vehicle)
    return value.__class__(**new_attrs)


def _do_configure(ctx: VehicleCtx, value: T, vehicle: VehicleInstance) -> T:
    # Catch all base types
    if (
        isinstance(
            value,
            (
                str,
                int,
                bool,
                float,
                Default,
                Enum,
                PosixPath,
                IsProtobuf,
                IPAddress,
                MACAddress,
            ),
        )
        or value is None
    ):
        return cast(T, value)
    if isinstance(value, Defer):
        return cast(T, run_deferred(value, ctx))
    if isinstance(value, set) or isinstance(value, frozenset):
        return cast(T, _do_configure_set(ctx, value, vehicle))
    if issubclass(type(value), SoftwareCompRoot):
        return cast(
            T,
            _do_configure_software_comp_root(
                ctx, cast(SoftwareCompRoot, value), vehicle
            ),
        )

    raise ValueError(f"Unhandled type for {ctx.path}: {type(value)}")


def configure(
    vehicle: VehicleInstance,
    software_template: SoftwareSpecification,
) -> SoftwareSpecification:
    """Configures a software template for a given vehicle instance."""
    root_ctx = VehicleCtx(
        path="root",
        name=vehicle.name,
        make=vehicle.make,
        model=vehicle.model,
        is_adas_available=vehicle.is_adas_available,
    )

    return _do_configure(root_ctx, software_template, vehicle)


#
# Contexts
#


@dataclass
class VehicleCtx:
    path: str  # Typically only used for debugging
    name: str
    make: str
    model: str
    is_adas_available: bool

    def __hash__(self) -> int:
        return hash(id(self))


@dataclass
class ComputeCtx(VehicleCtx):
    compute_element: HardwareElementInstance

    def __hash__(self) -> int:
        return super().__hash__()


def get_software_and_hardware_from_symbols(
    software_symbol: str, hardware_symbol: str
) -> tuple[VehicleInstance, _SoftwareSpecification]:
    software_module, software_symbol = software_symbol.rsplit(".", 1)
    hardware_module, hardware_symbol = hardware_symbol.rsplit(".", 1)
    software = import_config(software_module, software_symbol)
    hardware = import_config(hardware_module, hardware_symbol)
    assert isinstance(software, SoftwareSpecification)
    assert isinstance(hardware, VehicleInstance)
    software_config: SoftwareSpecification = configure(hardware, software)
    return hardware, software_config.simplify()
