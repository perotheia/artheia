"""Netgraph generator.

Given a parsed Artheia model (and optionally a gateway catalog), emit a JSON
document the runtime uses to route TIPC traffic between nodes plus describe
which nodes terminate vehicle-bus signals via the gateway.

Each node entry can carry `gateway_routes: [...]` whose entries match the
GwCanMeta / GwFlexRayMeta layout from gw_proto.h. Three input forms:

  1. `gateway_route N { signal=Foo direction=in }` — catalog-driven; bus and
      address are filled in from the catalog by message name.
  2. `gateway_route N { can id=0x42 bus=kcan ... direction=in }` — explicit
      CAN; nothing to look up.
  3. `gateway_route N { flexray slot=15 bus=mlbevo_gen2_a ... direction=in }`
      — explicit FlexRay; same.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional


_PORT_KIND = {
    "SenderPort":   ("out", "senderReceiver"),
    "ReceiverPort": ("in",  "senderReceiver"),
    "ServerPort":   ("out", "clientServer"),
    "ClientPort":   ("in",  "clientServer"),
}


def _iter(model, cls: str):
    for el in model.elements:
        if el.__class__.__name__ == cls:
            yield el


def _iface_messages(iface) -> list[str]:
    if iface.__class__.__name__ == "SenderReceiverInterface":
        return [d.type.name for d in iface.data]
    out: list[str] = []
    for op in iface.operations:
        out.extend(p.type.name for p in op.params)
        if op.returns is not None:
            out.append(op.returns.name)
    seen: set[str] = set()
    deduped: list[str] = []
    for m in out:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


def _node_dict(node) -> dict:
    ports = []
    for p in node.ports or []:
        direction, family = _PORT_KIND[p.__class__.__name__]
        ports.append({
            "name": p.name,
            "direction": direction,
            "family": family,
            "interface": p.iface.name,
            "messages": _iface_messages(p.iface),
        })
    return {
        "name": node.name,
        "tipc": {"type": node.tipc.type, "instance": node.tipc.instance},
        "ports": ports,
    }


def _port_on_node(node, port_name: str):
    for p in node.ports or []:
        if p.name == port_name:
            return p
    return None


def _connection_dict(conn) -> dict:
    src_port = _port_on_node(conn.source.proto.type, conn.source.port)
    return {
        "source": {"prototype": conn.source.proto.name, "port": conn.source.port},
        "target": {"prototype": conn.target.proto.name, "port": conn.target.port},
        "interface": src_port.iface.name,
        "messages": _iface_messages(src_port.iface),
    }


def _composition_dict(comp) -> dict:
    prototypes = []
    connections = []
    for el in comp.elements:
        if el.__class__.__name__ == "PrototypeDecl":
            prototypes.append({
                "name": el.name,
                "node": el.type.name,
                "tipc": {"type": el.type.tipc.type, "instance": el.type.tipc.instance},
            })
        else:
            connections.append(_connection_dict(el))
    return {"name": comp.name, "prototypes": prototypes, "connections": connections}


def _parse_hex_or_int(s: str) -> int:
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _can_meta(spec, *, catalog_entry: Optional[dict] = None) -> dict:
    """Build a CAN metadata dict matching GwCanMeta field names."""
    out: dict = {}
    if catalog_entry:
        out["can_id"] = catalog_entry["can_id"]
        out["bus"] = catalog_entry["bus"]
        if "channel_idx" in catalog_entry:
            out["channel_idx"] = catalog_entry["channel_idx"]
        if "dlc" in catalog_entry:
            out["dlc"] = catalog_entry["dlc"]
        return out

    out["can_id"] = _parse_hex_or_int(spec.can_id)
    out["bus"] = spec.bus
    if spec.channel_idx is not None and spec.channel_idx != 0:
        out["channel_idx"] = spec.channel_idx
    if spec.dlc is not None and spec.dlc != 0:
        out["dlc"] = spec.dlc
    if spec.extended_id:
        out["extended_id"] = spec.extended_id == "true"
    if spec.rtr:
        out["rtr"] = spec.rtr == "true"
    return out


def _flexray_meta(spec, *, catalog_entry: Optional[dict] = None) -> dict:
    """Build a FlexRay metadata dict matching GwFlexRayMeta field names."""
    if catalog_entry:
        return {
            "slot_id":     catalog_entry["slot_id"],
            "bus":         catalog_entry["bus"],
            "channel":     catalog_entry.get("channel", "A"),
            "cycle":       catalog_entry.get("cycle", 0),
            "pdu_offset":  catalog_entry.get("pdu_offset", 0),
        }
    out = {"slot_id": spec.slot_id, "bus": spec.bus}
    if spec.channel:
        out["channel"] = spec.channel
    if spec.cycle is not None and spec.cycle != 0:
        out["cycle"] = spec.cycle
    if spec.pdu_offset is not None and spec.pdu_offset != 0:
        out["pdu_offset"] = spec.pdu_offset
    return out


def _route_dict(route, catalog: dict | None) -> dict:
    spec = route.spec
    kind = spec.__class__.__name__
    direction = route.direction.value

    if kind == "SignalRouteSpec":
        msg_name = spec.message.name
        entry = (catalog or {}).get("messages", {}).get(msg_name)
        if entry is None:
            return {
                "form": "signal",
                "signal": msg_name,
                "direction": direction,
                "unresolved": True,
            }
        bus_kind = entry.get("bus_kind", "can")
        if bus_kind == "can":
            return {
                "form": "signal",
                "signal": msg_name,
                "direction": direction,
                "can": _can_meta(spec=None, catalog_entry=entry),
            }
        return {
            "form": "signal",
            "signal": msg_name,
            "direction": direction,
            "flexray": _flexray_meta(spec=None, catalog_entry=entry),
        }

    if kind == "CanRouteSpec":
        return {"form": "can", "direction": direction, "can": _can_meta(spec)}

    if kind == "FlexRayRouteSpec":
        return {"form": "flexray", "direction": direction, "flexray": _flexray_meta(spec)}

    raise AssertionError(f"unhandled gateway route spec: {kind}")


def _routes_by_node(model, catalog: dict | None) -> dict[str, list[dict]]:
    by_node: dict[str, list[dict]] = {}
    for route in _iter(model, "GatewayRouteDecl"):
        by_node.setdefault(route.node.name, []).append(_route_dict(route, catalog))
    return by_node


def build_netgraph(model, catalog: dict | None = None) -> dict:
    routes = _routes_by_node(model, catalog)
    nodes = []
    for n in _iter(model, "NodeDecl"):
        d = _node_dict(n)
        if routes.get(n.name):
            d["gateway_routes"] = routes[n.name]
        nodes.append(d)
    return {
        "package": model.name or "",
        "nodes": nodes,
        "compositions": [_composition_dict(c) for c in _iter(model, "CompositionDecl")],
    }


def generate_netgraph(model, out_file: str | Path, *, catalog: dict | None = None) -> Path:
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(build_netgraph(model, catalog=catalog), indent=2) + "\n")
    return out_file
