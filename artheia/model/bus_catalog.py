"""Bus catalog: which bus identifiers the parser accepts without a BusDecl.

The Theia gateway publishes its canonical enumeration in
`gw_bus_types.h` (auto-generated from `gen_gw_types.py`). When that header
is reachable on the local filesystem we parse it at import time so the
list stays in sync; otherwise we fall back to a hardcoded snapshot.

The format we parse:

    GW_BUS_CAN_KCAN = 6,
    GW_BUS_VEHICLE_GEN2_A = 128,

Naming convention → bus identifier in DSL:
  GW_BUS_CAN_KCAN          -> "kcan"          (drop GW_BUS_, drop CAN_)
  GW_BUS_VEHICLE_GEN2_A     -> "vehicle_gen2_a" (drop GW_BUS_)
  GW_BUS_INVALID           -> skipped
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Snapshot of gw_bus_types.h at the time Artheia v0.1 shipped. Kept as a
# fallback so the validator works even when the Theia repo isn't checked
# out next to Artheia. Bus_id values (1-127 = CAN, 128+ = FlexRay) are
# derived from gw_proto.h conventions.
_SNAPSHOT: dict[str, str] = {
    "diagcan":       "can",
    "dcan":          "can",
    "hcan":          "can",
    "ican":          "can",
    "k2can":         "can",
    "kcan":          "can",
    "komfortcan":    "can",
    "subcan":        "can",
    "vehicle_gen2_a": "flexray",
    "vehicle_gen2_b": "flexray",
}

_ENUM_LINE = re.compile(r"GW_BUS_([A-Z0-9_]+)\s*=\s*(\d+)")


def _dsl_name(symbol: str) -> str | None:
    if symbol == "INVALID":
        return None
    parts = symbol.split("_")
    if parts and parts[0] == "CAN":
        parts = parts[1:]
    return "_".join(parts).lower() or None


def parse_header(path: Path | str) -> dict[str, str]:
    """Return {dsl_name: 'can'|'flexray'} for every GW_BUS_* in the header."""
    text = Path(path).read_text()
    out: dict[str, str] = {}
    for sym, value in _ENUM_LINE.findall(text):
        name = _dsl_name(sym)
        if not name:
            continue
        out[name] = "can" if int(value) < 128 else "flexray"
    return out


def _candidate_paths() -> list[Path]:
    """Where to look for gw_bus_types.h. Most specific first."""
    env = os.environ.get("ARTHEIA_GW_BUS_TYPES_H")
    paths: list[Path] = []
    if env:
        paths.append(Path(env))
    home = Path.home()
    paths.extend([
        home / "repo" / "theia" / "gateway" / "pero_cmp_lnx" / "lib" / "gw" / "include" / "gw_bus_types.h",
        home / "repo" / "theia" / "applications" / "pero_cmp_gw_cln_demo" / "src" / "gw_bus_types.h",
    ])
    return paths


def load_buses() -> dict[str, str]:
    """Merge the snapshot with anything we can parse from a live header.

    The live header wins on conflicts because it's the upstream source of
    truth. The snapshot fills in anything the local header omits — which
    matters when running against an older theia checkout that hasn't yet
    grown a bus the snapshot already knows about, and vice versa.
    """
    merged = dict(_SNAPSHOT)
    for candidate in _candidate_paths():
        if candidate.is_file():
            try:
                merged.update(parse_header(candidate))
            except (OSError, ValueError):
                continue
            break  # first hit wins
    return merged


# Module-level cache. Cheap (one file read at import time on success).
WELL_KNOWN_GATEWAY_BUSES: dict[str, str] = load_buses()
