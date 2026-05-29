"""Semantic validators that run after a model is parsed.

textX gives us name-resolution for cross-references via `[Type|FQN]`. Here we
add whole-model invariants:
  - Field numbers in a message are unique and positive.
  - TIPC (type, instance) pairs are unique across nodes.
  - `connect A.x to B.y` wires compatible port direction + interface.
  - Param defaults match their declared types.
  - Gateway route bus refs resolve (declared or in the well-known gateway set).
  - Gateway route direction matches the bus kind (CAN spec ↔ CAN bus, etc).
  - Nested-composition refs resolve, are cycle-free, and don't introduce
    prototype-name collisions after flattening.
"""
from __future__ import annotations

from textx import TextXSemanticError

from .bus_catalog import WELL_KNOWN_GATEWAY_BUSES
from .flatten import flatten_composition


# The gateway itself sits at TIPC type=0x80010000, instance=0 (see
# theia/.../gw_proto.h: TIPC_GW_TYPE / TIPC_GW_INSTANCE). Reject any node
# declaration that collides with that well-known endpoint.
_GATEWAY_TIPC_TYPE = 0x80010000
_GATEWAY_TIPC_INSTANCE = 0


def _iter(model, cls_name: str):
    for el in getattr(model, "elements", []) or []:
        if el.__class__.__name__ == cls_name:
            yield el


def _parse_hex_or_int(s: str) -> int:
    return int(s, 16) if s.lower().startswith("0x") else int(s)


# ---- messages --------------------------------------------------------------

def _validate_messages(model):
    """Field numbers are assigned by the proto generator from declaration
    order — they are not part of the Artheia AST. We only validate that
    field *names* are unique within a message."""
    for msg in _iter(model, "MessageDecl"):
        seen: set[str] = set()
        for f in msg.fields:
            if f.name in seen:
                raise TextXSemanticError(
                    f"message {msg.name}: field name '{f.name}' declared twice",
                )
            seen.add(f.name)


# ---- enums ----------------------------------------------------------------

def _validate_enums(model):
    """Each enum's value names and numbers are unique. Negative numbers
    are accepted (proto3 allows them); 0 is fine (idiomatic default)."""
    for en in _iter(model, "EnumDecl"):
        seen_num: dict[int, str] = {}
        seen_name: set[str] = set()
        for v in en.values:
            if v.name in seen_name:
                raise TextXSemanticError(
                    f"enum {en.name}: value name '{v.name}' declared twice",
                )
            seen_name.add(v.name)
            if v.number in seen_num:
                raise TextXSemanticError(
                    f"enum {en.name}: value number {v.number} used by "
                    f"both '{seen_num[v.number]}' and '{v.name}'",
                )
            seen_num[v.number] = v.name


# ---- TIPC uniqueness -------------------------------------------------------

def _validate_tipc_unique(model):
    seen: dict[tuple[int, int], str] = {}
    for node in _iter(model, "NodeDecl"):
        # extern forward-decls carry no tipc (the real def, in an imported
        # package, owns the address). A non-extern node MAY also omit tipc
        # at this stage if it derives one via `prototype <Base>` — the
        # inheritance flatten (model/inherit.py) fills it before generators
        # run. Either way, nothing to check for a tipc-less node here.
        if getattr(node, "extern", False) or getattr(node, "tipc", None) is None:
            continue
        ttype = _parse_hex_or_int(node.tipc.type)
        tinst = _parse_hex_or_int(node.tipc.instance)
        if ttype == _GATEWAY_TIPC_TYPE and tinst == _GATEWAY_TIPC_INSTANCE:
            raise TextXSemanticError(
                f"node {node.name}: TIPC address "
                f"(type=0x{_GATEWAY_TIPC_TYPE:x}, instance={_GATEWAY_TIPC_INSTANCE}) "
                f"is reserved for the gateway itself (see gw_proto.h: "
                f"TIPC_GW_TYPE / TIPC_GW_INSTANCE). Pick a higher type."
            )
        key = (ttype, tinst)
        if key in seen:
            raise TextXSemanticError(
                f"node {node.name}: TIPC address (type={node.tipc.type}, "
                f"instance={node.tipc.instance}) already used by node "
                f"'{seen[key]}'",
            )
        seen[key] = node.name


# ---- connections -----------------------------------------------------------

_PORT_KIND = {
    "SenderPort":   ("out", "sr"),
    "ReceiverPort": ("in",  "sr"),
    "ServerPort":   ("out", "cs"),
    "ClientPort":   ("in",  "cs"),
}


def _port_kind(port):
    return _PORT_KIND[port.__class__.__name__]


def _resolve_port(proto, port_name: str):
    for p in getattr(proto.type, "ports", []) or []:
        if p.name == port_name:
            return p
    return None


def _validate_connections(model):
    for comp in _iter(model, "CompositionDecl"):
        if getattr(comp, "extern", False):
            continue  # forward-decl: real composition validated where defined
        # Flatten so connects inside nested compositions are validated
        # together with the parent's connects. Inner prototype names
        # appear verbatim in the flat list, so connects targeting them
        # resolve correctly.
        _, connects = flatten_composition(comp)
        for el in connects:
            src_port = _resolve_port(el.source.proto, el.source.port)
            tgt_port = _resolve_port(el.target.proto, el.target.port)

            if src_port is None:
                raise TextXSemanticError(
                    f"composition {comp.name}: prototype "
                    f"'{el.source.proto.name}' has no port '{el.source.port}'",
                )
            if tgt_port is None:
                raise TextXSemanticError(
                    f"composition {comp.name}: prototype "
                    f"'{el.target.proto.name}' has no port '{el.target.port}'",
                )

            src_dir, src_family = _port_kind(src_port)
            tgt_dir, tgt_family = _port_kind(tgt_port)

            if src_family != tgt_family:
                raise TextXSemanticError(
                    f"composition {comp.name}: connect "
                    f"{el.source.proto.name}.{el.source.port} -> "
                    f"{el.target.proto.name}.{el.target.port} mixes "
                    f"senderReceiver and clientServer ports",
                )

            if src_dir == tgt_dir:
                raise TextXSemanticError(
                    f"composition {comp.name}: connect "
                    f"{el.source.proto.name}.{el.source.port} -> "
                    f"{el.target.proto.name}.{el.target.port}: both ports are "
                    f"'{src_dir}' (need one provider and one requirer)",
                )

            if src_port.iface is not tgt_port.iface:
                raise TextXSemanticError(
                    f"composition {comp.name}: connect "
                    f"{el.source.proto.name}.{el.source.port} -> "
                    f"{el.target.proto.name}.{el.target.port}: interface "
                    f"mismatch ({src_port.iface.name} vs {tgt_port.iface.name})",
                )


# ---- composition refs ------------------------------------------------------

def _validate_composition_refs(model):
    """Cross-cutting checks on nested-composition references.

    Three failure modes, each reported with the offending composition
    name + reference name so the error message points at the .art line.
    """
    for comp in _iter(model, "CompositionDecl"):
        if getattr(comp, "extern", False):
            continue  # forward-decl: no body to check
        # Unresolved refs: textX leaves `.type` as None when a bare-name
        # reference can't be resolved (typically a missing `import`).
        for el in comp.elements:
            if el.__class__.__name__ != "CompositionRefDecl":
                continue
            if el.type is None:
                raise TextXSemanticError(
                    f"composition {comp.name}: cannot resolve "
                    f"`composition {el.name}` — referenced composition "
                    f"not in scope (missing `import <pkg>.*`?)"
                )

        # Cycles + flat-name collisions: flatten_composition raises on
        # cycles; we surface that as TextXSemanticError. Name collisions
        # are not raised by the helper (it preserves duplicates so callers
        # see them) — we walk the flat list here and check.
        try:
            prototypes, _ = flatten_composition(comp)
        except ValueError as exc:
            raise TextXSemanticError(
                f"composition {comp.name}: {exc}"
            ) from exc

        seen_proto: dict[str, str] = {}
        for p in prototypes:
            if p.name in seen_proto:
                raise TextXSemanticError(
                    f"composition {comp.name}: prototype name '{p.name}' "
                    f"appears twice after flattening (already used by "
                    f"prototype of type '{seen_proto[p.name]}')"
                )
            seen_proto[p.name] = p.type.name

        # Instance-name collision between a CompositionRefDecl's `name`
        # and a sibling PrototypeDecl's `name`. The instance name is
        # presentational today, but a clash misleads readers.
        ref_names = {
            el.name for el in comp.elements
            if el.__class__.__name__ == "CompositionRefDecl"
        }
        proto_names_at_parent = {
            el.name for el in comp.elements
            if el.__class__.__name__ == "PrototypeDecl"
        }
        clash = ref_names & proto_names_at_parent
        if clash:
            sample = next(iter(clash))
            raise TextXSemanticError(
                f"composition {comp.name}: name '{sample}' is used by "
                f"both a `prototype` and a `composition` reference"
            )


# ---- params ----------------------------------------------------------------

_PARAM_TYPE_TO_PY = {
    "int32":  int,
    "int64":  int,
    "uint32": int,
    "uint64": int,
    "float":  float,
    "double": float,
    "bool":   bool,
    "string": str,
}

_UINT_BITS = {"uint32": 32, "uint64": 64}
_INT_BITS  = {"int32": 32, "int64": 64}


def _coerce_param_literal(literal):
    v = literal.value
    if isinstance(v, str) and v in ("true", "false"):
        return v == "true"
    return v


def _validate_params(model):
    for node in _iter(model, "NodeDecl"):
        if getattr(node, "extern", False):
            continue  # forward-decl: no params block
        seen: set[str] = set()
        for p in getattr(node, "params", []) or []:
            if p.name in seen:
                raise TextXSemanticError(
                    f"node {node.name}: duplicate parameter '{p.name}'"
                )
            seen.add(p.name)

            val = _coerce_param_literal(p.default)
            expected = _PARAM_TYPE_TO_PY[p.type]

            # bool is a subclass of int — check it first.
            if p.type == "bool":
                if not isinstance(val, bool):
                    raise TextXSemanticError(
                        f"node {node.name}: parameter '{p.name}' is bool but "
                        f"default {val!r} is not 'true' or 'false'"
                    )
                continue

            if p.type == "string":
                if not isinstance(val, str) or isinstance(val, bool):
                    raise TextXSemanticError(
                        f"node {node.name}: parameter '{p.name}' is string but "
                        f"default {val!r} is not a string literal"
                    )
                continue

            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise TextXSemanticError(
                    f"node {node.name}: parameter '{p.name}' has incompatible "
                    f"default {val!r} for type {p.type}"
                )

            if expected is int and not float(val).is_integer():
                raise TextXSemanticError(
                    f"node {node.name}: parameter '{p.name}' is {p.type} but "
                    f"default {val!r} is fractional"
                )

            ival = int(val)
            if p.type in _UINT_BITS:
                bits = _UINT_BITS[p.type]
                if ival < 0 or ival >= (1 << bits):
                    raise TextXSemanticError(
                        f"node {node.name}: parameter '{p.name}' default {val!r} "
                        f"is out of range for {p.type}"
                    )
            elif p.type in _INT_BITS:
                bits = _INT_BITS[p.type]
                lim = 1 << (bits - 1)
                if ival < -lim or ival >= lim:
                    raise TextXSemanticError(
                        f"node {node.name}: parameter '{p.name}' default {val!r} "
                        f"is out of range for {p.type}"
                    )


# ---- gateway routes --------------------------------------------------------

def _declared_buses(model) -> dict[str, str]:
    return {b.name: b.kind for b in _iter(model, "BusDecl")}


def _resolve_bus_kind(name: str, declared: dict[str, str]) -> str:
    """Return the bus kind ('can'|'flexray') or raise."""
    if name in declared:
        return declared[name]
    if name in WELL_KNOWN_GATEWAY_BUSES:
        return WELL_KNOWN_GATEWAY_BUSES[name]
    raise TextXSemanticError(
        f"gateway_route references unknown bus '{name}' "
        f"(no `bus {name} kind=...` declaration, and not a well-known "
        f"gateway bus). Add a `bus` declaration or use one of: "
        f"{', '.join(sorted(WELL_KNOWN_GATEWAY_BUSES))}"
    )


def _validate_gateway_routes(model):
    declared = _declared_buses(model)
    for route in _iter(model, "GatewayRouteDecl"):
        spec_kind = route.spec.__class__.__name__

        # `signal=Foo` form: bus + address get resolved later via the catalog,
        # nothing to check at parse time beyond textX having already resolved
        # the message cross-reference.
        if spec_kind == "SignalRouteSpec":
            continue

        bus_name = route.spec.bus
        bus_kind = _resolve_bus_kind(bus_name, declared)

        if spec_kind == "CanRouteSpec" and bus_kind != "can":
            raise TextXSemanticError(
                f"gateway_route {route.node.name}: CAN spec references bus "
                f"'{bus_name}' but that bus is kind={bus_kind}"
            )
        if spec_kind == "FlexRayRouteSpec" and bus_kind != "flexray":
            raise TextXSemanticError(
                f"gateway_route {route.node.name}: FlexRay spec references bus "
                f"'{bus_name}' but that bus is kind={bus_kind}"
            )


# ---- entry point -----------------------------------------------------------

def _validate_extern_bodies(model):
    """An `extern` decl is a forward declaration — it MUST have an empty
    body. A non-empty body means it's a real definition, where `extern` is
    a mistake (the empty-body-is-magic heuristic was retired in favour of
    explicit `extern`)."""
    for el in getattr(model, "elements", []) or []:
        if not getattr(el, "extern", False):
            continue
        kind = el.__class__.__name__
        nonempty = None
        if kind == "NodeDecl":
            # textX leaves an absent optional list attr as [] (not None) and
            # an absent optional object attr as None. Only a *populated*
            # body contradicts `extern`. (A bare `extern node X { }` has
            # tipc=None, ports=[], params=[], statem=None.)
            if getattr(el, "tipc", None) is not None:
                nonempty = "a tipc address"
            elif list(getattr(el, "ports", []) or []):
                nonempty = "a ports block"
            elif list(getattr(el, "params", []) or []):
                nonempty = "a params block"
            elif getattr(el, "statem", None) is not None:
                nonempty = "a statem block"
        elif kind in ("CompositionDecl", "ClusterDecl"):
            if list(getattr(el, "elements", []) or []):
                nonempty = "members"
        elif kind == "SenderReceiverInterface":
            if list(getattr(el, "data", []) or []):
                nonempty = "data elements"
        elif kind == "ClientServerInterface":
            if list(getattr(el, "operations", []) or []):
                nonempty = "operations"
        if nonempty is not None:
            raise TextXSemanticError(
                f"extern {el.name}: a forward declaration must have an empty "
                f"body, but this one declares {nonempty}. Drop `extern` to "
                f"make it a real definition, or empty the body to keep it a "
                f"forward declaration."
            )


def _on_model(model, metamodel):
    _validate_messages(model)
    _validate_enums(model)
    _validate_extern_bodies(model)
    _validate_tipc_unique(model)
    # Composition refs first — cycle / collision errors are more
    # informative than the downstream connect/codegen failures they'd
    # otherwise trigger.
    _validate_composition_refs(model)
    _validate_connections(model)
    _validate_params(model)
    _validate_gateway_routes(model)


def register_validators(mm):
    mm.register_model_processor(_on_model)
