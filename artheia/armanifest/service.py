"""Service Manifest — AUTOSAR TPS Manifest Specification.

Two chapter sources:

- Chapter 3 (Application Design) defines :class:`ServiceInterface`,
  :class:`ClientServerOperation` (the AUTOSAR name for what we used to
  call ``ServiceMethod``), :class:`VariableDataPrototype` (events),
  and :class:`Field`.
- Chapter 11 (Service Instance Manifest) defines the binding side:
  ``ProvidedServiceInstance``, ``RequiredServiceInstance``, and
  ``ServiceInterfaceDeployment``. That chapter is huge (~414 KB of
  spec text); for now we keep a simpler in-memory shape that carries
  the transport binding directly and document the spec home for each
  field.

Sources:
- §3.4: ServiceInterface, VariableDataPrototype, Field
- §3.4.4: ClientServerOperation (operation = method)
- §11: ServiceInstance bindings and deployment
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from ipaddress import IPv4Address

from artheia.armanifest.transform import Identifiable


# ---------------------------------------------------------------------------
# Data types for the Adaptive Platform (§3.3)
# ---------------------------------------------------------------------------


@dataclass
class DataType(Identifiable):
    """An Adaptive Platform datatype (§3.3).

    The spec splits ApplicationDataType, ImplementationDataType, and
    AutosarDataType; we collapse them into one carrier until the
    consumers actually need the distinction.
    """

    name: str
    base_type: str = ""  # e.g. "uint32", or another DataType name


@dataclass
class VariableDataPrototype(Identifiable):
    """One service event (§3.4.2).

    AUTOSAR aggregates these on :attr:`ServiceInterface.event`. The
    server controls the value and the time of update; clients receive
    notifications.
    """

    name: str
    payload: DataType | None = None


@dataclass
class ArgumentDataPrototype:
    """A parameter on a :class:`ClientServerOperation` (§3.4.4)."""

    name: str
    type: DataType | None = None
    direction: str = "in"  # "in" | "out" | "inout"


@dataclass
class ClientServerOperation(Identifiable):
    """One method on a :class:`ServiceInterface` (§3.4.4)."""

    name: str
    arguments: list[ArgumentDataPrototype] = field(default_factory=list)
    # Note on naming: AUTOSAR uses the term "method" colloquially and
    # "ClientServerOperation" formally. Tools may emit one or the
    # other; ours uses the formal name.


@dataclass
class Field(Identifiable):
    """A field on a :class:`ServiceInterface` (§3.4.5)."""

    name: str
    type: DataType | None = None
    has_getter: bool = True
    has_setter: bool = False
    has_notifier: bool = True


@dataclass
class Trigger(Identifiable):
    """A data-less server→client signal (§3.4.3)."""

    name: str


# ---------------------------------------------------------------------------
# Service Interface (§3.4)
# ---------------------------------------------------------------------------


@dataclass
class ServiceInterface(Identifiable):
    """One Adaptive service interface (§3.4).

    Aggregates events (``VariableDataPrototype``), methods
    (``ClientServerOperation``), fields (``Field``), and triggers
    (``Trigger``). Versions are positive integers per §3.4.1.

    AUTOSAR uses ``majorVersion``/``minorVersion`` as the attribute
    names; we keep the spec spelling.
    """

    name: str
    major_version: int = 1
    minor_version: int = 0
    event: list[VariableDataPrototype] = field(default_factory=list)
    method: list[ClientServerOperation] = field(default_factory=list)
    field_: list[Field] = field(default_factory=list)  # 'field' clashes w/ dataclasses.field
    trigger: list[Trigger] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoint binding (transport-layer)
# ---------------------------------------------------------------------------


class TransportBinding(Enum):
    """How service traffic reaches the wire.

    Project-local enum — AUTOSAR Ch. 11 defines binding via
    ``ServiceInterfaceDeployment`` subclasses (e.g.
    ``SomeIpServiceInterfaceDeployment``). Two bindings cover the
    runtime today:

    - ``TIPC`` — host-local AF_TIPC SOCK_SEQPACKET. The default for
      every service the runtime hosts; address is a TIPC ``(type,
      instance)`` pair.
    - ``INET`` — TCP/UDP over IPv4. Used when a service is forwarded
      to a remote machine (e.g. ``log`` streamed via syslog-ng).
    """

    TIPC = "tipc"
    INET = "inet"


@dataclass(frozen=True)
class TipcAddress:
    """AF_TIPC service address — a ``(type, instance)`` pair."""

    type: int  # u32, matches GwMessageHeader.tipc.type
    instance: int = 0


@dataclass(frozen=True)
class InetEndpoint:
    """TCP/UDP endpoint for INET-bound services."""

    address: IPv4Address
    port: int


# ---------------------------------------------------------------------------
# Service instance binding (project-local; spec home is Ch. 11)
# ---------------------------------------------------------------------------


@dataclass
class ServiceInstance(Identifiable):
    """A concrete instance of a :class:`ServiceInterface` on the wire.

    Project-local roll-up of what AUTOSAR §11 splits into
    ``ProvidedServiceInstance`` (server side) + ``RequiredServiceInstance``
    (client side) plus a binding-specific deployment block. We track
    one entry per service and let the runtime decide provider vs
    consumer at deploy time.

    Identity for layer merging is :attr:`name`. The Macan layer can
    override e.g. ``log.binding`` from TIPC to INET plus an endpoint
    to redirect traffic at a remote forwarder, without restating the
    rest of the instance.
    """

    name: str
    interface: ServiceInterface | None = None
    instance_id: int = 0
    binding: TransportBinding = TransportBinding.TIPC
    tipc: TipcAddress | None = None
    inet: InetEndpoint | None = None
    # When the service is consumed off-machine, points at the
    # Machine.name that hosts the remote endpoint. Empty when the
    # service is local to its machine.
    remote_machine: str = ""


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@dataclass
class ServiceManifest(Identifiable):
    """Aggregate of one process's service-binding configuration.

    Note: the AUTOSAR spec doesn't define a single "ServiceManifest"
    class — service config is spread across ApplicationDesign (Ch. 3)
    and ServiceInstanceManifest (Ch. 11). This dataclass is a
    project-local convenience that bundles the related elements per
    process for the runtime.
    """

    name: str
    data_types: list[DataType] = field(default_factory=list)
    interfaces: list[ServiceInterface] = field(default_factory=list)
    instances: list[ServiceInstance] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Legacy aliases
# ---------------------------------------------------------------------------

# Old field name was `methods` / `events` / `fields` (Python-style
# plurals); the spec spelling is singular. Keep the plural aliases
# until callers migrate.
ServiceEvent = VariableDataPrototype
ServiceMethod = ClientServerOperation
ServiceField = Field
