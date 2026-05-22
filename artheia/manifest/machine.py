"""Machine Manifest — AUTOSAR TPS Manifest Specification, Chapter 9.

The root class on the AUTOSAR side is ``Machine``. A Machine aggregates
``Processor`` instances (each with one or more ``ProcessorCore``),
environment variables, a default application timeout, security
configuration, and module-instantiation entries.

Process placement uses ``ProcessToMachineMappingSet`` /
``ProcessToMachineMapping`` (§9.4) — ``shallRunOn`` / ``shallNotRunOn``
references onto ``ProcessorCore`` express core affinity for a given
Process (the AUTOSAR-correct home for CPU pinning; see Ch. 8 prose).

Sources: AUTOSAR_TPS_ManifestSpecification.pdf §9 (Machine Manifest).

Project-specific extensions retained from the earlier scaffold:

- ``NetworkInterface`` lives in the Machine for the runtime's
  convenience (the spec routes network config through MachineDesign in
  Ch. 5). We keep it here until the MachineDesign side lands.
- ``CpuArchitecture`` enum is project-local (the spec uses arbitrary
  string in :attr:`Processor`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from ipaddress import IPv4Address

from artheia.manifest.execution import EnterExitTimeout, TagWithOptionalValue
from artheia.manifest.transform import Identifiable, identifiable_dataclass


# ---------------------------------------------------------------------------
# Processor / cores (§9.2)
# ---------------------------------------------------------------------------


class CpuArchitecture(str, Enum):
    """Project-local enum. The spec lets :attr:`Processor` carry an
    arbitrary string identifier; we use this for type safety and the
    common cases.
    """

    X86_64 = "x86_64"
    AARCH64 = "aarch64"
    ARMV7 = "armv7"
    RISCV64 = "riscv64"


@identifiable_dataclass
class ProcessorCore(Identifiable):
    """One core within a :class:`Processor` (§9.2)."""

    name: str
    core_id: int | None = None


@identifiable_dataclass
class Processor(Identifiable):
    """A processor aggregated by :class:`Machine` (§9.2)."""

    name: str
    core: list[ProcessorCore] = field(default_factory=list)
    architecture: CpuArchitecture = CpuArchitecture.AARCH64


# ---------------------------------------------------------------------------
# Process → Machine mapping (§9.4)
# ---------------------------------------------------------------------------


@identifiable_dataclass
class ProcessToMachineMapping(Identifiable):
    """Association of a Process to a Machine, with optional core affinity."""

    name: str
    process: str = ""                      # ref by name to Process.shortName
    machine: str = ""                      # ref by name to Machine.shortName
    shall_run_on: list[str] = field(       # refs to ProcessorCore by name
        default_factory=list
    )
    shall_not_run_on: list[str] = field(
        default_factory=list
    )
    non_os_module_instantiation: str = ""  # ref by name to NonOsModuleInstantiation
    persistency_central_storage_uri: str = ""


@identifiable_dataclass
class ProcessToMachineMappingSet(Identifiable):
    """A bucket of :class:`ProcessToMachineMapping` (§9.4)."""

    name: str
    process_to_machine_mapping: list[ProcessToMachineMapping] = field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# NodeToCPUMapping (project-local; intra-process thread affinity)
# ---------------------------------------------------------------------------

# AUTOSAR §9.4 puts process-to-machine affinity on the Machine manifest
# via ProcessToMachineMapping. That mechanism stops at the process
# boundary: it can say "this Process runs on machine M, may use cores
# {0,1}", but in our world a Process is also a host for several actor
# threads (one per artheia ``node atomic``). Real workloads need a
# *narrower* pin — e.g. "the crypto Daemon process can use cores 0-3,
# but inside it the AES worker thread pins to core 0 with SCHED_FIFO
# priority 80". That's NodeToCPUMapping.
#
# Identity (for layer merging) is :attr:`name`. The rig validator
# checks that ``process`` matches a Process.shortName already in the
# rig and ``node`` matches a node declared in that process's .art.

class SchedulingPolicyEnum(str, Enum):
    """POSIX scheduler policy (mirrored from <sched.h>)."""
    SCHED_OTHER    = "SCHED_OTHER"     # default time-sharing
    SCHED_FIFO     = "SCHED_FIFO"      # static real-time, no preemption by peers
    SCHED_RR       = "SCHED_RR"        # static real-time, round-robin
    SCHED_DEADLINE = "SCHED_DEADLINE"  # EDF (3 params: runtime, deadline, period)
    SCHED_BATCH    = "SCHED_BATCH"     # cpu-intensive batch
    SCHED_IDLE     = "SCHED_IDLE"      # lowest priority


@identifiable_dataclass
class NodeToCPUMapping(Identifiable):
    """Pin one artheia node (its thread) to a CPU set + scheduling policy.

    Lives on the rig alongside :class:`ProcessToMachineMapping`; the
    supervisor side resolves it onto a per-worker ChildSpec hint that
    the worker's process applies during node startup (pthread_setaffinity_np
    + sched_setscheduler). For SCHED_DEADLINE the three EDF params land
    in dl_runtime/deadline/period_ns (0 = unused otherwise).
    """

    name: str
    node:    str = ""                       # artheia node short name
    process: str = ""                       # the Process.shortName hosting the node
    machine: str = ""                       # the Machine.shortName the process runs on

    # CPU set — same semantics as PTM (mutually exclusive). When both
    # empty the node inherits its process's affinity.
    shall_run_on:     list[str] = field(default_factory=list)
    shall_not_run_on: list[str] = field(default_factory=list)

    # POSIX scheduling. SCHED_OTHER+0 means "leave default".
    scheduling_policy:   SchedulingPolicyEnum = SchedulingPolicyEnum.SCHED_OTHER
    scheduling_priority: int = 0          # rtprio for SCHED_FIFO/RR (1..99)
    nice:                int = 0          # -20..19, only for SCHED_OTHER/BATCH

    # SCHED_DEADLINE EDF parameters (nanoseconds). Ignored unless
    # scheduling_policy == SCHED_DEADLINE. Constraint: dl_runtime ≤
    # dl_deadline ≤ dl_period.
    dl_runtime_ns:  int = 0
    dl_deadline_ns: int = 0
    dl_period_ns:   int = 0


# ---------------------------------------------------------------------------
# Trust-platform launch behaviour (§9.1)
# ---------------------------------------------------------------------------


class TrustedPlatformExecutableLaunchBehaviorEnum(str, Enum):
    """How authentication affects the ability to launch an Executable."""

    STRICT_MODE = "strictMode"
    MONITOR_MODE = "monitorMode"
    NO_TRUSTED_PLATFORM_SUPPORT = "noTrustedPlatformSupport"


# ---------------------------------------------------------------------------
# Project-local network description (kept until MachineDesign lands)
# ---------------------------------------------------------------------------


class NetworkInterfaceKind(str, Enum):
    ETHERNET = "ethernet"
    CAN = "can"
    LIN = "lin"
    FLEXRAY = "flexray"
    LOOPBACK = "loopback"


@dataclass
class IpEndpoint:
    """A network endpoint exposed by a machine."""

    address: IPv4Address | None = None
    port: int = 0


@identifiable_dataclass
class NetworkInterface(Identifiable):
    name: str
    kind: NetworkInterfaceKind = NetworkInterfaceKind.ETHERNET
    endpoints: list[IpEndpoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Convenience aggregate (legacy `HardwareResource` shim)
# ---------------------------------------------------------------------------


@dataclass
class MemoryResource:
    """Memory description (project-local; spec puts memory budgets in
    :class:`ResourceConsumption` on the execution side).
    """

    total_bytes: int = 0
    huge_pages_bytes: int = 0


@dataclass
class CpuResource:
    """Compact CPU descriptor — wraps one :class:`Processor` for the
    common single-CPU case. Legacy alias.
    """

    architecture: CpuArchitecture = CpuArchitecture.AARCH64
    core_count: int = 0
    isolated_cores: list[int] = field(default_factory=list)


@dataclass
class HardwareResource:
    """Project-local roll-up of CPU + memory.

    For full spec fidelity use :attr:`Machine.processor` (list of
    :class:`Processor`); this dataclass exists for backward
    compatibility with the earlier scaffold.
    """

    cpu: CpuResource = field(default_factory=CpuResource)
    memory: MemoryResource = field(default_factory=MemoryResource)


# ---------------------------------------------------------------------------
# Machine (root, §9.1)
# ---------------------------------------------------------------------------


@identifiable_dataclass
class Machine(Identifiable):
    """Root of one machine's manifest set (§9.1).

    The spec splits this into multiple physical files (Machine,
    MachineDesign references, NICs, etc.); we keep it as one dataclass
    in memory and let the serializer fragment on emit.
    """

    name: str
    processor: list[Processor] = field(default_factory=list)
    environment_variable: list[TagWithOptionalValue] = field(default_factory=list)
    default_application_timeout: EnterExitTimeout | None = None
    trusted_platform_executable_launch_behavior: TrustedPlatformExecutableLaunchBehaviorEnum = (
        TrustedPlatformExecutableLaunchBehaviorEnum.NO_TRUSTED_PLATFORM_SUPPORT
    )
    machine_design: str = ""               # ref by name to MachineDesign
    module_instantiation: list[str] = field(default_factory=list)
    secure_communication_deployment: list[str] = field(default_factory=list)

    # Project-local fields below this line.
    hardware: HardwareResource = field(default_factory=HardwareResource)
    network_interfaces: list[NetworkInterface] = field(default_factory=list)
    # Operator endpoint(s) for this machine — used by tooling on a
    # different host (e.g. the supervisor GUI) to find services on the
    # machine. The supervisor is *not* exposed directly; instead, a
    # ``services/com`` bridge per machine fronts the supervisor (and
    # other in-host actors) on a stable gRPC endpoint. Per-machine port
    # assignment lets several machines coexist on one physical host.
    # Phase 7 lifts ``com_endpoint`` into ``machines.yaml`` for the GUI.
    com_endpoint: IpEndpoint = field(
        default_factory=lambda: IpEndpoint(
            address=IPv4Address("127.0.0.1"),
            port=7700,
        )
    )


# Legacy alias — keep existing callers compiling.
MachineManifest = Machine


# Project-local enum kept for backwards compatibility (older docs used
# ``MachineState`` directly; AUTOSAR puts machine states in the State
# Management interface, not on the Machine manifest).
@identifiable_dataclass
class MachineState(Identifiable):
    name: str
    description: str = ""
