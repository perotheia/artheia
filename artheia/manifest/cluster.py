"""Cluster — deployment / packaging unit.

A :class:`Cluster` is a bundle of compositions deployed together.
Maps to AUTOSAR's *Distribution* (a multi-machine tar bundle); each
:class:`ClusterMember` inside maps to one installable package
(.ipk / .deb).

Abstraction ladder::

    node         = thread          (one GenServer instance)
    composition  = executable      (one process; prototypes + connects)
    cluster      = package bundle  (distribution; compositions + deploy attrs)

The grammar (``artheia.tx ClusterDecl``) is minimal — just a list of
composition references. Deploy-time attributes (per-member package
name override, target machine kind, etc.) live HERE in Python so
``rig.py`` can do the actual machine binding without polluting the
``.art`` source.

A cluster is **machine-agnostic** at the .art level. The mapping
``cluster -> machine`` is established by rig.py (a vehicle integrator
decides which clusters land on which machines).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from artheia.manifest.machine import MachineKind


@dataclass(frozen=True)
class ClusterMember:
    """One composition inside a cluster.

    :attr instance_name:    The cluster-local handle (e.g. ``services``
                            in ``composition system.services.Services services``).
                            Defaults to the package name when no
                            override is set.
    :attr composition_fqn:  Fully-qualified composition reference, e.g.
                            ``system.services.Services``.
    :attr package_name:     opkg/deb package name (.ipk / .deb basename).
                            Defaults to ``instance_name``.
    :attr machine_kind:     Optional pin — when set, this member only
                            deploys to machines of this kind. Default
                            ``None`` means the member is portable and
                            rig.py decides.
    """

    instance_name: str
    composition_fqn: str
    package_name: Optional[str] = None
    machine_kind: Optional[MachineKind] = None

    @property
    def effective_package_name(self) -> str:
        """The .ipk/.deb basename for this member.

        ``package_name`` takes precedence; falls back to
        ``instance_name``.
        """
        return self.package_name or self.instance_name


@dataclass(frozen=True)
class ClusterPort:
    """2-level port reference inside a cluster: ``proto.port``.

    The prototype name is globally unique across the cluster (textX
    scope), and each prototype is one node = one TIPC endpoint, so
    a 2-level reference is enough — no need to qualify with the
    member composition.
    """

    proto: str
    port: str


@dataclass(frozen=True)
class ClusterConnect:
    """A cross-composition wire inside a cluster.

    The cluster owns the inter-process topology: cluster connects are
    EXTERNAL messages (TIPC across processes). Composition-internal
    connects are INTERNAL actor-mailbox messages.
    """

    source: ClusterPort
    target: ClusterPort


@dataclass(frozen=True)
class Cluster:
    """A deployment bundle of compositions.

    :attr name:      The cluster name from .art (e.g. ``Platform``).
                     Used as the distribution tar prefix —
                     ``<name>_<machine>.tar``.
    :attr members:   Compositions packaged together.
    :attr connects:  Cross-composition wires (cluster-owned topology).

    Construct from a parsed .art file via :func:`cluster_from_ast`.
    """

    name: str
    members: tuple[ClusterMember, ...] = field(default_factory=tuple)
    connects: tuple[ClusterConnect, ...] = field(default_factory=tuple)

    def member(self, instance_name: str) -> ClusterMember:
        """Look up a member by its cluster-local handle.

        Raises :class:`KeyError` if the handle is unknown.
        """
        for m in self.members:
            if m.instance_name == instance_name:
                return m
        raise KeyError(
            f"cluster {self.name!r} has no member {instance_name!r}; "
            f"members: {[m.instance_name for m in self.members]}"
        )


def cluster_from_ast(node) -> Cluster:
    """Build a :class:`Cluster` from a parsed textX ``ClusterDecl``.

    The textX ``ClusterMember`` carries no per-member attributes
    today (grammar is minimal). Attributes (``package_name``,
    ``machine_kind``) come from the Python side — either the default
    fallback (``effective_package_name = instance_name``) or by
    constructing :class:`ClusterMember` directly in rig.py.

    The cluster body is a heterogeneous list of ``ClusterMember`` and
    ``ClusterConnect`` AST nodes; we split them here.
    """
    if type(node).__name__ != "ClusterDecl":
        raise TypeError(
            f"expected ClusterDecl, got {type(node).__name__}"
        )
    members = tuple(
        ClusterMember(
            instance_name=el.name,
            composition_fqn=_qualify(el.type),
        )
        for el in node.elements
        if type(el).__name__ == "ClusterMember"
    )
    connects = tuple(
        ClusterConnect(
            source=ClusterPort(
                proto=el.source.proto.name,
                port=el.source.port,
            ),
            target=ClusterPort(
                proto=el.target.proto.name,
                port=el.target.port,
            ),
        )
        for el in node.elements
        if type(el).__name__ == "ClusterConnect"
    )
    return Cluster(name=node.name, members=members, connects=connects)


def _qualify(composition_decl) -> str:
    """Reconstruct the FQN of a composition reference.

    textX resolves cross-references to the target ``CompositionDecl``
    object; the human FQN is ``<package>.<name>``. The package lives
    on the parent ``Model``.
    """
    name = composition_decl.name
    # Walk up to the parent Model to find the package.
    parent = getattr(composition_decl, "parent", None)
    while parent is not None and type(parent).__name__ != "Model":
        parent = getattr(parent, "parent", None)
    if parent is None or not getattr(parent, "name", None):
        return name
    return f"{parent.name}.{name}"
