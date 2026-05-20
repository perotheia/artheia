"""Generate the host-side netgraph: symbolic port name -> TIPC address.

Walks .art files for `node atomic` decls carrying a `tipc type=…` and
collects each node's ports. Output is a flat JSON map keyed by a
qualified name `<NodeName>.<port_name>` so a downstream consumer (the
host transport layer in pero_cmp_lnx) can join the symbolic port the
app uses with the underlying TIPC address.

Output shape:

    {
      "nodes": {
        "Gateway": {
          "tipc_type": "0xa0010001",
          "tipc_instance": 0,
          "ports": {
            "status": {"kind": "server", "interface": "Status"}
          }
        },
        "OddPathMonitor": {
          "tipc_type": "0xc0010001",
          "tipc_instance": 0,
          "ports": {
            "status_query": {"kind": "client", "interface": "Status"},
            "eml_01": {"kind": "receiver", "interface": "EML_01_Iface"},
            ...
          }
        }
      }
    }

Multiple .art files may declare the same node (e.g. as a forward-decl
plus a real decl in different files); the parser does no merging here
— last-write-wins. If a node appears with different tipc/ports across
files that's a real conflict; surface it as a warning, keep the
first.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..model import parse_file


def _port_dict(port) -> dict:
    """Render a single Port to JSON. Port kind / interface comes from
    the parsed textX object — class is `ServerPort`/`ClientPort`/
    `SenderPort`/`ReceiverPort`."""
    cls = port.__class__.__name__
    kind = cls[:-len("Port")].lower() if cls.endswith("Port") else cls.lower()
    iface = getattr(port, "iface", None) or getattr(port, "interface", None)
    iface_name = iface.name if iface is not None else "?"
    return {"kind": kind, "interface": iface_name}


def _format_tipc(tipc) -> str:
    """tipc is a TipcAddress with .type and .instance attributes from textX."""
    t = getattr(tipc, "type", None)
    if isinstance(t, int):
        return f"0x{t:08x}"
    return str(t)


def generate(art_paths: list[str | Path], out_path: str | Path) -> Path:
    """Parse each .art, harvest TIPC-addressed nodes, emit JSON LUT."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    nodes: dict[str, dict] = {}
    sources: list[str] = []

    for p in art_paths:
        p = Path(p)
        sources.append(str(p))
        model = parse_file(str(p))
        for el in model.elements:
            if el.__class__.__name__ != "NodeDecl":
                continue
            if not getattr(el, "tipc", None):
                continue
            name = el.name
            entry = {
                "tipc_type": _format_tipc(el.tipc),
                "tipc_instance": int(getattr(el.tipc, "instance", 0) or 0),
                "ports": {},
                "source": str(p),
            }
            for port in getattr(el, "ports", []) or []:
                entry["ports"][port.name] = _port_dict(port)
            if name in nodes:
                existing = nodes[name]
                if entry["tipc_type"] != existing["tipc_type"]:
                    print(
                        f"  WARNING: {name} redeclared in {p} with different "
                        f"tipc_type ({entry['tipc_type']} vs "
                        f"{existing['tipc_type']}); keeping richer port set"
                    )
                # Forward-decls trim ports; pick the entry with the most ports.
                if len(entry["ports"]) <= len(existing["ports"]):
                    continue
            nodes[name] = entry

    payload = {
        "sources": sources,
        "nodes": nodes,
    }
    out_path.write_text(json.dumps(payload, indent=2))

    total_ports = sum(len(n["ports"]) for n in nodes.values())
    print(f"  wrote: {out_path}")
    print(f"  nodes: {len(nodes)}  ports: {total_ports}")
    for nm, entry in nodes.items():
        print(f"    {nm} @ {entry['tipc_type']} ({len(entry['ports'])} ports)")
    return out_path
