"""Generate a signal_filter.csv from a vendor's component .art files.

Walks every .art under a vendor root (typically `vendor/<v>/system/components/`)
and collects every `gateway_route Node { signal = Foo ... }` reference. Cross-
references the referenced message name against AUTOSAR catalog.json files
under `<vendor>/system/autosar/<bus>/catalog.json` to recover the per-frame
signal layout, then emits a CSV in the gateway-expected format:

    signal_name,pdu_name
    <sig>,<frame>

The CSV is what `pero_cmp_gw_svc/generate.sh` feeds into
`artheia gen-app-dispatch` so that only the listed signals get encoded into
the runtime dispatch table.

Today (vendor/tornado), no component .art carries gateway_route blocks yet —
Tornado still runs on its own DDS topics and hasn't been rewired to route
through the gateway. Running this generator against vendor/tornado emits a
header-only CSV; once the components are migrated, the CSV populates itself.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable


_ROUTE_BLOCK_RE = re.compile(
    r"^gateway_route\s+\S+\s*\{([^}]*)\}",
    re.MULTILINE | re.DOTALL,
)
_SIGNAL_RE = re.compile(r"^\s*signal\s*=\s*([\w.]+)\s*$", re.MULTILINE)


def _scan_components(components_root: Path) -> list[str]:
    """Return the unique list of message names referenced via `signal = Foo`."""
    names: list[str] = []
    seen: set[str] = set()
    if not components_root.is_dir():
        return names
    for art_path in sorted(components_root.rglob("*.art")):
        text = art_path.read_text()
        for block_match in _ROUTE_BLOCK_RE.finditer(text):
            for sig_match in _SIGNAL_RE.finditer(block_match.group(1)):
                # The `signal = FQN` reference may be qualified
                # (vendor.tornado.system.autosar.kcan.ACC_07) or bare (ACC_07).
                ref = sig_match.group(1)
                msg_name = ref.rsplit(".", 1)[-1]
                if msg_name not in seen:
                    seen.add(msg_name)
                    names.append(msg_name)
    return names


def _load_catalogs(autosar_root: Path) -> dict[str, dict]:
    """Load every catalog.json under autosar_root and merge by message name.

    Returns {frame_name: catalog_entry}. If the same frame appears on two
    buses we keep the first (warn-worthy but not fatal).
    """
    merged: dict[str, dict] = {}
    if not autosar_root.is_dir():
        return merged
    for cat_path in sorted(autosar_root.rglob("catalog.json")):
        cat = json.loads(cat_path.read_text())
        for frame_name, frame in cat.get("messages", {}).items():
            if frame_name not in merged:
                merged[frame_name] = frame
    return merged


def generate(
    vendor_root: str | Path,
    out_path: str | Path,
) -> Path:
    """Walk `<vendor_root>/system/components/` for gateway_route signal refs,
    expand each via `<vendor_root>/system/autosar/<bus>/catalog.json`, and
    emit `<out_path>` as a `signal_name,pdu_name` CSV.

    Returns the written CSV path. Emits a header-only CSV if no routes are
    found (intentional — driven by component state).
    """
    vendor_root = Path(vendor_root)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    components_root = vendor_root / "system" / "components"
    autosar_root = vendor_root / "system" / "autosar"

    referenced_frames = _scan_components(components_root)
    catalogs = _load_catalogs(autosar_root)

    rows: list[tuple[str, str]] = []
    missing: list[str] = []
    for frame_name in referenced_frames:
        entry = catalogs.get(frame_name)
        if entry is None:
            missing.append(frame_name)
            continue
        for field in entry.get("fields", []):
            rows.append((field["name"], frame_name))

    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["signal_name", "pdu_name"])
        for sig, frame in rows:
            writer.writerow([sig, frame])

    print(f"  wrote: {out_path}")
    print(f"  frames referenced  : {len(referenced_frames)}")
    print(f"  signal rows emitted: {len(rows)}")
    if missing:
        print(f"  WARNING: {len(missing)} referenced frame(s) not in AUTOSAR catalog:")
        for n in missing:
            print(f"    {n}")
    return out_path
