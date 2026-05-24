"""Per-bus PSP netgraph: PDU → bus-level address LUT.

One of TWO netgraphs in the system (see
docs/tasks/PROGRESS/netgraph-signal-routing/README.md):

  - **PSP netgraph** (this file): per-bus PDU → bus address (CAN id,
    FlexRay slot/cycle/channel). Consumed by the **GATEWAY daemon** —
    it does active routing, translating TIPC traffic to/from the
    actual CAN/FlexRay wire. The gateway loads this LUT as JSON at
    startup; partial orchestration ships a new netgraph.json without
    reinstalling the gateway binary.

  - **Cluster netgraph** (artheia/generators/netgraph.py): per-node
    TIPC address map for the whole cluster. Consumed by the
    **SUPERVISOR** — passive observer, surfaces the cluster topology
    in the GUI plus TIPC stats. The supervisor does NOT route TIPC
    traffic (TIPC kernel handles that transparently); it just shows
    the topology.

Why JSON, not C++: gateway + supervisor are not ARA components;
they're platform infrastructure that outlives any single .art
revision. Updating the bus catalog or rewiring connects ⇒ ship a
new JSON, no recompile.

PSP netgraph maps the symbolic PDU name (the unit declared by
`autosar gen-autosar-system`) to its bus-level address(es): a CAN
can_id+dlc, or a FlexRay (slot, cycle, channel, byte_offset) tuple.

We split this off from `catalog.json` so the catalog can stay focused
on signal layout (bit positions, types, enum companions) and the
netgraph can stay focused on wire addressing. Downstream consumers:

- Gateway daemon: the active router, joins the TIPC transport
  header by symbolic port name → bus address via this file.
- The signal-filter CSV generator (future) joins per-app port lists
  with this LUT to compute which signals the gateway needs to forward.

Generator was previously named `netgraph_partition.py` /
`gen-netgraph-partition` — renamed in 2026-05 to reflect the format
(this is the PSP-side netgraph, distinct from the cluster netgraph).

Output schema:

    {
      "bus": "mlbevo_gen2",
      "bus_kind": "flexray",
      "routes": {
        "EML_01": {
          "byte_length": 8,
          "frame_triggers": [
            { "frame_name": "FRAME_5_15_16",
              "slot_id": 5, "cycle": 15, "cycle_repetition": 16,
              "channel": "channel_782614", "channel_idx": 0,
              "pdu_byte_offset": 0 },
            ...
          ]
        },
        ...
      }
    }

For a CAN catalog:

    {
      "bus": "kcan",
      "bus_kind": "can",
      "routes": {
        "ACC_07": { "can_id": 302, "extended_id": false, "dlc": 8 },
        ...
      }
    }
"""
from __future__ import annotations

import json
from pathlib import Path


def generate(catalog_path: str | Path, out_path: str | Path) -> Path:
    """Read `catalog.json`, emit a netgraph partition at `out_path`.
    Returns the written path.
    """
    catalog_path = Path(catalog_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cat = json.loads(catalog_path.read_text())
    bus = cat["bus"]
    bus_kind = cat["bus_kind"]

    routes: dict[str, dict] = {}
    messages = cat.get("messages", {})

    if bus_kind == "can":
        for name, msg in messages.items():
            routes[name] = {
                "can_id": msg["can_id"],
                "extended_id": msg.get("extended_id", False),
                "dlc": msg["dlc"],
            }
    elif bus_kind == "flexray":
        for name, msg in messages.items():
            routes[name] = {
                "byte_length": msg["byte_length"],
                "frame_triggers": msg.get("frame_triggers", []),
            }
    else:
        raise ValueError(f"unknown bus_kind {bus_kind!r} in {catalog_path}")

    payload = {
        "bus": bus,
        "bus_kind": bus_kind,
        "source_catalog": str(catalog_path),
        "routes": routes,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"  wrote: {out_path}")
    print(f"  routes: {len(routes)}")
    return out_path
