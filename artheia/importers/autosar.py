"""DBC + FIBEX → Artheia importer.

Reverses the AUTOSAR-side of the network (CAN frames from `.dbc` and
FlexRay frames + PDUs from FIBEX cluster XML) into:

  - `vendor/autosar/<bus>/package.art` — Artheia forward-declaration
    `message FrameName { }` per gateway-visible frame. The body is opaque:
    bit layout lives in the catalog, not the `.art`.
  - `vendor/autosar/<bus>/catalog.json` — netgraph metadata in the shape
    the `gen-netgraph --catalog` consumer already understands. Per frame:
    bus, bus_kind ("can"|"flexray"), can_id|slot_id|cycle|channel, dlc,
    and the signal-level layout for downstream codecs.

CSV filter (optional) matches theia's tooling: one row per (signal, frame)
pair to include. When omitted, every frame in the source is emitted.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ._asam_cmp_parser import (
    DbcDb, DbcMessage, DbcSignal,
    FibexDb, FrameInfo, PduInfo,
    SignalInstance, _proto_type_for,
)


# ---- CSV filter ------------------------------------------------------------


def _load_signal_csv(csv_path: Path | None) -> set[str] | None:
    """Return a set of frame/message names to keep, or None for 'all frames'.

    Matches theia's CSV format: header row `signal_name,message_name`. We
    only need the message_name column because the artheia output is at
    frame granularity — the signal-level filter is enforced by the catalog
    consumers downstream.
    """
    if csv_path is None:
        return None
    keep: set[str] = set()
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("message_name") or row.get("frame_name") or "").strip()
            if name:
                keep.add(name)
    return keep


# ---- emit ------------------------------------------------------------------


def _emit_package_art(bus_name: str, frame_names: list[str]) -> str:
    """Render the opaque-message .art file for one bus."""
    lines: list[str] = []
    lines.append(
        f"// Generated from a DBC/FIBEX source — DO NOT EDIT BY HAND.\n"
        f"// Forward declarations for every AUTOSAR-side frame on bus `{bus_name}`.\n"
        f"// Bit layout, addresses, and signal-level metadata live in catalog.json;\n"
        f"// Artheia treats the frame body as opaque (PDU contents stay in the\n"
        f"// gateway's domain)."
    )
    lines.append("")
    lines.append(f"package vendor.autosar.{bus_name}")
    lines.append("")
    for name in sorted(frame_names):
        lines.append(f"message {name} {{ }}")
    return "\n".join(lines) + "\n"


def _dbc_field_entry(sig: DbcSignal) -> dict:
    return {
        "name": sig.name,
        "bit_position": sig.start_bit,
        "bit_length": sig.bit_length,
        "proto_type": sig.proto_type,
        "is_signed": sig.is_signed,
        "motorola_byte_order": sig.motorola_byte_order,
        "factor": sig.factor,
        "offset": sig.offset,
        "unit": sig.unit,
    }


def _dbc_catalog_entry(bus_name: str, msg: DbcMessage) -> dict:
    return {
        "bus": bus_name,
        "bus_kind": "can",
        "can_id": msg.can_id,
        "extended_id": msg.is_extended,
        "dlc": msg.dlc,
        "fields": [_dbc_field_entry(s) for s in msg.signals.values()],
    }


def _fibex_field_entry(inst: SignalInstance) -> dict:
    sig = inst.signal
    coding = sig.coding if sig is not None else None
    name = (sig.name if sig is not None else inst.signal_ref) or ""
    if coding is not None:
        is_signed = coding.encoding == "SIGNED"
        proto = _proto_type_for(
            coding.bit_length, is_signed, coding.scale, coding.offset,
            has_values=bool(coding.text_table),
        )
        bit_length = coding.bit_length
    else:
        is_signed = False
        proto = "uint32"
        bit_length = 0
    return {
        "name": name,
        "bit_position": inst.bit_position,
        "bit_length": bit_length,
        "proto_type": proto,
        "is_signed": is_signed,
        "motorola_byte_order": bool(inst.motorola_byte_order),
    }


def _fibex_catalog_entry(
    bus_name: str, trig, frame: FrameInfo, channel_name: str,
) -> dict:
    fields: list[dict] = []
    for inst in frame.pdu_instances:
        if inst.pdu is None:
            continue
        for sig_inst in inst.pdu.signal_instances:
            fields.append(_fibex_field_entry(sig_inst))
    return {
        "bus": bus_name,
        "bus_kind": "flexray",
        "slot_id": trig.slot_id,
        "cycle": trig.base_cycle,
        "cycle_repetition": trig.cycle_repetition,
        "channel": channel_name,
        "channel_idx": trig.channel_idx,
        "byte_length": frame.byte_length,
        "fields": fields,
    }


# ---- public entry points ---------------------------------------------------


@dataclass
class ImportResult:
    art: Path
    catalog: Path
    frame_count: int


def import_dbc(
    dbc_path: str | Path,
    bus_name: str,
    out_dir: str | Path,
    *,
    signal_csv: str | Path | None = None,
) -> ImportResult:
    """Parse a DBC file, emit `package.art` + `catalog.json` under
    `out_dir`. Returns paths and the number of emitted frames.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = DbcDb()
    db.load(str(dbc_path), bus_name=bus_name)

    keep = _load_signal_csv(Path(signal_csv) if signal_csv else None)
    catalog: dict[str, dict] = {}
    frame_names: list[str] = []
    for name, msg in db.messages.items():
        if keep is not None and name not in keep:
            continue
        catalog[name] = _dbc_catalog_entry(bus_name, msg)
        frame_names.append(name)

    art_path = out_dir / "package.art"
    cat_path = out_dir / "catalog.json"
    art_path.write_text(_emit_package_art(bus_name, frame_names))
    cat_path.write_text(
        json.dumps(
            {"bus": bus_name, "bus_kind": "can", "messages": catalog},
            indent=2,
        )
    )
    return ImportResult(art=art_path, catalog=cat_path, frame_count=len(frame_names))


def _iter_fibex_frames(
    db: FibexDb,
) -> Iterable[tuple[object, FrameInfo, str]]:
    """Yield (trigger, frame, channel_name) for every frame triggered in
    the FIBEX cluster. Channel-name lookup inverts the parser's
    `_channel_map` (`name → idx`) so we report a human-readable name when
    possible, falling back to `channel_<idx>`.
    """
    name_by_idx = {idx: name for name, idx in getattr(db, "_channel_map", {}).items()}
    for trig in db.frame_triggers:
        if trig.frame is None:
            continue
        ch_name = name_by_idx.get(trig.channel_idx, f"channel_{trig.channel_idx}")
        yield trig, trig.frame, ch_name


def import_fibex(
    fibex_path: str | Path,
    bus_name: str,
    out_dir: str | Path,
    *,
    signal_csv: str | Path | None = None,
) -> ImportResult:
    """Parse a FIBEX cluster file, emit `package.art` + `catalog.json` under
    `out_dir`. Returns paths and the number of emitted frames.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = FibexDb()
    db.load(str(fibex_path))

    keep = _load_signal_csv(Path(signal_csv) if signal_csv else None)
    catalog: dict[str, dict] = {}
    frame_names: list[str] = []
    for trig, frame, ch_name in _iter_fibex_frames(db):
        if keep is not None and frame.name not in keep:
            continue
        if frame.name in catalog:
            # Same frame triggered on multiple channels — record only the
            # first, but tag the channel-name list onto the catalog entry
            # so consumers don't lose the multi-channel fact.
            existing = catalog[frame.name]
            existing.setdefault("extra_channels", []).append(ch_name)
            continue
        catalog[frame.name] = _fibex_catalog_entry(bus_name, trig, frame, ch_name)
        frame_names.append(frame.name)

    art_path = out_dir / "package.art"
    cat_path = out_dir / "catalog.json"
    art_path.write_text(_emit_package_art(bus_name, frame_names))
    cat_path.write_text(
        json.dumps(
            {"bus": bus_name, "bus_kind": "flexray", "messages": catalog},
            indent=2,
        )
    )
    return ImportResult(art=art_path, catalog=cat_path, frame_count=len(frame_names))
