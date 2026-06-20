"""Rig — vendor-side bundle of machines + applications for one vehicle.

Not part of the AUTOSAR Adaptive manifest set proper; this is the
top-level container a vendor syscomp file emits so a single
``arsyscomp.py`` produces the full set of manifests for one rig.

Two parallel types in this module:

- :class:`Rig` (legacy) — flat dataclass with ``list[...]`` fields,
  composed via the ``apply_ops``-driven :class:`Layer` from
  ``manifest/layer.py``. Used by every current call site
  (``services/manifest/fc.py``, ``app/manifest/rig.py``,
  ``artheia executor emit``).
- :class:`SoftwareSpecification` (new) — :class:`Layer` subclass with
  ``set[...]`` fields that accept either bare elements OR
  :class:`Append` / :class:`Remove` transforms inline.
  ``base.mappend(other)`` composes them. The mosaic-style DSL the
  manifest module is migrating to. See
  ``docs/tasks/PROGRESS/artheia-dsl-recovery.md``.

Both types coexist during the migration. New vehicle layers should
use :class:`SoftwareSpecification`; old call sites stay on :class:`Rig`
until each is ported.

Future work: emit each :class:`MachineManifest` / :class:`ApplicationManifest`
to its own YAML, plus a ``rig.yaml`` index — the bazel rule
``bazel distr //vendor/vehicles/<rig>/`` consumes the index to assemble
per-machine opkg images.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from artheia.manifest.application import ApplicationManifest
from artheia.manifest.execution import ExecutionManifest
from artheia.manifest.machine import (
    MachineManifest,
    NodeToCPUMapping,
    ProcessToMachineMapping,
)
from artheia.manifest.service import ServiceManifest
from artheia.manifest.supervisor import SupervisorNode
from artheia.manifest.applicative import (
    Append,
    Layer,
    Remove,
    SetTransformTypes,
    Empty,
)


@dataclass
class VehicleIdentity:
    """Vendor-facing identity of one vehicle / rig."""

    name: str
    make: str = ""
    model: str = ""


@dataclass
class Rig:
    """Top-level container: one rig = one vehicle identity + N machines + M applications + service manifests.

    Cross-references resolve by *name*:

    - Every :class:`ApplicationManifest`'s ``host_machine`` must match a
      :class:`MachineManifest.name` in :attr:`machines`.
    - Every :class:`service.ServiceInstance.remote_machine` (when set)
      must match a :class:`MachineManifest.name` in :attr:`machines`.
    - SW components live inside applications; their bazel targets are
      what the build system ultimately consumes.
    """

    vehicle: VehicleIdentity
    machines: list[MachineManifest] = field(default_factory=list)
    applications: list[ApplicationManifest] = field(default_factory=list)
    service_manifests: list[ServiceManifest] = field(default_factory=list)
    execution_manifests: list[ExecutionManifest] = field(default_factory=list)
    # Process-to-machine mappings (AUTOSAR §9.4). Each entry binds a
    # Process.name to a Machine.name plus optional core-affinity refs
    # (shall_run_on / shall_not_run_on, identified by ProcessorCore.name
    # which we resolve to integer core_ids at supervisor-emit time).
    process_to_machine_mappings: list[ProcessToMachineMapping] = field(
        default_factory=list
    )
    # Node-to-CPU mappings (project-local). The intra-process equivalent
    # of PTM: pin one artheia node (its thread) to a CPU set + scheduling
    # policy. PTM constrains the *process*; this constrains a *thread*
    # inside that process — both can apply simultaneously, with the
    # thread-side acting as a finer subset of the process-side.
    node_to_cpu_mappings: list[NodeToCPUMapping] = field(
        default_factory=list
    )
    # OTP-style supervisor tree, declared by name. Each SupervisorNode
    # names its children (other SupervisorNodes or Process names from
    # execution_manifests). build_supervisor_tree(rig) walks this list
    # to produce the materialized SupervisorSpec tree consumed by the
    # supervisor binary.
    supervisors: list[SupervisorNode] = field(default_factory=list)

    # Per-machine supervisor identity — the node implementing ARA Execution
    # Management on each machine. Keyed by Machine.name → Supervisor (its TIPC
    # instance). Lets two machines on one TIPC namespace run distinct supervisors
    # (central=0, compute=1). Flows into <machine>/execution.json as
    # supervisor_instance. Empty = every machine's supervisor at instance 0.
    supervisor: dict = field(default_factory=dict)

    # Rig-wide default logger SINK for every supervised Process (THEIA_LOGGER:
    # stdio|null|file:<path>|syslog). A Process with its own `logger` overrides
    # this; if BOTH are empty, build_supervisor_tree falls back to a per-process
    # file under /tmp/theia. A bare "file:<dir>" (no .log) is treated as a
    # DIRECTORY — each process gets <dir>/<name>.log (so one rig-level
    # "file:/var/log/theia" gives separable per-FC files).
    logger: str = ""


# ---------------------------------------------------------------------------
# Structured-DSL aggregator — the layered-record shape
# ---------------------------------------------------------------------------

# Type aliases for the set-typed fields. Each field accepts either:
#   - a bare set of concrete elements (replaces base's set on mappend), OR
#   - a set of Append/Remove transforms (composed onto base's set), OR
#   - Empty() (inherit base's value).
_MachineSet     = Union[set[MachineManifest], set[SetTransformTypes], Empty]
_AppSet         = Union[set[ApplicationManifest], set[SetTransformTypes], Empty]
_ServiceManSet  = Union[set[ServiceManifest], set[SetTransformTypes], Empty]
_ExecSet        = Union[set[ExecutionManifest], set[SetTransformTypes], Empty]
_PTMSet         = Union[set[ProcessToMachineMapping], set[SetTransformTypes], Empty]
_NodeMapSet     = Union[set[NodeToCPUMapping], set[SetTransformTypes], Empty]
_SupervisorSet  = Union[set[SupervisorNode], set[SetTransformTypes], Empty]


@dataclass
class SoftwareSpecification(Layer):
    """Vendor-side spec: composition-of-compositions for one rig.

    The structured-DSL counterpart to :class:`Rig`. Where :class:`Rig`
    has ``list[X]`` fields composed via the parallel ``add_X`` /
    ``remove_X`` lists on :class:`Layer`, ``SoftwareSpecification`` has
    ``set[X] | set[SetTransformTypes] | Empty``, with the
    transforms living *inside* the set they target.

    Usage:

    .. code-block:: python

        from artheia.manifest import (
            SoftwareSpecification, MachineManifest, VehicleIdentity,
        )
        from artheia.manifest.applicative import Append, Remove, SetTransformTypes
        from typing import cast

        # Base spec for a platform.
        PlatformSoftware = SoftwareSpecification(
            vehicle=VehicleIdentity(name="platform"),
            machines={MachineManifest(name="default_host", ...)},
            ...
        )

        # A rig-specific layer.
        AppLayer = SoftwareSpecification(
            vehicle=VehicleIdentity(name="app", make="theia", model="..."),
            machines=cast(set[SetTransformTypes], {
                Append(MachineManifest(name="app_host", ...)),
                Remove(MachineManifest(name="default_host")),
            }),
        )

        # Compose.
        AppSoftware = PlatformSoftware.mappend(AppLayer)

    Field semantics mirror :class:`Rig` 1:1 — same containment, just
    set-typed instead of list-typed and with transform support.
    """

    # Fields default to Empty() (not empty set) so that a layer
    # which doesn't touch a field inherits the base's value during
    # mappend. Empty set as default would REPLACE the base's content
    # (ap_transforms treats a plain non-transform set as "wholesale
    # replace") — which is rarely what an upper layer means by
    # "I don't touch this field".
    vehicle: Union[VehicleIdentity, Empty] = field(default_factory=Empty)
    machines: _MachineSet = field(default_factory=Empty)
    applications: _AppSet = field(default_factory=Empty)
    service_manifests: _ServiceManSet = field(default_factory=Empty)
    execution_manifests: _ExecSet = field(default_factory=Empty)
    # Process-to-machine mappings (AUTOSAR §9.4).
    process_to_machine_mappings: _PTMSet = field(default_factory=Empty)
    # Node-to-CPU mappings (project-local; per-thread within a process).
    node_to_cpu_mappings: _NodeMapSet = field(default_factory=Empty)
    # OTP-style supervisor tree.
    supervisors: _SupervisorSet = field(default_factory=Empty)

    # Per-machine supervisor identity (machine name → Supervisor; its TIPC
    # instance). Scalar dict like `logger` — not a set transform. Empty → {}.
    supervisor: Union[dict, Empty] = field(default_factory=Empty)

    # Rig-wide default logger sink (scalar, like `vehicle`). Empty → a
    # combining layer inherits the base's value; an empty string in the
    # materialized Rig means "no rig default → per-process /tmp/theia fallback".
    logger: Union[str, Empty] = field(default_factory=Empty)

    # -----------------------------------------------------------------
    # Bridge to legacy Rig — until call sites (executor emit, gui emit,
    # generate_manifest) walk SoftwareSpecification directly. New code
    # writes the spec; this method projects it back into the flat
    # list-typed Rig the CLI still expects.
    # -----------------------------------------------------------------

    def to_rig(self) -> "Rig":
        """Materialize this spec into a legacy :class:`Rig`.

        Resolves every set-typed field by running :func:`fold_transforms`
        (Append/Remove transforms applied against an empty base) and
        emitting a deterministically-sorted list.

        Sort key: ``_set_identify`` (i.e. ``hash(name)`` for the
        default Identifiable). The CLI depends on stable ordering for
        the executor.yaml supervisor tree.
        """
        from artheia.manifest.applicative import fold_transforms

        def _resolve(field_value):
            """Set field → sorted list. Empty → empty list."""
            if isinstance(field_value, Empty):
                return []
            resolved = fold_transforms(field_value)
            # Deterministic order: by name (the default _identity_field).
            return sorted(resolved, key=lambda x: getattr(x, "name", ""))

        vehicle = self.vehicle
        if isinstance(vehicle, Empty):
            vehicle = VehicleIdentity(name="")

        logger = "" if isinstance(self.logger, Empty) else self.logger

        return Rig(
            vehicle=vehicle,
            machines=_resolve(self.machines),
            applications=_resolve(self.applications),
            service_manifests=_resolve(self.service_manifests),
            execution_manifests=_resolve(self.execution_manifests),
            process_to_machine_mappings=_resolve(self.process_to_machine_mappings),
            node_to_cpu_mappings=_resolve(self.node_to_cpu_mappings),
            supervisors=_resolve(self.supervisors),
            supervisor={} if isinstance(self.supervisor, Empty) else self.supervisor,
            logger=logger,
        )
