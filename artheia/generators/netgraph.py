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
    # Flatten nested-composition refs: an inner `composition Foo bar`
    # contributes its prototypes + connects verbatim. Inner names appear
    # at parent scope (no instance-prefixing — see model/flatten.py).
    from artheia.model import flatten_composition
    proto_decls, connect_decls = flatten_composition(comp)
    prototypes = [
        {
            "name": el.name,
            "node": el.type.name,
            "tipc": {"type": el.type.tipc.type, "instance": el.type.tipc.instance},
        }
        for el in proto_decls
    ]
    connections = [_connection_dict(el) for el in connect_decls]
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


def _walk_connects(model) -> Iterable:
    """Yield every ConnectDecl in the model, regardless of which
    composition or cluster scope it sits in.

    Cluster ConnectDecls are externally-routed (TIPC across processes);
    composition ConnectDecls are internally-routed (in-process mailbox).
    For the netgraph LUT both shapes resolve to the same `signal →
    destination_tipc_address` answer — the TIPC kernel routes a
    cast() transparently regardless. Apps don't see the difference.
    """
    from artheia.model import flatten_composition
    for comp in _iter(model, "CompositionDecl"):
        _, connect_decls = flatten_composition(comp)
        for c in connect_decls:
            yield c
    for cluster in _iter(model, "ClusterDecl"):
        for el in getattr(cluster, "elements", []) or []:
            if el.__class__.__name__ == "ClusterConnect":
                yield el


def _signals_by_node(model) -> dict[str, dict]:
    """Transpose composition + cluster connects into per-node signal
    routing tables.

    Result shape:

        {
            "<NodeName>": {
                "<MsgName>": {
                    "direction": "out" | "in",
                    "destinations": [
                        {"node": "<TargetNode>",
                         "tipc_type": "0x...",
                         "tipc_instance": "..."},
                        ...
                    ]
                }
            }
        }

    Multiple connects from the same source port (fan-out: sm →
    exec/com/ucm/per) produce multiple `destinations[]` entries.
    Incoming-side signals get `direction: "in"` with the source
    node's tipc as their `destinations[0]` (the address whose
    Subscribe message the consumer would route to, if subscribe
    were used — kept for symmetry, not used by the in-process
    routing).

    TIPC is a cluster protocol — the kernel discovers nodes over
    Ethernet — so a destination tipc address resolves transparently
    regardless of which machine hosts the target node. There's no
    host-vs-cluster distinction at this layer.
    """
    out: dict[str, dict] = {}
    for conn in _walk_connects(model):
        src_proto = conn.source.proto
        tgt_proto = conn.target.proto
        # `proto.type` is the resolved NodeDecl. PortRefs in the
        # grammar carry the prototype reference; we go through it
        # to find the node + its tipc address.
        src_node = src_proto.type
        tgt_node = tgt_proto.type
        src_port = _port_on_node(src_node, conn.source.port)
        if src_port is None:
            continue   # bad connect — surfaced elsewhere (audit-manifest)
        # Every message the source port carries is a "signal" that
        # flows along this connect.
        for msg in _iface_messages(src_port.iface):
            entry_src = out.setdefault(src_node.name, {}).setdefault(msg, {
                "direction": "out",
                "destinations": [],
            })
            entry_src["destinations"].append({
                "node": tgt_node.name,
                "tipc_type": tgt_node.tipc.type,
                "tipc_instance": tgt_node.tipc.instance,
            })
            # Receiving side gets the symmetric inbound entry; the
            # destinations[] points BACK at the source — useful for
            # discovery / debug but apps cast outbound only.
            entry_tgt = out.setdefault(tgt_node.name, {}).setdefault(msg, {
                "direction": "in",
                "destinations": [],
            })
            entry_tgt["destinations"].append({
                "node": src_node.name,
                "tipc_type": src_node.tipc.type,
                "tipc_instance": src_node.tipc.instance,
            })
    return out


def build_netgraph(model, catalog: dict | None = None) -> dict:
    routes = _routes_by_node(model, catalog)
    signals = _signals_by_node(model)
    nodes = []
    for n in _iter(model, "NodeDecl"):
        d = _node_dict(n)
        if routes.get(n.name):
            d["gateway_routes"] = routes[n.name]
        # Signal routing table — what the runtime LUT consumes.
        # Empty dict if this node has no connects in any composition
        # / cluster the model walked.
        d["signals"] = signals.get(n.name, {})
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
