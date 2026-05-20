"""Generate per-bus netgraph partitions from an AUTOSAR catalog.json.

A **netgraph partition** is the routing LUT that maps a symbolic PDU
name (the unit declared by `autosar gen-autosar-system`) to its
bus-level address(es): a CAN can_id+dlc, or a FlexRay
(slot, cycle, channel, byte_offset) tuple.

We split this off from `catalog.json` so the catalog can stay focused
on signal layout (bit positions, types, enum companions) and the
netgraph can stay focused on wire addressing. Downstream consumers:

- The gateway runtime LUT (`pero_cmp_lnx`) joins the host transport
  header by symbolic port name → bus address via this file.
- The signal-filter CSV generator (future) joins per-app port lists
  with this LUT to compute which signals the gateway needs to forward.

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
