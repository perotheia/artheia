"""Application Manifest — AUTOSAR TPS Manifest Specification, Chapter 3.

Note: the spec doesn't define a single ``ApplicationManifest`` class.
Chapter 3 is "Application Design" — it specifies the classes an
application contributes: :class:`Executable` (§3.18),
:class:`SwComponentType` and its subclasses (§3.4, §3.5),
:class:`ProcessDesign` (§3.21), the :class:`ServiceInterface` family
(§3.4; defined in :mod:`artheia.manifest.service`), and data types (§3.3).

We keep :class:`ApplicationManifest` as a project-local umbrella that
bundles these per logical application. The :class:`SwComponent`
dataclass that the earlier scaffolding shipped is a deployment-side
convenience (bazel target + .art node reference) and *not* a spec
class; it lives next to :class:`SwComponentPrototype` for symmetry.

Sources:
- §3.4 ServiceInterface and its members.
- §3.18 Executable + BuildTypeEnum + RootSwComponentPrototype.
- §3.21 ProcessDesign.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from artheia.manifest.transform import Identifiable, identifiable_dataclass


# ---------------------------------------------------------------------------
# SwComponent (design-side, §3.4 / §3.5)
# ---------------------------------------------------------------------------


@identifiable_dataclass
class PortPrototype(Identifiable):
    """A port on a :class:`SwComponentType` (§3.4 / §3.5).

    AUTOSAR distinguishes PPortPrototype (provided) /
    RPortPrototype (required) / PRPortPrototype (provided-and-required);
    we keep one class with an explicit :attr:`direction` until the
    asymmetry actually matters.
    """

    name: str
    direction: str = "provided"   # "provided" | "required" | "providedRequired"
    interface: str = ""           # ref by name to a ServiceInterface


@identifiable_dataclass
class SwComponentType(Identifiable):
    """Abstract base for AUTOSAR software-component types (§3.4 / §3.5).

    Subclasses in the spec:

    - :class:`AdaptiveApplicationSwComponentType` (the common app case).
    - :class:`AtomicSwComponentType` (leaf-level component).
    - :class:`CompositionSwComponentType` (aggregates prototypes +
      connectors).
    - :class:`ParameterSwComponentType` (config-only).

    We don't carve out separate dataclasses per subclass yet — the
    :attr:`category` attribute distinguishes them in the same way that
    AUTOSAR's M1 layer uses the type tag.
    """

    name: str
    category: str = "adaptiveApplication"
    port: list[PortPrototype] = field(default_factory=list)


@identifiable_dataclass
class SwComponentPrototype(Identifiable):
    """An instance of a :class:`SwComponentType` inside a composition (§3.5)."""

    name: str
    component_type: str = ""   # ref by name to SwComponentType


@identifiable_dataclass
class SwConnector(Identifiable):
    """A connector between two ports in a composition (§3.5)."""

    name: str
    source: str = ""   # "<prototype>.<port>"
    target: str = ""   # "<prototype>.<port>"


@identifiable_dataclass
class CompositionSwComponentType(Identifiable):
    """A composition aggregating :class:`SwComponentPrototype` + :class:`SwConnector` (§3.5)."""

    name: str
    component_prototype: list[SwComponentPrototype] = field(default_factory=list)
    connector: list[SwConnector] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deployment-side convenience: a buildable handle on a component
# ---------------------------------------------------------------------------


@identifiable_dataclass
class SwComponent(Identifiable):
    """A deployable software component (project-local).

    This *is not* a spec class — it's the buildable handle our toolchain
    needs to drive bazel and resolve ``.art`` definitions. Carries:

    - :attr:`bazel_target` — what ``bazel build <bazel_target>``
      compiles, against the target machine's architecture.
    - :attr:`art_node` — the path-and-node ref into the artheia textX
      model. Form is ``<package>/NodeName``, e.g.
      ``vendor.odd_path_client.system.components/OddPathMonitor``.
    - :attr:`owner` — informational.

    Identity for layer merging is :attr:`name`. The mapping from
    :class:`SwComponent` to the spec-side :class:`SwComponentType`
    happens at codegen / deploy time once the ``.art`` definitions are
    parsed.
    """

    name: str
    bazel_target: str
    owner: str = ""
    art_node: str = ""


# ---------------------------------------------------------------------------
# Executable (§3.18)
# ---------------------------------------------------------------------------


class BuildTypeEnum(str, Enum):
    """Spec §3.18.1 — release vs debug build."""

    BUILD_TYPE_RELEASE = "buildTypeRelease"
    BUILD_TYPE_DEBUG = "buildTypeDebug"


class ExecutionStateReportingBehaviorEnum(str, Enum):
    """Spec §3.18 — how the executable reports its execution state."""

    REPORTING_BEHAVIOR_ALWAYS = "reportingBehaviorAlways"
    REPORTING_BEHAVIOR_NEVER = "reportingBehaviorNever"
    REPORTING_BEHAVIOR_INDIVIDUAL = "reportingBehaviorIndividual"


@identifiable_dataclass
class RootSwComponentPrototype(Identifiable):
    """Root SwComponentPrototype aggregated by an :class:`Executable` (§3.18.3)."""

    name: str
    application_type: str = ""  # ref by name to SwComponentType


@identifiable_dataclass
class Executable(Identifiable):
    """Spec §3.18.

    Attribute names mirror the spec. :attr:`category` is one of
    ``PLATFORM_LEVEL`` / ``APPLICATION_LEVEL`` per §3.18.2.
    """

    name: str
    category: str = "APPLICATION_LEVEL"
    build_type: BuildTypeEnum = BuildTypeEnum.BUILD_TYPE_RELEASE
    minimum_timer_granularity: float | None = None  # seconds
    reporting_behavior: ExecutionStateReportingBehaviorEnum = (
        ExecutionStateReportingBehaviorEnum.REPORTING_BEHAVIOR_INDIVIDUAL
    )
    root_sw_component_prototype: RootSwComponentPrototype | None = None
    version: str = ""


# ---------------------------------------------------------------------------
# ProcessDesign (§3.21)
# ---------------------------------------------------------------------------


@identifiable_dataclass
class ProcessDesign(Identifiable):
    """Design-time pre-allocation of an :class:`Executable` to a Process (§3.21).

    Stands in for a :class:`artheia.manifest.execution.Process` before the
    Process itself exists. Used by the deployment-side mapping
    machinery (``ProcessToMachineMapping``, etc.) to plan placement
    without committing to a concrete deployment.
    """

    name: str
    executable: str = ""  # ref by name to Executable


# ---------------------------------------------------------------------------
# Legacy compatibility shims
# ---------------------------------------------------------------------------

# Old scaffolding called PortPrototype the same name; ServiceInterface
# elements were inline. Kept for source compat.
@dataclass
class StartupConfig:
    """Compatibility shim — application-level startup config.

    Use :class:`artheia.manifest.execution.StartupConfig` for the spec-aligned
    deployment-side definition.
    """

    arguments: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Root umbrella (project-local)
# ---------------------------------------------------------------------------


@identifiable_dataclass
class ApplicationManifest(Identifiable):
    """Project-local umbrella bundling the design-side artefacts of one
    application.

    The AUTOSAR spec doesn't pack these into one class; we use this
    container so the layer system has a target for ``add_components``
    et al.

    Cross-references:

    - :attr:`host_machine` — name of the :class:`artheia.manifest.machine.Machine`
      this application lands on. (Strictly an AUTOSAR
      ``ProcessToMachineMapping`` concern; we keep it here for
      convenience.)
    - :attr:`components` — buildable :class:`SwComponent` handles.
    - :attr:`component_types` / :attr:`compositions` — spec-aligned
      design content.
    - :attr:`executables` / :attr:`process_designs` — deployment ramp.
    """

    name: str
    host_machine: str = ""
    components: list[SwComponent] = field(default_factory=list)
    component_types: list[SwComponentType] = field(default_factory=list)
    compositions: list[CompositionSwComponentType] = field(default_factory=list)
    executables: list[Executable] = field(default_factory=list)
    process_designs: list[ProcessDesign] = field(default_factory=list)
