"""Rig — vendor-side bundle of machines + applications for one vehicle.

Not part of the AUTOSAR Adaptive manifest set proper; this is the
top-level container a vendor syscomp file emits so a single
``arsyscomp.py`` produces the full set of manifests for one rig.

Future work: emit each :class:`MachineManifest` / :class:`ApplicationManifest`
to its own YAML, plus a ``rig.yaml`` index — the bazel rule
``bazel distr //vendor/vehicles/<rig>/`` consumes the index to assemble
per-machine opkg images.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from artheia.manifest.application import ApplicationManifest
from artheia.manifest.execution import ExecutionManifest
from artheia.manifest.machine import (
    MachineManifest,
    NodeToCPUMapping,
    ProcessToMachineMapping,
)
from artheia.manifest.service import ServiceManifest
from artheia.manifest.supervisor import SupervisorNode


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
