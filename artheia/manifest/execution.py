"""Execution Manifest — AUTOSAR TPS Manifest Specification, Chapter 8.

The root class on the AUTOSAR side is ``Process``: a runtime container
that references an :class:`Executable` and carries per-machine-state
startup configuration. The legacy alias :data:`ExecutionManifest`
points at :class:`Process` so existing callers keep working.

Class hierarchy (matches the spec where possible; Python idioms applied
where they conflict with verbose AUTOSAR naming):

- :class:`Process` — root, identified by ``shortName``.
  - :attr:`stateDependentStartupConfig` — list of
    :class:`StateDependentStartupConfig`.
  - :attr:`processState` — :class:`ModeDeclarationGroup`.
- :class:`StateDependentStartupConfig` — startup config tied to one or
  more function-group states.
  - :attr:`startupConfig` — ref to :class:`StartupConfig`.
  - :attr:`executionDependency` — list of :class:`ExecutionDependency`.
  - :attr:`resourceConsumption` — :class:`ResourceConsumption`.
  - :attr:`resourceGroup` — ref to ``ResourceGroup`` (defined in
    :mod:`artheia.manifest.machine`).
- :class:`StartupConfig` — reusable startup config.
- :class:`ExecutionDependency` — launch ordering edge.
- :class:`ResourceConsumption` / :class:`MemoryUsage`.
- :class:`ProcessArgument` — one CLI arg.
- :class:`FunctionGroup` / :class:`ModeDeclarationGroup` /
  :class:`ModeDeclaration` — state machinery.

Sources: AUTOSAR_TPS_ManifestSpecification.pdf §8 (Execution Manifest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from artheia.manifest.transform import Identifiable


# ---------------------------------------------------------------------------
# State / Mode declarations (§8.4)
# ---------------------------------------------------------------------------


@dataclass
class ModeDeclaration(Identifiable):
    """One discrete state in a :class:`ModeDeclarationGroup`."""

    name: str
    value: int | None = None


@dataclass
class ModeDeclarationGroup(Identifiable):
    """A named set of mode declarations (e.g. a Process's state list).

    Aggregated by :attr:`Process.processState` and
    :class:`FunctionGroup`.
    """

    name: str
    modes: list[ModeDeclaration] = field(default_factory=list)
    initial_mode: str = ""


@dataclass
class FunctionGroup(Identifiable):
    """An identifiable group of processes managed together by State
    Management — e.g. ``Startup``, ``Driving``, ``Parking``.

    The group's states (e.g. ``Running``, ``Idle``) live in
    :attr:`modeDeclarationGroup`.
    """

    name: str
    mode_declaration_group: ModeDeclarationGroup | None = None


# ---------------------------------------------------------------------------
# Scheduling primitives (§8.3.2)
# ---------------------------------------------------------------------------


class SchedulingPolicy(str, Enum):
    """Standardised values for :attr:`StartupConfig.schedulingPolicy`.

    Non-standardised values may be used but must not clash with future
    AUTOSAR extensions.
    """

    SCHED_OTHER = "SCHED_OTHER"
    SCHED_FIFO = "SCHED_FIFO"
    SCHED_RR = "SCHED_RR"


# ---------------------------------------------------------------------------
# Resource accounting (§8.3.7)
# ---------------------------------------------------------------------------


@dataclass
class MemoryUsage(Identifiable):
    """Worst-case memory consumption, in bytes."""

    name: str
    memory_consumption: int | None = None


@dataclass
class ResourceConsumption(Identifiable):
    """Per-startup resource budgets aggregated by
    :class:`StateDependentStartupConfig`.
    """

    name: str
    memory_usage: list[MemoryUsage] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Process arguments + environment (§8.3.3)
# ---------------------------------------------------------------------------


@dataclass
class ProcessArgument:
    """One command-line argument (ordered within a :class:`StartupConfig`)."""

    argument: str = ""


@dataclass
class TagWithOptionalValue:
    """key / optional-value pair (env var, etc.)."""

    key: str
    value: str | None = None
    sequence_offset: int | None = None


# ---------------------------------------------------------------------------
# Termination (§8.3.8)
# ---------------------------------------------------------------------------


class TerminationBehaviorEnum(str, Enum):
    PROCESS_IS_NOT_SELF_TERMINATING = "processIsNotSelfTerminating"
    PROCESS_IS_SELF_TERMINATING = "processIsSelfTerminating"


# Defined fully in :mod:`artheia.manifest.machine` (the spec aggregates it via
# both ``Machine.defaultApplicationTimeout`` and
# ``StartupConfig.timeout``). Forward-declared here so this file stays
# self-contained for typing purposes.
@dataclass
class EnterExitTimeout:
    enter_timeout_value: float | None = None  # seconds
    exit_timeout_value: float | None = None   # seconds


# ---------------------------------------------------------------------------
# StartupConfig (§8.3.1)
# ---------------------------------------------------------------------------


@dataclass
class ProcessExecutionError(Identifiable):
    """Identifiable execution-error reference. Spec-§8.3.8."""

    name: str
    error_code: int | None = None


@dataclass
class StartupConfig(Identifiable):
    """Reusable startup configuration for one or more processes."""

    name: str
    environment_variable: list[TagWithOptionalValue] = field(default_factory=list)
    execution_error: ProcessExecutionError | None = None
    process_argument: list[ProcessArgument] = field(default_factory=list)
    scheduling_policy: SchedulingPolicy = SchedulingPolicy.SCHED_OTHER
    scheduling_priority: int = 0
    termination_behavior: TerminationBehaviorEnum = (
        TerminationBehaviorEnum.PROCESS_IS_NOT_SELF_TERMINATING
    )
    timeout: EnterExitTimeout | None = None


# ---------------------------------------------------------------------------
# Execution Dependency (§8.3.5)
# ---------------------------------------------------------------------------


@dataclass
class ExecutionDependency:
    """Launch-ordering edge.

    ``process_state`` is an instanceRef to a :class:`ModeDeclaration`
    on another Process — start order requires that target process to
    have reached that state.
    """

    process_state: str = ""  # "<other_process>.<mode_name>"


# ---------------------------------------------------------------------------
# StateDependentStartupConfig (§8.3 root) and Process (§8.2 root)
# ---------------------------------------------------------------------------


@dataclass
class StateDependentStartupConfig:
    """Per-state startup configuration aggregated by :class:`Process`."""

    function_group_state: list[str] = field(default_factory=list)
    # ^ list of "<FunctionGroup>.<ModeDeclaration>" iref strings.
    startup_config: StartupConfig | None = None
    execution_dependency: list[ExecutionDependency] = field(default_factory=list)
    resource_consumption: ResourceConsumption | None = None
    resource_group: str = ""  # ref by name to ResourceGroup on the Machine


@dataclass
class Process(Identifiable):
    """The root execution-manifest class — one POSIX process.

    Identity for layer merging is :attr:`name` (Process.shortName in
    the AUTOSAR data model).
    """

    name: str
    executable: str = ""  # ref by short-name to an Executable
    function_cluster_affiliation: str = ""
    number_of_restart_attempts: int | None = None
    pre_mapping: bool | None = None
    process_state: ModeDeclarationGroup | None = None
    state_dependent_startup_config: list[StateDependentStartupConfig] = field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# Legacy aliases
# ---------------------------------------------------------------------------

# The earlier scaffold called the root :class:`ExecutionManifest` and bundled
# a tiny :class:`ExecutableBinding` + :class:`TimingConfig` + :class:`StartupSpec`
# under it. The AUTOSAR-aligned shape uses :class:`Process` /
# :class:`StateDependentStartupConfig` / :class:`StartupConfig`. Keep the old
# names as type aliases + tiny shims so existing imports stay readable.
ExecutionManifest = Process


# Compact compatibility wrapper used by artheia.manifest.loader and vendor
# layer files. Constructs the equivalent :class:`StartupConfig` —
# callers that previously built `ExecutableBinding(timing=…, resources=…)`
# can switch field-by-field as they migrate.
@dataclass
class ExecutableBinding:
    """Compatibility helper grouping a small subset of StartupConfig.

    Maps onto the spec as follows:

    - :attr:`executable` → :attr:`Process.executable`.
    - :attr:`process_name` → :attr:`Process.name` of the resulting
      Process (set by the caller via the surrounding layer).
    - :attr:`timing` → :class:`StartupConfig` (scheduling_policy +
      scheduling_priority).
    - :attr:`resources` → :class:`ResourceLimits`, mapped into
      :class:`StartupConfig.execution_error` / cpu affinity (the
      AUTOSAR spec routes core affinity through
      ``ProcessToMachineMapping.shallRunOn`` — handled at the machine
      manifest level).
    """

    executable: str = ""
    process_name: str = ""
    timing: "TimingConfig" = field(default_factory=lambda: TimingConfig())
    resources: "ResourceLimits" = field(default_factory=lambda: ResourceLimits())


@dataclass
class TimingConfig:
    policy: SchedulingPolicy = SchedulingPolicy.SCHED_OTHER
    priority: int = 0
    period_ns: int | None = None
    deadline_ns: int | None = None
    runtime_ns: int | None = None


@dataclass
class ResourceLimits:
    cpu_affinity: list[int] = field(default_factory=list)
    memory_max_bytes: int | None = None
    open_files_max: int | None = None
    nice: int | None = None


@dataclass
class FunctionGroupReference:
    """Compatibility shim: legacy ``FunctionGroupReference`` referenced a FG
    + a subset of states. AUTOSAR uses an instanceRef from
    :class:`StateDependentStartupConfig.functionGroupState` directly.
    """

    function_group: str
    states: list[str] = field(default_factory=list)


@dataclass
class ProcessDependency:
    """Compatibility shim for ExecutionDependency with explicit edges."""

    process: str
    depends_on: str
    required_state: str = "running"


class ProcessState(str, Enum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    TERMINATING = "terminating"


@dataclass
class StartupSpec:
    """Compatibility wrapper around :class:`StateDependentStartupConfig`."""

    machine_states: list[str] = field(default_factory=list)
    function_groups: list[FunctionGroupReference] = field(default_factory=list)
    dependencies: list[ProcessDependency] = field(default_factory=list)
