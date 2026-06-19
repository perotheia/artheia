"""Composable layers over a base manifest.

A :class:`Layer` is a small delta that adds, removes, or overrides
elements of a base :class:`Rig`. Layers chain in lower-to-upper order:
platform → vehicle-family → concrete rig. Each layer's ops are applied
to the running result via :func:`apply_layer`; :func:`merge_layers`
chains them in one call.

Field-level overrides honour identity (the ``name`` of each
:class:`SwComponent` / :class:`ServiceInstance`), so an upper layer can
patch one field of one element without restating the rest.

This module works on the *list*-shaped :class:`Rig` (its element
collections are plain lists), so it carries its own list-merge driver,
:func:`apply_ops`, on top of the identity-keyed element helpers in
:mod:`.applicative`. The set-shaped structured DSL (``mappend`` /
``Insert`` / ``Delete``) is the other consumer of those helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Union

from artheia.manifest.application import ApplicationManifest, SwComponent
from artheia.manifest.execution import ExecutionManifest
from artheia.manifest.machine import (
    MachineManifest,
    NodeToCPUMapping,
    ProcessToMachineMapping,
)
from artheia.manifest.rig import Rig, VehicleIdentity
from artheia.manifest.service import ServiceInstance, ServiceManifest
from artheia.manifest.supervisor import SupervisorNode
from artheia.manifest.applicative import (
    Identifiable,
    Insert,
    Delete,
    _identity_of,
    _merge_element,
    _merge_fields,
)


@dataclass(frozen=True)
class Override:
    """Patch field(s) of an existing list element keyed by ``identity``.

    Layer-only op: the set-shaped DSL expresses the same intent as a
    same-identity ``Insert`` that merges by field. Here, where elements
    live in plain lists, a dedicated patch op is simpler.
    """

    identity: Any
    patch: dict[str, Any]


Op = Union[Insert, Delete, Override]


def apply_ops(target_list: list, ops: Iterable[Op]) -> list:
    """Apply a sequence of Insert/Delete/Override ops to a list.

    Insert unions by identity (merging same-identity elements via
    :func:`_merge_element`); Delete drops by identity; Override patches
    named fields of a same-identity element.
    """
    out: list = list(target_list)
    index = {_identity_of(e): i for i, e in enumerate(out)}

    for op in ops:
        if isinstance(op, Insert):
            ident = _identity_of(op.value)
            if ident in index:
                out[index[ident]] = _merge_element(out[index[ident]], op.value)
            else:
                index[ident] = len(out)
                out.append(op.value)
        elif isinstance(op, Delete):
            # Delete takes either an identity or an Identifiable.
            ident = op.value._identity if isinstance(op.value, Identifiable) else op.value
            i = index.pop(ident, None)
            if i is not None:
                out.pop(i)
                index = {_identity_of(e): j for j, e in enumerate(out)}
        elif isinstance(op, Override):
            i = index.get(op.identity)
            if i is None:
                continue
            out[i] = _merge_fields(out[i], op.patch)
        else:  # pragma: no cover
            raise TypeError(f"unknown op {type(op).__name__}")

    return out


@dataclass
class Layer:
    """A delta over an existing :class:`Rig`.

    Each list is interpreted as a sequence of :class:`Insert` /
    :class:`Delete` / :class:`Override` ops. The convenience aliases
    (``add_components``, ``remove_components`` …) skip the boilerplate
    of wrapping every element in an op:

    - ``add_components: [SwComponent(...)]`` is shorthand for
      ``component_ops: [Insert(SwComponent(...))]``.
    - ``remove_components: ["fw"]`` is shorthand for
      ``component_ops: [Delete("fw")]``.
    - ``override_components: [Override("log", {"binding": INET})]``
      passes through unchanged.

    All three slots are flattened together in declared order during
    :func:`apply_layer`.
    """

    name: str = ""

    # Applies to ApplicationManifest.components on a target application.
    add_components: list[SwComponent] = field(default_factory=list)
    remove_components: list[str] = field(default_factory=list)
    override_components: list[Override] = field(default_factory=list)

    # Applies to ServiceManifest.instances on a target service manifest.
    add_services: list[ServiceInstance] = field(default_factory=list)
    remove_services: list[str] = field(default_factory=list)
    override_services: list[Override] = field(default_factory=list)

    # Whole-machine additions / removals (we don't field-override
    # machines yet — they're a flat hardware description).
    add_machines: list[MachineManifest] = field(default_factory=list)
    remove_machines: list[str] = field(default_factory=list)

    # Applies to Rig.execution_manifests. Override is the common case
    # (e.g. raise priority of phm, pin crypto to a core).
    add_executions: list[ExecutionManifest] = field(default_factory=list)
    remove_executions: list[str] = field(default_factory=list)
    override_executions: list[Override] = field(default_factory=list)

    # Applies to Rig.process_to_machine_mappings. Used by Macan and Tornado
    # to express CPU affinity (shall_run_on / shall_not_run_on).
    add_process_mappings: list[ProcessToMachineMapping] = field(
        default_factory=list
    )
    remove_process_mappings: list[str] = field(default_factory=list)
    override_process_mappings: list[Override] = field(default_factory=list)

    # Applies to Rig.node_to_cpu_mappings. Per-node (= per-thread)
    # affinity + POSIX scheduling policy. The intra-process counterpart
    # of process_mappings.
    add_node_mappings: list[NodeToCPUMapping] = field(
        default_factory=list
    )
    remove_node_mappings: list[str] = field(default_factory=list)
    override_node_mappings: list[Override] = field(default_factory=list)

    # Applies to Rig.supervisors — declarative supervisor tree.
    # Upper layers can :class:`Override` a SupervisorNode's strategy or
    # children list by name, e.g. to splice a vendor sub-supervisor in.
    add_supervisors: list[SupervisorNode] = field(default_factory=list)
    remove_supervisors: list[str] = field(default_factory=list)
    override_supervisors: list[Override] = field(default_factory=list)

    # Optional vehicle-identity patch. None = leave the base identity
    # untouched; otherwise this VehicleIdentity replaces the rig's.
    set_vehicle: VehicleIdentity | None = None

    # Which application this layer's component ops apply to. Default
    # picks the first application on the rig (matches our one-app rigs).
    target_application: str = ""

    # Which service manifest this layer's service ops apply to. Default
    # picks the first service manifest on the rig.
    target_service_manifest: str = ""

    # --- ops collation -----------------------------------------------------

    def component_ops(self) -> list[Op]:
        ops: list[Op] = []
        for c in self.add_components:
            ops.append(Insert(c))
        for name in self.remove_components:
            ops.append(Delete(name))
        ops.extend(self.override_components)
        return ops

    def service_ops(self) -> list[Op]:
        ops: list[Op] = []
        for s in self.add_services:
            ops.append(Insert(s))
        for name in self.remove_services:
            ops.append(Delete(name))
        ops.extend(self.override_services)
        return ops

    def machine_ops(self) -> list[Op]:
        ops: list[Op] = []
        for m in self.add_machines:
            ops.append(Insert(m))
        for name in self.remove_machines:
            ops.append(Delete(name))
        return ops

    def execution_ops(self) -> list[Op]:
        ops: list[Op] = []
        for e in self.add_executions:
            ops.append(Insert(e))
        for name in self.remove_executions:
            ops.append(Delete(name))
        ops.extend(self.override_executions)
        return ops

    def process_mapping_ops(self) -> list[Op]:
        ops: list[Op] = []
        for m in self.add_process_mappings:
            ops.append(Insert(m))
        for name in self.remove_process_mappings:
            ops.append(Delete(name))
        ops.extend(self.override_process_mappings)
        return ops

    def node_mapping_ops(self) -> list[Op]:
        ops: list[Op] = []
        for m in self.add_node_mappings:
            ops.append(Insert(m))
        for name in self.remove_node_mappings:
            ops.append(Delete(name))
        ops.extend(self.override_node_mappings)
        return ops

    def supervisor_ops(self) -> list[Op]:
        ops: list[Op] = []
        for s in self.add_supervisors:
            ops.append(Insert(s))
        for name in self.remove_supervisors:
            ops.append(Delete(name))
        ops.extend(self.override_supervisors)
        return ops


def apply_layer(rig: Rig, layer: Layer) -> Rig:
    """Apply one layer to a rig, returning a new rig."""

    new_vehicle = layer.set_vehicle if layer.set_vehicle is not None else rig.vehicle

    new_machines = apply_ops(rig.machines, layer.machine_ops())

    # Resolve which application receives the component ops.
    target_app = layer.target_application or (
        rig.applications[0].name if rig.applications else ""
    )
    new_apps: list[ApplicationManifest] = []
    component_ops = layer.component_ops()
    for app in rig.applications:
        if app.name == target_app and component_ops:
            new_apps.append(
                replace(app, components=apply_ops(app.components, component_ops))
            )
        else:
            new_apps.append(app)

    # Resolve which service manifest receives the service ops.
    target_svc = layer.target_service_manifest or (
        rig.service_manifests[0].name if rig.service_manifests else ""
    )
    new_svc_mans = []
    service_ops = layer.service_ops()
    for svc in rig.service_manifests:
        if svc.name == target_svc and service_ops:
            new_svc_mans.append(
                replace(svc, instances=apply_ops(svc.instances, service_ops))
            )
        else:
            new_svc_mans.append(svc)

    new_execs = apply_ops(rig.execution_manifests, layer.execution_ops())
    new_ptms = apply_ops(rig.process_to_machine_mappings, layer.process_mapping_ops())
    new_ntms = apply_ops(rig.node_to_cpu_mappings, layer.node_mapping_ops())
    new_supervisors = apply_ops(rig.supervisors, layer.supervisor_ops())

    return Rig(
        vehicle=new_vehicle,
        machines=new_machines,
        applications=new_apps,
        service_manifests=new_svc_mans,
        execution_manifests=new_execs,
        process_to_machine_mappings=new_ptms,
        node_to_cpu_mappings=new_ntms,
        supervisors=new_supervisors,
    )


def merge_layers(base: Rig, layers: Iterable[Layer]) -> Rig:
    """Apply a sequence of layers in order, returning the final rig."""
    result = base
    for layer in layers:
        result = apply_layer(result, layer)
    return result
