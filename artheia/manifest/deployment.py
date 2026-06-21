"""Orthogonal ARA manifests, composed into one Deployment.

Per ``docs/autosar/manifest.md`` a runtime is described end-to-end by a small
set of *orthogonal* manifest kinds — each answering one independent question:

- **ExecutionManifest** — WHAT processes exist (lifecycle, CPU / scheduling /
  memory, which Function-Group states they run in). Consumed by Execution Mgmt.
- **ServiceManifest** — HOW they talk (interface + version + instance id,
  transport binding + endpoint, discovery, QoS). Consumed by Communication Mgmt.
- **MachineManifest** — WHERE they live (machine states + FG composition, NICs,
  OS resources, time base, resource groups). Per-machine platform settings.
- **ApplicationManifest** — the Adaptive-Application grouping (which components +
  executables + service endpoints make up a deployable app).

These are ORTHOGONAL: each composes on its own axis. A :class:`Deployment` is
the product of the four; ``combine`` folds each axis independently
(``exec ⊕ exec``, ``service ⊕ service``, …) with no cross-axis coupling — that
is what makes layering predictable. Cross-axis CONSISTENCY (a service's process
exists in execution; an execution process maps to a declared machine; a CPU
affinity references a core that machine actually has) is checked by
:meth:`Deployment._invariants` over the UNMATERIALIZED product, before
``simplify`` serializes it to JSON.

Each manifest kind is a Layer/Target pair: the ``*Layer`` carries
:class:`~artheia.manifest.algebra.ConfigField`-wrapped fields and composes; its
frozen ``*Target`` is the materialized output. ``simplify`` walks Layer → Target.

This is the clean-break successor to ``applicative.py`` + the per-kind
dataclasses; it models the manifests directly as monoids on
:mod:`artheia.manifest.algebra`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from artheia.manifest.algebra import (
    ConfigField,
    Default,
    EmptySet,
    Explicit,
    Identifiable,
    Issue,
    Layer,
    Undefined,
    empty_set,
    fold_transforms,
    identifiable_dataclass,
)


# =====================================================================
# Execution axis — WHAT processes exist.
# =====================================================================

@identifiable_dataclass
class ProcessLayer(Identifiable):
    """One process Execution Management starts. Identity = ``name``."""
    name: str
    executable: ConfigField = field(default_factory=Undefined)   # bazel/binary ref
    start_cmd: ConfigField = field(default_factory=Undefined)    # e.g. "bin/<name>"
    function_group: ConfigField = field(default_factory=Undefined)  # FG it belongs to
    fg_states: object = field(default_factory=empty_set)              # FG states it runs in
    cpu_affinity: object = field(default_factory=empty_set)           # core ids (ints)
    scheduling: ConfigField = field(default_factory=lambda: Default("OTHER"))
    priority: ConfigField = field(default_factory=lambda: Default(0))
    mem_limit_bytes: ConfigField = field(default_factory=lambda: Default(None))
    machine: ConfigField = field(default_factory=Undefined)     # target machine name
    depends_on: object = field(default_factory=empty_set)            # process names

    @property
    def _resolver(self):
        return ProcessTarget


@dataclass(frozen=True)
class ProcessTarget:
    name: str
    executable: str
    start_cmd: str
    function_group: str
    fg_states: frozenset
    cpu_affinity: frozenset
    scheduling: str
    priority: int
    mem_limit_bytes: object
    machine: str
    depends_on: frozenset


@identifiable_dataclass
class ExecutionLayer(Identifiable):
    """The execution axis: the set of processes. Identity = ``name`` (the
    manifest name, usually the machine or app it scopes)."""
    name: str = "execution"
    processes: object = field(default_factory=empty_set)   # set[ProcessLayer] | edits | Undefined

    @property
    def _resolver(self):
        return ExecutionTarget


@dataclass(frozen=True)
class ExecutionTarget:
    name: str
    processes: frozenset


# =====================================================================
# Service axis — HOW they talk.
# =====================================================================

@identifiable_dataclass
class ServiceInstanceLayer(Identifiable):
    """One SOA service instance. Identity = ``name``."""
    name: str
    interface: ConfigField = field(default_factory=Undefined)   # interface fqn
    version: ConfigField = field(default_factory=lambda: Default("1.0"))
    instance_id: ConfigField = field(default_factory=lambda: Default(None))
    binding: ConfigField = field(default_factory=lambda: Default("tipc"))  # tipc|someip|dds|ipc
    endpoint: ConfigField = field(default_factory=lambda: Default(None))    # transport endpoint
    provided_by: ConfigField = field(default_factory=Undefined)  # process name that offers it

    @property
    def _resolver(self):
        return ServiceInstanceTarget


@dataclass(frozen=True)
class ServiceInstanceTarget:
    name: str
    interface: str
    version: str
    instance_id: object
    binding: str
    endpoint: object
    provided_by: object


@identifiable_dataclass
class ServiceLayer(Identifiable):
    name: str = "service"
    instances: object = field(default_factory=empty_set)   # set[ServiceInstanceLayer] | edits

    @property
    def _resolver(self):
        return ServiceTarget


@dataclass(frozen=True)
class ServiceTarget:
    name: str
    instances: frozenset


# =====================================================================
# Machine axis — WHERE they live.
# =====================================================================

@identifiable_dataclass
class MachineLayer(Identifiable):
    """One ECU / VM. Identity = ``name``. ``cores`` is the set of core ids the
    Processor exposes (an Execution cpu_affinity must reference one of these)."""
    name: str
    arch: ConfigField = field(default_factory=lambda: Default("aarch64"))
    cores: object = field(default_factory=empty_set)           # set[int] core ids
    machine_states: object = field(default_factory=empty_set)  # FG composition per state
    network_interfaces: object = field(default_factory=empty_set)
    os_packages: object = field(default_factory=empty_set)
    time_base: ConfigField = field(default_factory=lambda: Default("monotonic"))
    # GUI com endpoint (address, port) the supervisor-GUI opens a gRPC channel
    # to. Optional: None → the GUI manifest defaults to 127.0.0.1:7700. Carried
    # as a plain (address, port) tuple so the orthogonal axes stay decoupled.
    com_endpoint: ConfigField = field(default_factory=lambda: Default(None))

    @property
    def _resolver(self):
        return MachineTarget


@dataclass(frozen=True)
class MachineTarget:
    name: str
    arch: str
    cores: frozenset
    machine_states: frozenset
    network_interfaces: frozenset
    os_packages: frozenset
    time_base: str
    com_endpoint: object = None


@identifiable_dataclass
class MachineSetLayer(Identifiable):
    name: str = "machine"
    machines: object = field(default_factory=empty_set)   # set[MachineLayer] | edits

    @property
    def _resolver(self):
        return MachineSetTarget


@dataclass(frozen=True)
class MachineSetTarget:
    name: str
    machines: frozenset


# =====================================================================
# Application axis — the AA grouping (design-time deployment unit).
# =====================================================================

@identifiable_dataclass
class ApplicationLayer(Identifiable):
    """An Adaptive Application: a named group of processes + the host it
    targets. Identity = ``name``."""
    name: str
    host_machine: ConfigField = field(default_factory=Undefined)
    processes: object = field(default_factory=empty_set)   # process names (str) it bundles

    @property
    def _resolver(self):
        return ApplicationTarget


@dataclass(frozen=True)
class ApplicationTarget:
    name: str
    host_machine: object
    processes: frozenset


@identifiable_dataclass
class ApplicationSetLayer(Identifiable):
    name: str = "application"
    applications: object = field(default_factory=empty_set)

    @property
    def _resolver(self):
        return ApplicationSetTarget


@dataclass(frozen=True)
class ApplicationSetTarget:
    name: str
    applications: frozenset


# =====================================================================
# Deployment — the orthogonal product of the four axes.
# =====================================================================

@dataclass
class DeploymentLayer(Layer):
    """The whole runtime: the product of the four orthogonal manifest axes.

    ``combine`` folds each axis independently (it inherits Layer's field-wise
    walk; every field is itself a Layer, so they recurse). ``validate`` runs the
    per-axis checks (via the algebra) PLUS the cross-axis invariants below."""
    execution: ExecutionLayer = field(default_factory=ExecutionLayer)
    service: ServiceLayer = field(default_factory=ServiceLayer)
    machines: MachineSetLayer = field(default_factory=MachineSetLayer)
    applications: ApplicationSetLayer = field(default_factory=ApplicationSetLayer)

    @property
    def _resolver(self):
        return DeploymentTarget

    # -- cross-axis consistency (run on the UNMATERIALIZED product) ---------

    def _invariants(self, context: str) -> list[Issue]:
        issues: list[Issue] = []

        procs = {p.name: p for p in _members(self.execution.processes)}
        machines = {m.name: m for m in _members(self.machines.machines)}

        # 1. Every execution process maps to a declared machine, and its CPU
        #    affinity references cores that machine actually exposes.
        for pname, p in procs.items():
            mname = _value(p.machine)
            if mname is None:
                continue  # Undefined machine is caught by the per-field check
            if mname not in machines:
                issues.append(Issue(
                    f"{context}.execution.processes[{pname}].machine",
                    f"process maps to machine {mname!r} not declared in machines axis",
                ))
                continue
            cores = _members(machines[mname].cores)
            for core in _members(p.cpu_affinity):
                if core not in cores:
                    issues.append(Issue(
                        f"{context}.execution.processes[{pname}].cpu_affinity",
                        f"affinity core {core!r} absent on machine {mname!r} "
                        f"(has {sorted(cores)})",
                    ))

        # 2. Every service instance is provided by a process that exists.
        for s in _members(self.service.instances):
            owner = _value(s.provided_by)
            if owner is not None and owner not in procs:
                issues.append(Issue(
                    f"{context}.service.instances[{s.name}].provided_by",
                    f"service provided by process {owner!r} not in execution axis",
                ))

        # 3. Every application's host + bundled processes resolve.
        for a in _members(self.applications.applications):
            host = _value(a.host_machine)
            if host is not None and host not in machines:
                issues.append(Issue(
                    f"{context}.applications[{a.name}].host_machine",
                    f"app host {host!r} not a declared machine",
                ))
            for pn in _members(a.processes):
                if pn not in procs:
                    issues.append(Issue(
                        f"{context}.applications[{a.name}].processes",
                        f"bundled process {pn!r} not in execution axis",
                    ))

        # 4. process depends_on references resolve.
        for pname, p in procs.items():
            for dep in _members(p.depends_on):
                if dep not in procs:
                    issues.append(Issue(
                        f"{context}.execution.processes[{pname}].depends_on",
                        f"depends on process {dep!r} not in execution axis",
                    ))
        return issues


@dataclass(frozen=True)
class DeploymentTarget:
    execution: ExecutionTarget
    service: ServiceTarget
    machines: MachineSetTarget
    applications: ApplicationSetTarget


# =====================================================================
# helpers
# =====================================================================

def _members(value: object) -> set:
    """The plain members of a set field, folding any Append/Remove edits and
    treating EmptySet/Undefined as empty. Safe on a still-unmaterialized field."""
    if isinstance(value, (EmptySet, Undefined)):
        return set()
    if isinstance(value, (set, frozenset)):
        return fold_transforms(value)
    return set()


def _value(cf: object):
    """The concrete value behind a ConfigField (Explicit/Default), or None for
    Undefined/Defer/other — for invariant checks that must not raise."""
    if isinstance(cf, Explicit):
        return cf.value
    if isinstance(cf, Default):
        return cf.default
    return None


__all__ = [
    "ProcessLayer", "ProcessTarget",
    "ExecutionLayer", "ExecutionTarget",
    "ServiceInstanceLayer", "ServiceInstanceTarget",
    "ServiceLayer", "ServiceTarget",
    "MachineLayer", "MachineTarget",
    "MachineSetLayer", "MachineSetTarget",
    "ApplicationLayer", "ApplicationTarget",
    "ApplicationSetLayer", "ApplicationSetTarget",
    "DeploymentLayer", "DeploymentTarget",
]
