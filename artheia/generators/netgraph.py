"""Cluster netgraph generator.

One of TWO netgraphs in the system (see
docs/tasks/PROGRESS/netgraph-signal-routing/README.md):

  - **Cluster netgraph** (this file): per-node TIPC address map for
    the whole cluster + each node's signal routing slice. Consumed
    by the **SUPERVISOR** — passive observer, surfaces topology in
    the GUI plus TIPC stats. Supervisor does NOT do active routing
    (TIPC kernel handles transport transparently); it just visualises.
    Loaded as JSON at supervisor startup; partial orchestration ships
    a new file without reinstalling the supervisor.

  - **PSP netgraph** (artheia/generators/psp_netgraph.py): per-bus
    PDU → bus-side address (CAN id, FlexRay slot/cycle/channel).
    Consumed by the **GATEWAY daemon** which does active CAN/FR
    routing.

Apps + services don't consume this JSON. Each FC binary instead gets
a per-node SLICE as `lib/<Node>_netgraph.hh` (constexpr TipcAddr per
reachable peer) at codegen time — that's the only routing info an
app needs at runtime.

This generator's output:
  - per-node entries: name, tipc, ports, signal routing (signals[]
    dict from compositions/clusters)
  - per-node `gateway_routes: [...]` for nodes that terminate
    vehicle-bus signals via the gateway. Entries match the
    GwCanMeta / GwFlexRayMeta layout from gw_proto.h. Three forms:

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


class DuplicateTipcAddress(ValueError):
    """Raised when two nodes in the system share a TIPC ``(type, instance)``
    address. TIPC routes a message purely by its destination address, so a
    collision means traffic for one node would be delivered to the other —
    a silent mis-wire the runtime can't detect. The netgraph is the
    system-wide view (it unions every reachable model via ``--recursive``),
    so it's the right place to assert global uniqueness."""


def _verify_distinct_tipc(nodes: list[dict]) -> None:
    """Assert every node's TIPC ``(type, instance)`` is unique across the
    whole system. Addresses are normalized through :func:`_parse_hex_or_int`
    first, so ``0x10`` and ``16`` are recognized as the same address. Raises
    :class:`DuplicateTipcAddress` naming every colliding group."""
    by_addr: dict[tuple[int, int], list[str]] = {}
    for n in nodes:
        tipc = n.get("tipc") or {}
        try:
            addr = (_parse_hex_or_int(str(tipc.get("type"))),
                    _parse_hex_or_int(str(tipc.get("instance"))))
        except (TypeError, ValueError):
            # A node without a parseable TIPC address can't collide on one;
            # leave shape validation to the grammar/parser.
            continue
        by_addr.setdefault(addr, []).append(n["name"])

    clashes = {addr: names for addr, names in by_addr.items() if len(names) > 1}
    if clashes:
        lines = [
            f"  tipc type={hex(t)} instance={hex(i)} shared by: "
            f"{', '.join(sorted(names))}"
            for (t, i), names in sorted(clashes.items())
        ]
        raise DuplicateTipcAddress(
            "duplicate TIPC address(es) in the system — each node must have "
            "a distinct (type, instance):\n" + "\n".join(lines)
        )


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


def _iface_directional_messages(iface, src_port_kind: str
                                  ) -> list[tuple[str, str]]:
    """Return [(direction, msg_name), ...] for every message on the
    interface, FROM THE PERSPECTIVE OF THE SOURCE PORT OF A CONNECT.

    The connect arrow is `src.port → tgt.port`. The source port has a
    kind (sender/receiver/client/server) that fixes how messages flow:

      - **SenderPort**   → ReceiverPort: every `data` element on the
        SR interface flows source→target. All msgs are direction="out"
        on the source side.
      - **ReceiverPort** → SenderPort: same data elements, opposite
        sense — source receives them. (Unusual: connects normally go
        sender→receiver. Still surfaced for completeness.)
      - **ClientPort**   → ServerPort: per CS operation, the `in`
        request param flows source→target ("out" on source); the
        `returns` reply flows target→source ("in" on source).
      - **ServerPort**   → ClientPort: same operations, opposite —
        reqs come "in", reps go "out".

    Distinct from :func:`_iface_messages` (which flattens for proto
    include enumeration). This one preserves direction so the
    netgraph routing tables are right for clientServer too — a Reply
    message should NOT be in the client's outbound destinations, and
    a Request should NOT be in the server's outbound destinations.
    """
    cls = iface.__class__.__name__
    if cls == "SenderReceiverInterface":
        # Sender→Receiver: data flows in connect-arrow direction.
        # The connect arrow being SenderPort→ReceiverPort is the
        # normal shape (an ad-hoc Receiver→Sender connect is rare
        # but legal in textX; treat it as a back-arrow).
        if src_port_kind == "SenderPort":
            return [("out", d.type.name) for d in iface.data]
        if src_port_kind == "ReceiverPort":
            return [("in", d.type.name) for d in iface.data]
        return []
    # ClientServerInterface
    out: list[tuple[str, str]] = []
    for op in iface.operations:
        for p in op.params:
            if src_port_kind == "ClientPort":
                # Client sends request out; reply comes in (handled
                # via the `returns` clause below).
                out.append(("out", p.type.name))
            elif src_port_kind == "ServerPort":
                # Server receives request; reply goes out.
                out.append(("in", p.type.name))
        if op.returns is not None:
            if src_port_kind == "ClientPort":
                out.append(("in", op.returns.name))
            elif src_port_kind == "ServerPort":
                out.append(("out", op.returns.name))
    # Dedup preserving order — same message appearing on multiple ops
    # collapses to one entry per direction.
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for entry in out:
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    return deduped


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

    Direction is per-msg from this node's perspective. For a
    senderReceiver interface every data element flows source→target.
    For a clientServer interface the request flows source→target and
    the reply flows target→source — so a `clientServer Status`
    connect produces a per-node table where the client has the
    Request as "out" + the Response as "in", and the server has
    them mirrored. **Service replies are never broadcast** — they
    travel only to the originating client (the connect endpoint),
    so this projection is point-to-point at the operation level.

    Multiple connects from the same source port (fan-out: sm →
    exec/com/ucm/per on a sender port) produce multiple
    `destinations[]` entries. For clientServer connects, fan-out is
    typically unusual (one client typically targets one server) but
    nothing in the grammar prevents it; the projection still works
    by simply listing every destination.

    TIPC is a cluster protocol — the kernel discovers nodes over
    Ethernet — so a destination tipc address resolves transparently
    regardless of which machine hosts the target node. There's no
    host-vs-cluster distinction at this layer.
    """
    out: dict[str, dict] = {}
    for conn in _walk_connects(model):
        src_proto = conn.source.proto
        tgt_proto = conn.target.proto
        src_node = src_proto.type
        tgt_node = tgt_proto.type
        src_port = _port_on_node(src_node, conn.source.port)
        tgt_port = _port_on_node(tgt_node, conn.target.port)
        if src_port is None or tgt_port is None:
            continue   # bad connect — surfaced elsewhere (audit-manifest)
        src_kind = src_port.__class__.__name__
        # The target port's kind is the inverse — if src is "out" for
        # msg M, tgt is "in" for M, and the address pair flips.
        # We compute per-direction msgs from src perspective, then
        # mirror to the tgt entry with flipped direction + flipped
        # peer.
        for direction, msg in _iface_directional_messages(src_port.iface,
                                                            src_kind):
            # The SOURCE side: msg with `direction` toward the
            # TARGET node.
            entry_src = out.setdefault(src_node.name, {}).setdefault(msg, {
                "direction": direction,
                "destinations": [],
            })
            # If a prior connect on this node already classified the
            # msg in the opposite direction, prefer the new one (real
            # connects shouldn't conflict; if they do, the latest
            # write wins — audit-manifest is the place that catches
            # genuinely conflicting topologies).
            if entry_src["direction"] != direction:
                entry_src["direction"] = direction
            entry_src["destinations"].append({
                "node": tgt_node.name,
                "tipc_type": tgt_node.tipc.type,
                "tipc_instance": tgt_node.tipc.instance,
            })
            # The TARGET side: same msg, flipped direction, peer is
            # the SOURCE node.
            opposite = "in" if direction == "out" else "out"
            entry_tgt = out.setdefault(tgt_node.name, {}).setdefault(msg, {
                "direction": opposite,
                "destinations": [],
            })
            if entry_tgt["direction"] != opposite:
                entry_tgt["direction"] = opposite
            entry_tgt["destinations"].append({
                "node": src_node.name,
                "tipc_type": src_node.tipc.type,
                "tipc_instance": src_node.tipc.instance,
            })
    return out


def build_netgraph(
    model,
    catalog: dict | None = None,
    *,
    extra_models: list | None = None,
) -> dict:
    all_models = [model] + list(extra_models or [])

    # Per-node maps from every reachable model. Later definitions
    # don't overwrite earlier ones — a non-stub appearance of NodeX
    # in any model wins, and gateway routes / signals get unioned.
    routes_by_node: dict[str, list[dict]] = {}
    signals_by_node: dict[str, dict] = {}
    for m in all_models:
        for k, v in _routes_by_node(m, catalog).items():
            routes_by_node.setdefault(k, []).extend(v)
        for k, sigs in _signals_by_node(m).items():
            dst = signals_by_node.setdefault(k, {})
            for msg, entry in sigs.items():
                cur = dst.get(msg)
                if cur is None:
                    dst[msg] = {
                        "direction": entry["direction"],
                        "destinations": list(entry["destinations"]),
                    }
                else:
                    cur["destinations"].extend(entry["destinations"])

    nodes_by_name: dict[str, dict] = {}
    for m in all_models:
        for n in _iter(m, "NodeDecl"):
            if n.name in nodes_by_name:
                continue
            d = _node_dict(n)
            if routes_by_node.get(n.name):
                d["gateway_routes"] = routes_by_node[n.name]
            d["signals"] = signals_by_node.get(n.name, {})
            nodes_by_name[n.name] = d

    # System-wide invariant: no two nodes may share a TIPC address.
    _verify_distinct_tipc(list(nodes_by_name.values()))

    compositions_by_name: dict[str, dict] = {}
    for m in all_models:
        for c in _iter(m, "CompositionDecl"):
            if c.name in compositions_by_name:
                continue
            compositions_by_name[c.name] = _composition_dict(c)

    return {
        "package": model.name or "",
        "nodes": list(nodes_by_name.values()),
        "compositions": list(compositions_by_name.values()),
    }


def generate_netgraph(
    model,
    out_file: str | Path,
    *,
    catalog: dict | None = None,
    extra_models: list | None = None,
) -> Path:
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(
            build_netgraph(model, catalog=catalog, extra_models=extra_models),
            indent=2,
        ) + "\n"
    )
    return out_file
