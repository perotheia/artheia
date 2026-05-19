"""DBC + FIBEX → Artheia importer.

Reverses the AUTOSAR-side of the network (CAN frames from `.dbc` and
FlexRay frames + PDUs from FIBEX cluster XML) into:

  - `vendor/autosar/<bus>/package.art` — one `message FrameName { ... }`
    per gateway-visible frame, with the signal fields laid out inline
    so callers can reference them by name. Signals that carry a value
    table (DBC `VAL_` / FIBEX `COMPU-METHOD TEXTTABLE`) drive a top-
    level `enum FrameName_SignalName { ... }` decl that the field then
    uses as its type.
  - `vendor/autosar/<bus>/catalog.json` — netgraph metadata. Per frame:
    bus, bus_kind ("can"|"flexray"), can_id|slot_id|cycle|channel, dlc,
    plus per-signal `bit_position`, `bit_length`, `proto_type`,
    `factor`, `offset`, `unit`, and `values` (when present) so the
    downstream codec generators have everything they need.

CSV filter (optional) matches theia's tooling: one row per (signal, frame)
pair to include. When omitted, every frame in the source is emitted.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ._asam_cmp_parser import (
    DbcDb, DbcMessage, DbcSignal,
    FibexDb, FrameInfo, PduInfo,
    SignalInstance, _proto_type_for,
)


# ---- identifier sanitization ----------------------------------------------


_NON_IDENT_RE = re.compile(r"[^A-Za-z0-9_]+")
_LEADING_DIGIT_RE = re.compile(r"^[0-9]")


def _sanitize_ident(s: str) -> str:
    """Map any string to a valid Artheia identifier, or "" if nothing
    sensible survives. Replaces runs of non-ident chars with `_`, then
    prefixes `_` if the result starts with a digit. Strips trailing `_`s
    so we don't get `Foo_` from `Foo!`."""
    s = _NON_IDENT_RE.sub("_", s).strip("_")
    if not s:
        return ""
    if _LEADING_DIGIT_RE.match(s):
        s = "_" + s
    return s


def _enum_type_name(frame_name: str, signal_name: str) -> str:
    """Enum decls are always prefixed with the frame name so signals
    named `Status` etc. in many frames don't collide. We don't sanitize
    further here — DBC/FIBEX signal names are already ident-shaped."""
    return f"{frame_name}_{signal_name}"


def _emit_enum(name: str, raw_pairs: Iterable[tuple[int, str]]) -> list[str]:
    """Render an `enum Name { K1 = 0 ... }` block. Sanitizes value
    labels; if sanitization would produce a duplicate (or empty) name,
    falls back to `VAL_<n>` and stashes the original in a `//` comment.
    Returns lines (no trailing blank)."""
    lines = [f"enum {name} {{"]
    seen: set[str] = set()
    for num, raw_label in raw_pairs:
        clean = _sanitize_ident(raw_label)
        if not clean or clean in seen:
            fallback = f"VAL_{num}"
            if fallback in seen:
                fallback = f"VAL_{num}_{len(seen)}"
            clean = fallback
        seen.add(clean)
        comment = ""
        if raw_label and raw_label != clean:
            comment = f"  // {raw_label!r}"
        lines.append(f"    {clean} = {num}{comment}")
    lines.append("}")
    return lines


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


def _emit_package_art(
    bus_name: str,
    catalog: dict[str, dict],
    package_prefix: str = "vendor.autosar",
) -> str:
    """Render the per-bus `.art` file.

    For every frame in `catalog`, emits:
      - one top-level `enum <frame>_<signal> { ... }` per signal that
        carries a value table (DBC `VAL_` / FIBEX `COMPU-METHOD TEXTTABLE`);
      - a `message <frame> { <field> <name> ... }` with one MessageField
        per signal. **Field types stay scalar** (uint32/int32/float/bool/...);
        the enums are companion declarations user code may reference when
        populating fields. Keeping the wire layout scalar means the codec
        layer doesn't need enum knowledge.
    """
    lines: list[str] = []
    lines.append(
        f"// Generated from a DBC/FIBEX source — DO NOT EDIT BY HAND.\n"
        f"// AUTOSAR-side frames on bus `{bus_name}` with inline signal\n"
        f"// fields. Full bit layout, scale, offset, units, and value tables\n"
        f"// also live in catalog.json so downstream codec generators can\n"
        f"// stay in sync without re-parsing the .art."
    )
    lines.append("")
    lines.append(f"package {package_prefix}.{bus_name}")
    lines.append("")

    # First pass: emit every enum at the top level.
    enum_emitted: list[str] = []
    for frame_name in sorted(catalog):
        for field in catalog[frame_name].get("fields", []):
            values = field.get("values") or []
            if not values:
                continue
            enum_name = _enum_type_name(frame_name, field["name"])
            enum_emitted.extend(_emit_enum(enum_name, values))
            enum_emitted.append("")
    if enum_emitted:
        lines.extend(enum_emitted)

    # Second pass: messages.
    for frame_name in sorted(catalog):
        fields = catalog[frame_name].get("fields", [])
        if not fields:
            lines.append(f"message {frame_name} {{ }}")
            lines.append("")
            continue
        lines.append(f"message {frame_name} {{")
        for field in fields:
            ftype = field["proto_type"]
            # If the signal has a value table, reference the companion
            # enum in a trailing comment so user code knows it exists.
            tail = ""
            if field.get("values"):
                tail = f"  // enum: {_enum_type_name(frame_name, field['name'])}"
            lines.append(f"    {ftype} {field['name']}{tail}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _dbc_field_entry(sig: DbcSignal) -> dict:
    entry: dict = {
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
    # DbcSignalValue is `(raw: float, label: str)` — emit as int when possible.
    if sig.values:
        entry["values"] = [
            [int(v.raw) if float(v.raw).is_integer() else v.raw, v.label]
            for v in sig.values
        ]
    return entry


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
    entry: dict = {
        "name": name,
        "bit_position": inst.bit_position,
        "bit_length": bit_length,
        "proto_type": proto,
        "is_signed": is_signed,
        "motorola_byte_order": bool(inst.motorola_byte_order),
    }
    if coding is not None and coding.text_table:
        entry["values"] = [
            [int(v) if float(v).is_integer() else v, label]
            for v, label in coding.text_table
        ]
    return entry


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
    package_prefix: str = "vendor.autosar",
) -> ImportResult:
    """Parse a DBC file, emit `package.art` + `catalog.json` under
    `out_dir`. Returns paths and the number of emitted frames.

    `package_prefix` controls the `.art` package name (e.g.
    "vendor.tornado.system.autosar" when the output lives under a
    vendor system tree).
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
    art_path.write_text(_emit_package_art(bus_name, catalog, package_prefix))
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
    package_prefix: str = "vendor.autosar",
) -> ImportResult:
    """Parse a FIBEX cluster file, emit `package.art` + `catalog.json` under
    `out_dir`. Returns paths and the number of emitted frames.

    `package_prefix` controls the `.art` package name (e.g.
    "vendor.tornado.system.autosar" when the output lives under a
    vendor system tree).
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
    art_path.write_text(_emit_package_art(bus_name, catalog, package_prefix))
    cat_path.write_text(
        json.dumps(
            {"bus": bus_name, "bus_kind": "flexray", "messages": catalog},
            indent=2,
        )
    )
    return ImportResult(art=art_path, catalog=cat_path, frame_count=len(frame_names))
