"""Semantic validators that run after a model is parsed.

textX gives us name-resolution for cross-references via `[Type|FQN]`. Here we
add whole-model invariants:
  - Field numbers in a message are unique and positive.
  - TIPC (type, instance) pairs are unique across nodes.
  - `connect A.x to B.y` wires compatible port direction + interface.
  - Param defaults match their declared types.
  - Gateway route bus refs resolve (declared or in the well-known gateway set).
  - Gateway route direction matches the bus kind (CAN spec ↔ CAN bus, etc).
"""
from __future__ import annotations

from textx import TextXSemanticError

from .bus_catalog import WELL_KNOWN_GATEWAY_BUSES


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
    for msg in _iter(model, "MessageDecl"):
        seen: dict[int, str] = {}
        for f in msg.fields:
            if f.number <= 0:
                raise TextXSemanticError(
                    f"message {msg.name}: field {f.name} has non-positive number {f.number}",
                )
            if f.number in seen:
                raise TextXSemanticError(
                    f"message {msg.name}: field number {f.number} used by "
                    f"both '{seen[f.number]}' and '{f.name}'",
                )
            seen[f.number] = f.name


# ---- TIPC uniqueness -------------------------------------------------------

def _validate_tipc_unique(model):
    seen: dict[tuple[int, int], str] = {}
    for node in _iter(model, "NodeDecl"):
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
        for el in comp.elements:
            if el.__class__.__name__ != "ConnectDecl":
                continue

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

def _on_model(model, metamodel):
    _validate_messages(model)
    _validate_tipc_unique(model)
    _validate_connections(model)
    _validate_params(model)
    _validate_gateway_routes(model)


def register_validators(mm):
    mm.register_model_processor(_on_model)
