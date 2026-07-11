"""Orthogonal-ARA manifest model (the manifest-algebra engine).

A runtime is described by a small set of *orthogonal* manifest axes — each
answering one independent question (WHAT processes / HOW they talk / WHERE they
live / which Adaptive-Application bundles them). They compose as monoids on
:mod:`artheia.manifest.algebra` and materialize via :meth:`DeploymentLayer.simplify`.

Modules:

- :mod:`.algebra` — the layer-merge engine: :class:`ConfigField`
  (Undefined/Default/Explicit/Defer), :class:`Layer` / :class:`Identifiable`
  / :class:`MonoidSet`, the ``Append``/``Remove`` set edits, and
  :func:`simplify` / :func:`validate`.
- :mod:`.deployment` — the four orthogonal axes (Execution / Service / Machine /
  Application) and :class:`DeploymentLayer` / :class:`DeploymentTarget`.
- :mod:`.supervisor` — the declarative OTP-style supervisor dataclasses
  (:class:`SupervisorNode` etc.) the executor.py sidecars author.

(The parsed-AST → :class:`StateMSpec` lowering moved to
:mod:`artheia.generators.statem` — it's a codegen concern, not a deployment
manifest, and its sole user is ``gen-app``.)

See ``docs/autosar/manifest.md`` for the conceptual model.
"""

from artheia.manifest.algebra import (  # noqa: F401
    Append,
    ConfigField,
    Default,
    Defer,
    EmptySet,
    Explicit,
    Identifiable,
    Issue,
    Layer,
    MonoidSet,
    Remove,
    Undefined,
    empty_set,
    identifiable_dataclass,
    validate,
)
from artheia.manifest.deployment import (  # noqa: F401
    ApplicationLayer,
    ApplicationSetLayer,
    ApplicationSetTarget,
    ApplicationTarget,
    DeploymentLayer,
    DeploymentTarget,
    ExecutionLayer,
    ExecutionTarget,
    MachineLayer,
    MachineSetLayer,
    MachineSetTarget,
    MachineTarget,
    ProcessLayer,
    ProcessTarget,
    ServiceInstanceLayer,
    ServiceInstanceTarget,
    ServiceLayer,
    ServiceTarget,
)
from artheia.manifest.supervisor import (  # noqa: F401
    RestartStrategy,
    SupervisorNode,
)

__all__ = [
    # algebra
    "Append",
    "ConfigField",
    "Default",
    "Defer",
    "EmptySet",
    "Explicit",
    "Identifiable",
    "Issue",
    "Layer",
    "MonoidSet",
    "Remove",
    "Undefined",
    "empty_set",
    "identifiable_dataclass",
    "validate",
    # deployment axes
    "ApplicationLayer",
    "ApplicationSetLayer",
    "ApplicationSetTarget",
    "ApplicationTarget",
    "DeploymentLayer",
    "DeploymentTarget",
    "ExecutionLayer",
    "ExecutionTarget",
    "MachineLayer",
    "MachineSetLayer",
    "MachineSetTarget",
    "MachineTarget",
    "ProcessLayer",
    "ProcessTarget",
    "ServiceInstanceLayer",
    "ServiceInstanceTarget",
    "ServiceLayer",
    "ServiceTarget",
    # supervisor
    "RestartStrategy",
    "SupervisorNode",
]
