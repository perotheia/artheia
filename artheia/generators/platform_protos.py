#!/usr/bin/env python3
"""
gen_platform_protos.py — unified FlexRay+CAN codec generator with deduplication.

Replaces running fibex_to_nanopb.py and can_to_nanopb.py separately.

Key feature: detects identical PDU/message layouts across buses and generates
shared codec functions + shared proto files instead of duplicating them.

Usage:
    python3 gen_platform_protos.py \\
        --fibex config/cluster.xml \\
        --dbc config/dbc/KCAN.dbc:kcan \\
        --dbc config/dbc/HCAN.dbc:hcan \\
        --dbc config/dbc/KomfortCAN.dbc:komfortcan \\
        --namespace-fr mlbevo_gen2 \\
        --out-src  src/ \\
        --out-proto proto/ \\
        [--all-signals | --csv signals.csv] \\
        [--encode-only | --decode-only]

Output structure:
    src/shared/can_encode_ACC_07.c      -- shared codec (same layout on multiple buses)
    src/shared/can_decode_ACC_07.c
    src/can/kcan/can_dispatch_table.c   -- references shared fn
    src/can/komfortcan/can_dispatch_table.c
    src/can/hcan/can_encode_ACC_99.c    -- bus-specific (unique layout)
    src/can/hcan/can_dispatch_table.c
    src/flexray/encode_ACC_06.c         -- FlexRay
    src/flexray/dispatch_table.c
    proto/shared/ACC_07.proto           -- shared proto
    proto/can/hcan/ACC_99.proto         -- bus-specific proto
    proto/flexray/ACC_06.proto          -- FlexRay proto
    src/psp_can_registry.c + .h         -- aggregates all buses
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    sys.exit("ERROR: jinja2 is required.  Run: pip install jinja2")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

from ..importers._asam_cmp_parser import (
    FibexDb, DbcDb, DbcMessage, DbcSignal,
    CodingInfo, SignalInfo, SignalInstance, PduInfo, FrameTrigger,
)


# ===========================================================================
# Fingerprint — layout deduplication key
# ===========================================================================

def _dbc_signal_fingerprint(sig: DbcSignal) -> tuple:
    """Fingerprint for a single DBC signal."""
    return (sig.start_bit, sig.bit_length, int(sig.motorola_byte_order),
            sig.factor, sig.offset, sig.name)


def _fibex_signal_fingerprint(si: SignalInstance) -> tuple:
    """Fingerprint for a single FIBEX signal instance."""
    sig  = si.signal
    cod  = sig.coding if sig else None
    name = sig.name if sig else ''
    bl   = cod.bit_length if cod else 0
    mot  = int(si.motorola_byte_order)
    sc   = cod.scale  if cod else 1.0
    off  = cod.offset if cod else 0.0
    return (si.bit_position, bl, mot, sc, off, name)


def can_layout_fingerprint(signals: List[DbcSignal]) -> tuple:
    """
    Stable fingerprint for a CAN message layout.
    Sorted so that signal order in the DBC does not affect equality.
    """
    return tuple(sorted(_dbc_signal_fingerprint(s) for s in signals))


def fr_layout_fingerprint(signal_instances: List[SignalInstance]) -> tuple:
    """Stable fingerprint for a FlexRay PDU layout."""
    return tuple(sorted(_fibex_signal_fingerprint(si) for si in signal_instances
                        if si.signal is not None))


# ===========================================================================
# CanField — mirrors can_to_nanopb.py
# ===========================================================================

@dataclass
class CanField:
    field_number: int
    signal:       DbcSignal
    proto_type:   str


def _proto_buf_max_can(fields: List[CanField]) -> int:
    return len(fields) * 12 + 8


# ===========================================================================
# Layout registry — tracks canonical owner of each fingerprint
# ===========================================================================

@dataclass
class CanonicalEntry:
    """The first bus/namespace that defined a given fingerprint."""
    fingerprint:  tuple
    name:         str          # message/PDU name
    namespace:    str          # owning namespace
    bus_name:     str          # 'kcan', 'hcan', 'flexray', ...
    is_shared:    bool = False # becomes True when a second bus matches

    # CAN-specific (None for FlexRay)
    msg:    Optional[DbcMessage]         = None
    fields: Optional[List[CanField]]     = None

    # FlexRay-specific (None for CAN)
    pdu:           Optional[PduInfo]     = None
    fr_fields:     Optional[list]        = None  # [(fn, si, sig, coding, proto_type)]
    dispatch_entry: Optional[object]     = None  # DispatchEntry from fibex_to_nanopb


# ===========================================================================
# FlexRay helpers (lifted from fibex_to_nanopb.py)
# ===========================================================================

def _proto_type_fibex(coding: CodingInfo) -> str:
    if coding.scale != 1.0 or coding.offset != 0.0:
        return "float"
    if coding.encoding == "FLOAT":
        return "float"
    bl = coding.bit_length
    m  = coding.method
    if m == "TEXTTABLE":
        return "uint32"
    if coding.encoding == "SIGNED":
        return "int32" if bl <= 32 else "int64"
    if bl == 1:
        return "bool"
    return "uint32" if bl <= 32 else "uint64"


@dataclass
class FrDispatchEntry:
    slot_id:          int
    channel_idx:      int
    pdu_name:         str
    pdu_byte_offset:  int
    pdu_byte_length:  int
    proto_buf_max:    int
    is_shared:        bool = False  # True → codec is cmp_encode_shared_{codec_name}
    codec_name:       str  = ''    # canonical PDU name (may differ from pdu_name for shared)


def _resolve_all_fr_pdus(
    db: FibexDb
) -> Dict[str, Tuple[PduInfo, List[Tuple[int, SignalInstance, SignalInfo, CodingInfo, str]]]]:
    """Return {pdu_name: (pdu_obj, [(fn, si, sig, coding, proto_type), ...])}."""
    result: Dict[str, Tuple] = {}
    seen_names: Set[str] = set()
    for pdu in db.pdus.values():
        if pdu.pdu_type.upper() != 'APPLICATION':
            continue
        if not pdu.signal_instances:
            continue
        if pdu.name in seen_names:
            continue
        seen_names.add(pdu.name)
        entries = []
        for si in pdu.signal_instances:
            sig = si.signal
            if sig is None:
                continue
            cod = sig.coding
            if cod is None:
                continue
            entries.append((si, sig, cod))
        if not entries:
            continue
        entries.sort(key=lambda t: t[0].bit_position)
        fields = [(fn + 1, si, sig, cod, _proto_type_fibex(cod))
                  for fn, (si, sig, cod) in enumerate(entries)]
        result[pdu.name] = (pdu, fields)
    return result


def _fr_dispatch_entries(
    db: FibexDb, pdu_name: str, pdu_obj: PduInfo, num_fields: int
) -> List[FrDispatchEntry]:
    """Build FlexRay dispatch entries for a PDU."""
    entries = []
    for ft in db.frame_triggers:
        if ft.frame is None:
            continue
        for pi in ft.frame.pdu_instances:
            if pi.pdu is not None and pi.pdu.id == pdu_obj.id:
                pdu_byte_offset = pi.bit_position // 8
                entries.append(FrDispatchEntry(
                    slot_id=ft.slot_id,
                    channel_idx=ft.channel_idx,
                    pdu_name=pdu_name,
                    pdu_byte_offset=pdu_byte_offset,
                    pdu_byte_length=pdu_obj.byte_length,
                    proto_buf_max=num_fields * 10 + 16,
                ))
    # deduplicate
    seen: Set[tuple] = set()
    unique = []
    for e in entries:
        key = (e.slot_id, e.channel_idx, e.pdu_name)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


# ===========================================================================
# File writing helpers
# ===========================================================================

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"  wrote: {path}")


def _render(env: Environment, template_name: str, data: dict) -> str:
    return env.get_template(template_name).render(**data)


# ===========================================================================
# CAN signal resolution
# ===========================================================================

def _resolve_all_can_signals(db: DbcDb) -> Dict[str, List[CanField]]:
    result: Dict[str, List[CanField]] = {}
    for msg_name, msg in db.messages.items():
        if not msg.signals:
            continue
        signals = sorted(msg.signals.values(), key=lambda s: s.start_bit)
        fields = [CanField(fn + 1, sig, sig.proto_type)
                  for fn, sig in enumerate(signals)]
        result[msg_name] = fields
    return result


def _load_csv(csv_path: str) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sig = row.get('signal_name', '').strip()
            msg = (row.get('message_name') or row.get('pdu_name') or '').strip()
            if sig and msg:
                rows.append((sig, msg))
    return rows


def _resolve_can_from_csv(
    db: DbcDb, csv_rows: List[Tuple[str, str]]
) -> Dict[str, List[CanField]]:
    by_msg: Dict[str, Dict[str, DbcSignal]] = {}
    for sig_name, msg_name in csv_rows:
        msg = db.lookup_by_name(msg_name)
        if msg is None:
            print(f"  WARNING: Message '{msg_name}' not found in DBC", file=sys.stderr)
            continue
        sig = msg.signals.get(sig_name)
        if sig is None:
            print(f"  WARNING: Signal '{sig_name}' not found in '{msg_name}'", file=sys.stderr)
            continue
        by_msg.setdefault(msg_name, {})[sig_name] = sig

    result: Dict[str, List[CanField]] = {}
    for msg_name, sig_map in by_msg.items():
        signals = sorted(sig_map.values(), key=lambda s: s.start_bit)
        result[msg_name] = [CanField(fn + 1, sig, sig.proto_type)
                            for fn, sig in enumerate(signals)]
    return result


# ===========================================================================
# Inline code generation (without Jinja2 templates for shared paths)
# Templates used for per-bus paths mirror can_to_nanopb.py / fibex_to_nanopb.py
# ===========================================================================

def _shared_can_encode_c(msg_name: str, msg: DbcMessage,
                          fields: List[CanField]) -> str:
    """Generate a shared (no bus-prefix) CAN encode function."""
    lines = [
        "/* AUTO-GENERATED — DO NOT EDIT */",
        f"/* Shared encoder for CAN layout '{msg_name}' (ID=0x{msg.can_id:X}) */",
        "/* Generated by gen_platform_protos.py — layout identical on multiple buses */",
        "",
        "#include <string.h>",
        "#include <stdint.h>",
        '#include "cmp_proto_wire.h"',
        '#include "cmp_extract.h"',
        "",
        f"size_t cmp_encode_shared_{msg_name}(const uint8_t* data, size_t data_len,",
        f"                                  uint8_t* out, size_t out_size) {{",
        f"    if (!data || data_len < {msg.dlc}u) return 0;",
        "    cmp_wire_t w;",
        "    cmp_wire_init(&w, out, out_size);",
        "",
    ]
    for f in fields:
        sig = f.signal
        pt  = f.proto_type
        lines.append(f"    /* field {f.field_number}: {sig.name} — "
                     f"bit={sig.start_bit}, len={sig.bit_length}, "
                     f"{'Motorola' if sig.motorola_byte_order else 'Intel'} */")
        lines.append("    {")
        if sig.motorola_byte_order:
            lines.append(f"        uint64_t _raw = cmp_extract_motorola(data, "
                         f"{sig.start_bit}u, {sig.bit_length}u);")
        else:
            lines.append(f"        uint64_t _raw = cmp_extract_intel(data, "
                         f"{sig.start_bit}u, {sig.bit_length}u);")

        if pt == 'float':
            if sig.is_signed:
                lines.append(f"        int32_t _si = cmp_sign_extend(_raw, {sig.bit_length}u);")
                lines.append(f"        float _phys = (float)_si * {sig.factor}f + {sig.offset}f;")
            else:
                lines.append(f"        float _phys = (float)(uint32_t)_raw * {sig.factor}f + {sig.offset}f;")
            lines.append(f"        cmp_write_float(&w, {f.field_number}u, _phys);")
        elif pt == 'bool':
            lines.append(f"        cmp_write_bool(&w, {f.field_number}u, _raw != 0);")
        elif pt == 'int32':
            lines.append(f"        cmp_write_int32(&w, {f.field_number}u, "
                         f"cmp_sign_extend(_raw, {sig.bit_length}u));")
        elif pt == 'uint64':
            lines.append(f"        cmp_write_uint64(&w, {f.field_number}u, _raw);")
        else:
            lines.append(f"        cmp_write_uint32(&w, {f.field_number}u, (uint32_t)_raw);")
        lines.append("    }")
    lines += [
        "",
        "    return w.overflow ? 0 : w.pos;",
        "}",
        "",
    ]
    return "\n".join(lines)


def _shared_can_decode_c(msg_name: str, msg: DbcMessage,
                          fields: List[CanField]) -> str:
    """Generate a shared (no bus-prefix) CAN decode function."""
    lines = [
        "/* AUTO-GENERATED — DO NOT EDIT */",
        f"/* Shared decoder for CAN layout '{msg_name}' (ID=0x{msg.can_id:X}) */",
        "/* Generated by gen_platform_protos.py — layout identical on multiple buses */",
        "",
        "#include <string.h>",
        "#include <stdint.h>",
        "#include <math.h>",
        '#include "cmp_wire_reader.h"',
        '#include "cmp_write_bits.h"',
        "",
        f"size_t cmp_decode_shared_{msg_name}(",
        f"    const uint8_t* proto_buf, size_t proto_len,",
        f"    uint8_t* pdu_out, size_t pdu_size)",
        "{",
        f"    if (!proto_buf || !pdu_out || pdu_size < {msg.dlc}u) return 0u;",
        f"    memset(pdu_out, 0, {msg.dlc}u);",
        "",
        "    cmp_reader_t r;",
        "    cmp_reader_init(&r, proto_buf, proto_len);",
        "",
        "    uint32_t fn, wt;",
        "    uint64_t val_u64 = 0u;",
        "    float    val_f32 = 0.0f;",
        "",
        "    while (cmp_read_field(&r, &fn, &wt, &val_u64, &val_f32)) {",
        "        switch (fn) {",
    ]
    for f in fields:
        sig = f.signal
        pt  = f.proto_type
        lines.append(f"        case {f.field_number}u: "
                     f"/* {sig.name} bit={sig.start_bit} len={sig.bit_length} */")
        lines.append("        {")
        if pt == 'float':
            if sig.is_signed:
                lines.append(f"            int32_t _raws = (int32_t)roundf("
                             f"(val_f32 - {sig.offset}f) / {sig.factor}f);")
                lines.append("            uint64_t _raw = (uint64_t)(uint32_t)_raws;")
            else:
                lines.append(f"            uint64_t _raw = (uint64_t)(uint32_t)roundf("
                             f"(val_f32 - {sig.offset}f) / {sig.factor}f);")
        else:
            lines.append("            uint64_t _raw = val_u64;")

        if sig.motorola_byte_order:
            lines.append(f"            cmp_write_bits_motorola(pdu_out, "
                         f"{sig.start_bit}u, {sig.bit_length}u, _raw);")
        else:
            lines.append(f"            cmp_write_bits_intel(pdu_out, "
                         f"{sig.start_bit}u, {sig.bit_length}u, _raw);")
        lines.append("            break;")
        lines.append("        }")
    lines += [
        "        default: break;",
        "        }",
        "    }",
        f"    return r.error ? 0u : {msg.dlc}u;",
        "}",
        "",
    ]
    return "\n".join(lines)


def _shared_proto(msg_name: str, msg: DbcMessage,
                  fields: List[CanField]) -> str:
    """Generate proto3 file with package shared;"""
    lines = [
        f"// AUTO-GENERATED by gen_platform_protos.py — DO NOT EDIT",
        f"// Shared layout: '{msg_name}' — same signal layout on multiple buses",
        "",
        "syntax = \"proto3\";",
        "package shared;",
        "",
        f"message {msg_name} {{",
    ]
    for f in fields:
        sig = f.signal
        comment_parts = [f"bit={sig.start_bit}", f"len={sig.bit_length}"]
        if sig.factor != 1.0 or sig.offset != 0.0:
            comment_parts.append(f"scale={sig.factor} offset={sig.offset}")
        if sig.unit:
            comment_parts.append(f'unit="{sig.unit}"')
        lines.append(f"    {f.proto_type} {sig.name} = {f.field_number};"
                     f"  // {' '.join(comment_parts)}")
    lines += ["}", ""]
    return "\n".join(lines)


def _bus_dispatch_table_c_with_shared(
    namespace: str,
    msg_names_bus_specific: List[str],
    entries_bus_specific: List[dict],
    entries_shared: List[dict],
) -> str:
    """
    Generate can_dispatch_table.c for a bus where some messages use shared codecs
    and others use bus-specific codecs.

    entries_shared items have 'codec_name' = canonical owner's message name
    (may differ from 'message_name' when canonicals differ in case/suffix).
    """
    # Collect unique canonical codec names used by this bus's shared entries
    seen_codec: Set[str] = set()
    unique_codec_names: List[str] = []
    for e in entries_shared:
        cn = e['codec_name']
        if cn not in seen_codec:
            seen_codec.add(cn)
            unique_codec_names.append(cn)

    lines = [
        "/* AUTO-GENERATED — DO NOT EDIT */",
        f'#include "can_dispatch_table.h"',
        "#include <stddef.h>",
        "",
        "/* Bus-specific codec declarations */",
    ]
    for name in msg_names_bus_specific:
        lines.append(f"extern size_t cmp_can_encode_{namespace}_{name}"
                     "(const uint8_t*, size_t, uint8_t*, size_t);")
    for name in msg_names_bus_specific:
        lines.append(f"extern size_t cmp_decode_{namespace}_{name}"
                     "(const uint8_t*, size_t, uint8_t*, size_t);")

    if unique_codec_names:
        lines += ["", "/* Shared codec declarations (canonical owner's function) */"]
        for cn in unique_codec_names:
            lines.append(f"extern size_t cmp_encode_shared_{cn}"
                         "(const uint8_t*, size_t, uint8_t*, size_t);  /* shared */")
            lines.append(f"extern size_t cmp_decode_shared_{cn}"
                         "(const uint8_t*, size_t, uint8_t*, size_t);  /* shared */")

    lines += [
        "",
        "static const cmp_can_dispatch_entry_t _can_table[] = {",
    ]

    # bus-specific entries
    for e in entries_bus_specific:
        can_id = e['can_id']
        ext    = 1 if e['is_extended'] else 0
        name   = e['message_name']
        dlc    = e['dlc']
        pbmax  = e['proto_buf_max']
        lines.append(
            f"    {{ 0x{can_id:X}u, {ext}u, \"{name}\", \"{namespace}.{name}\","
            f"  cmp_can_encode_{namespace}_{name}, cmp_decode_{namespace}_{name},"
            f" {dlc}u, {pbmax}u }},"
        )

    # shared entries — use codec_name (canonical) not message_name (DBC name)
    for e in entries_shared:
        can_id = e['can_id']
        ext    = 1 if e['is_extended'] else 0
        name   = e['message_name']
        cn     = e['codec_name']   # canonical owner's name → function name
        dlc    = e['dlc']
        pbmax  = e['proto_buf_max']
        lines.append(
            f"    {{ 0x{can_id:X}u, {ext}u, \"{name}\", \"shared.{cn}\","
            f"  cmp_encode_shared_{cn}, cmp_decode_shared_{cn},"
            f" {dlc}u, {pbmax}u }},  /* shared codec */"
        )

    lines += [
        "};",
        "",
        f"/* Namespace-prefixed accessors for bus '{namespace}' */",
        f"const cmp_can_dispatch_entry_t* cmp_can_{namespace}_get_table(void)"
        " { return _can_table; }",
        f"size_t cmp_can_{namespace}_get_count(void)"
        " { return sizeof(_can_table)/sizeof(_can_table[0]); }",
        f'const char* cmp_can_{namespace}_get_ns(void) {{ return "{namespace}"; }}',
        "",
    ]
    return "\n".join(lines)


# ===========================================================================
# FlexRay dispatch table with shared codec support
# ===========================================================================

def _fr_dispatch_table_c_with_shared(
    namespace:    str,
    ns_pdu_names: List[str],     # non-shared PDUs → cmp_encode_{namespace}_{name}
    entries:      List[FrDispatchEntry],
    fibex_version: str,
) -> str:
    # Collect the unique canonical codec names used by shared entries
    shared_codec_names: List[str] = []
    seen_shared: Set[str] = set()
    for e in entries:
        if e.is_shared and e.codec_name not in seen_shared:
            seen_shared.add(e.codec_name)
            shared_codec_names.append(e.codec_name)

    lines = [
        "/* AUTO-GENERATED — DO NOT EDIT */",
        '#include "dispatch_table.h"',
        "#include <stddef.h>",
        "",
        "/* FlexRay-specific codec declarations */",
    ]
    for name in ns_pdu_names:
        lines.append(f"extern size_t cmp_encode_{namespace}_{name}"
                     "(const uint8_t*, size_t, uint8_t*, size_t);")
    for name in ns_pdu_names:
        lines.append(f"extern size_t cmp_decode_{namespace}_{name}"
                     "(const uint8_t*, size_t, uint8_t*, size_t);")

    if shared_codec_names:
        lines += ["", "/* Shared codec declarations (canonical owner's function) */"]
        for name in shared_codec_names:
            lines.append(f"extern size_t cmp_encode_shared_{name}"
                         "(const uint8_t*, size_t, uint8_t*, size_t);  /* shared */")
            lines.append(f"extern size_t cmp_decode_shared_{name}"
                         "(const uint8_t*, size_t, uint8_t*, size_t);  /* shared */")

    lines += ["", "static const cmp_dispatch_entry_t _table[] = {"]

    for e in entries:
        if e.is_shared:
            # codec_name is the canonical PDU name (may differ from pdu_name)
            enc_fn   = f"cmp_encode_shared_{e.codec_name}"
            dec_fn   = f"cmp_decode_shared_{e.codec_name}"
            proto_ns = "shared"
            # proto type uses codec_name (canonical) not pdu_name (alias)
            proto_msg = e.codec_name
        else:
            enc_fn   = f"cmp_encode_{namespace}_{e.pdu_name}"
            dec_fn   = f"cmp_decode_{namespace}_{e.pdu_name}"
            proto_ns  = namespace
            proto_msg = e.pdu_name
        lines.append(
            f"    {{ {e.slot_id}u, {e.channel_idx}u, "
            f"\"{e.pdu_name}\", \"{proto_ns}.{proto_msg}\","
            f"  {enc_fn}, {dec_fn},"
            f" {e.pdu_byte_offset}u, {e.pdu_byte_length}u, {e.proto_buf_max}u }},"
        )

    lines += [
        "};",
        "",
        "const cmp_dispatch_entry_t* cmp_get_dispatch_table(void) { return _table; }",
        "size_t cmp_get_dispatch_count(void)"
        " { return sizeof(_table)/sizeof(_table[0]); }",
        f'const char* cmp_get_fibex_version(void) {{ return "{fibex_version}"; }}',
        "",
    ]
    return "\n".join(lines)


# ===========================================================================
# PSP registry generation
# ===========================================================================

def _write_psp_registry(out_src: str, can_namespaces: List[str],
                         lib_include: str) -> None:
    """Write psp_can_registry.c and .h aggregating all CAN buses."""
    # .h
    h_lines = [
        "/* AUTO-GENERATED — DO NOT EDIT — PSP CAN bus registry */",
        "#pragma once",
        '#include "cmp_plugin.h"',
        "",
        "typedef struct {",
        "    const char*  bus_ns;",
        "    const cmp_can_dispatch_entry_t* (*get_table)(void);",
        "    size_t                           (*get_count)(void);",
        "} psp_can_bus_t;",
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
        "const psp_can_bus_t*             psp_can_get_buses(void);",
        "size_t                           psp_can_get_bus_count(void);",
        "const cmp_can_dispatch_entry_t*  psp_can_lookup(uint32_t can_id);",
        "const cmp_can_dispatch_entry_t*  psp_can_decode_lookup(uint32_t can_id);",
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
    ]
    _write(os.path.join(out_src, "psp_can_registry.h"), "\n".join(h_lines))

    # .c
    c_lines = [
        "/* AUTO-GENERATED — DO NOT EDIT — PSP CAN bus registry */",
        '#include "psp_can_registry.h"',
        "#include <stddef.h>",
        "",
    ]
    for ns in can_namespaces:
        c_lines.append(f"extern const cmp_can_dispatch_entry_t* cmp_can_{ns}_get_table(void);")
        c_lines.append(f"extern size_t                           cmp_can_{ns}_get_count(void);")
    c_lines += [
        "",
        "static const psp_can_bus_t _buses[] = {",
    ]
    for ns in can_namespaces:
        c_lines.append(f'    {{ "{ns}", cmp_can_{ns}_get_table, cmp_can_{ns}_get_count }},')
    c_lines += [
        "};",
        "",
        "const psp_can_bus_t* psp_can_get_buses(void)    { return _buses; }",
        "size_t psp_can_get_bus_count(void)"
        " { return sizeof(_buses)/sizeof(_buses[0]); }",
        "",
        "const cmp_can_dispatch_entry_t* psp_can_lookup(uint32_t can_id) {",
        "    size_t b, i;",
        "    for (b = 0; b < sizeof(_buses)/sizeof(_buses[0]); b++) {",
        "        const cmp_can_dispatch_entry_t* t = _buses[b].get_table();",
        "        size_t n = _buses[b].get_count();",
        "        for (i = 0; i < n; i++)",
        "            if (t[i].can_id == can_id) return &t[i];",
        "    }",
        "    return (const cmp_can_dispatch_entry_t*)0;",
        "}",
        "",
        "const cmp_can_dispatch_entry_t* psp_can_decode_lookup(uint32_t can_id) {",
        "    return psp_can_lookup(can_id);",
        "}",
        "",
    ]
    _write(os.path.join(out_src, "psp_can_registry.c"), "\n".join(c_lines))


# ===========================================================================
# Main generator
# ===========================================================================

def generate(
    fibex_path:   Optional[str],
    dbc_specs:    List[Tuple[str, str]],  # [(path, bus_name), ...]
    namespace_fr: str,
    out_src:      str,
    out_proto:    str,
    all_signals:  bool,
    csv_path:     Optional[str],
    encode_only:  bool,
    decode_only:  bool,
) -> None:

    templates_dir = os.path.join(_THIS_DIR, 'templates')
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )

    # ── fingerprint registry ─────────────────────────────────────────────────
    # fingerprint → CanonicalEntry
    fingerprint_registry: Dict[tuple, CanonicalEntry] = {}

    # ── Step 1: Parse FIBEX (FlexRay) ────────────────────────────────────────
    fr_pdu_data: Dict[str, Tuple] = {}   # pdu_name → (pdu_obj, fields)
    fr_dispatch: Dict[str, List[FrDispatchEntry]] = {}
    fibex_version = ''
    fr_db: Optional[FibexDb] = None

    if fibex_path:
        print(f"Parsing FIBEX: {fibex_path}")
        fr_db = FibexDb()
        fr_db.load(fibex_path)
        fibex_version = fr_db.fibex_version or os.path.basename(fibex_path)
        print(f"  FlexRay: {len(fr_db.frames)} frames, "
              f"{len(fr_db.pdus)} PDUs, {len(fr_db.signals)} signals")

        fr_pdu_data = _resolve_all_fr_pdus(fr_db)
        print(f"  APPLICATION PDUs with signals: {len(fr_pdu_data)}")

        for pdu_name, (pdu_obj, fields) in fr_pdu_data.items():
            si_list = [item[1] for item in fields]  # (fn, si, sig, cod, pt)
            fp = fr_layout_fingerprint(si_list)
            de_list = _fr_dispatch_entries(fr_db, pdu_name, pdu_obj, len(fields))
            fr_dispatch[pdu_name] = de_list

            entry = CanonicalEntry(
                fingerprint=fp,
                name=pdu_name,
                namespace=namespace_fr,
                bus_name='flexray',
                pdu=pdu_obj,
                fr_fields=fields,
            )
            if fp not in fingerprint_registry:
                fingerprint_registry[fp] = entry
            else:
                # Already defined by another FlexRay PDU or a CAN bus
                fingerprint_registry[fp].is_shared = True
                entry.is_shared = True
                print(f"  [DEDUP] FlexRay PDU '{pdu_name}' shares layout with "
                      f"'{fingerprint_registry[fp].name}'")

    # ── Step 2: Parse DBC files (CAN) ────────────────────────────────────────
    # dbc_bus_name → DbcDb
    can_dbs: Dict[str, DbcDb] = {}
    # bus_name → {msg_name → List[CanField]}
    can_fields_by_bus: Dict[str, Dict[str, List[CanField]]] = {}

    for dbc_path, bus_name in dbc_specs:
        print(f"Parsing DBC ({bus_name}): {dbc_path}")
        db = DbcDb()
        db.load(dbc_path, bus_name=bus_name)
        can_dbs[bus_name] = db
        print(f"  {bus_name}: {db.message_count()} messages, {db.signal_count()} signals")

        if all_signals:
            msg_fields = _resolve_all_can_signals(db)
        elif csv_path:
            csv_rows = _load_csv(csv_path)
            msg_fields = _resolve_can_from_csv(db, csv_rows)
        else:
            msg_fields = _resolve_all_can_signals(db)

        can_fields_by_bus[bus_name] = msg_fields

        namespace = f"can_{bus_name}"
        for msg_name, fields in msg_fields.items():
            msg = db.lookup_by_name(msg_name)
            assert msg is not None
            fp = can_layout_fingerprint(list(msg.signals.values()))
            if fp in fingerprint_registry:
                fingerprint_registry[fp].is_shared = True
                print(f"  [DEDUP] {bus_name}.{msg_name} shares layout with "
                      f"{fingerprint_registry[fp].bus_name}."
                      f"{fingerprint_registry[fp].name}")
            else:
                fingerprint_registry[fp] = CanonicalEntry(
                    fingerprint=fp,
                    name=msg_name,
                    namespace=namespace,
                    bus_name=bus_name,
                    msg=msg,
                    fields=fields,
                )

    # ── Step 3: Generate FlexRay outputs ─────────────────────────────────────
    if fibex_path and fr_db is not None:
        print("\n--- Generating FlexRay outputs ---")
        fr_src_dir   = os.path.join(out_src, 'flexray')
        fr_proto_dir = os.path.join(out_proto, 'flexray')
        os.makedirs(fr_src_dir, exist_ok=True)
        os.makedirs(fr_proto_dir, exist_ok=True)

        fr_all_dispatch: List[FrDispatchEntry] = []
        fr_ns_pdu_names:     List[str] = []   # non-shared → namespace codec
        fr_shared_pdu_names: List[str] = []   # shared → cmp_encode_shared_

        pdu_by_name: Dict[str, PduInfo] = {}
        for pdu in fr_db.pdus.values():
            if pdu.name and pdu.name not in pdu_by_name:
                pdu_by_name[pdu.name] = pdu

        for pdu_name, (pdu_obj, fields) in fr_pdu_data.items():
            fp = fr_layout_fingerprint([item[1] for item in fields])
            canon = fingerprint_registry.get(fp)
            is_shared = canon.is_shared if canon else False

            if is_shared:
                shared_src_dir   = os.path.join(out_src,   'shared')
                shared_proto_dir = os.path.join(out_proto,  'shared')

                # Only the canonical owner writes the shared codec files.
                # canon.bus_name == 'flexray' means FlexRay is canonical;
                # otherwise a CAN bus already wrote can_encode_{name}.c.
                if canon.name == pdu_name and canon.bus_name == 'flexray':
                    if not decode_only:
                        encode_tmpl = env.get_template('pdu_encode.c.j2')
                        rendered = encode_tmpl.render(
                            fibex_version=fibex_version, namespace='shared',
                            pdu=pdu_obj, fields=fields,
                        )
                        _write(os.path.join(shared_src_dir, f'encode_{pdu_name}.c'), rendered)

                    if not encode_only:
                        decode_tmpl = env.get_template('fr_decode.c.j2')
                        rendered_d = decode_tmpl.render(
                            fibex_version=fibex_version, namespace='shared',
                            pdu=pdu_obj, fields=fields,
                        )
                        _write(os.path.join(shared_src_dir, f'decode_{pdu_name}.c'), rendered_d)

                    proto_tmpl = env.get_template('proto.j2')
                    rendered_p = proto_tmpl.render(
                        fibex_version=fibex_version, namespace='shared',
                        pdu=pdu_obj, fields=fields,
                    )
                    _write(os.path.join(shared_proto_dir, f'{pdu_name}.proto'), rendered_p)

                if pdu_name not in fr_shared_pdu_names:
                    fr_shared_pdu_names.append(pdu_name)
            else:
                # Bus-specific FlexRay codec
                if not decode_only:
                    encode_tmpl = env.get_template('pdu_encode.c.j2')
                    rendered = encode_tmpl.render(
                        fibex_version=fibex_version,
                        namespace=namespace_fr,
                        pdu=pdu_obj,
                        fields=fields,
                    )
                    _write(os.path.join(fr_src_dir, f'encode_{pdu_name}.c'), rendered)

                if not encode_only:
                    decode_tmpl = env.get_template('fr_decode.c.j2')
                    rendered_d = decode_tmpl.render(
                        fibex_version=fibex_version,
                        namespace=namespace_fr,
                        pdu=pdu_obj,
                        fields=fields,
                    )
                    _write(os.path.join(fr_src_dir, f'decode_{pdu_name}.c'), rendered_d)

                # Per-FlexRay proto
                proto_tmpl = env.get_template('proto.j2')
                rendered_p = proto_tmpl.render(
                    fibex_version=fibex_version,
                    namespace=namespace_fr,
                    pdu=pdu_obj,
                    fields=fields,
                )
                _write(os.path.join(fr_proto_dir, f'{pdu_name}.proto'), rendered_p)

                if pdu_name not in fr_ns_pdu_names:
                    fr_ns_pdu_names.append(pdu_name)

            # Dispatch entries: mark shared flag and canonical codec name.
            # For shared PDUs, use the canonical name so the extern resolves correctly
            # (e.g. DEV_X_03_B → codec_name='DEV_X_03' because DEV_X_03 is canonical).
            canonical_name = canon.name if (is_shared and canon) else pdu_name
            for de in fr_dispatch.get(pdu_name, []):
                de.is_shared  = is_shared
                de.codec_name = canonical_name
                fr_all_dispatch.append(de)

        # FlexRay dispatch table — uses shared codec names for shared PDUs
        if not encode_only:
            dt_h = env.get_template('dispatch_table.h.j2').render(namespace=namespace_fr)
            _write(os.path.join(fr_src_dir, 'dispatch_table.h'), dt_h)

        dt_c = _fr_dispatch_table_c_with_shared(
            namespace=namespace_fr,
            ns_pdu_names=fr_ns_pdu_names,
            entries=fr_all_dispatch,
            fibex_version=fibex_version,
        )
        _write(os.path.join(fr_src_dir, 'dispatch_table.c'), dt_c)

        # FlexRay hercules_filter.h
        slot_set = sorted({e.slot_id for e in fr_all_dispatch})
        hfh = env.get_template('hercules_filter.h.j2').render(
            namespace=namespace_fr,
            slots=slot_set,
            fibex_version=fibex_version,
        )
        _write(os.path.join(fr_src_dir, 'hercules_filter.h'), hfh)

        total_fr = len(fr_ns_pdu_names) + len(fr_shared_pdu_names)
        print(f"  FlexRay: {total_fr} PDUs ({len(fr_shared_pdu_names)} shared), "
              f"{len(fr_all_dispatch)} dispatch entries, "
              f"slots: {slot_set}")

    # ── Step 4: Generate CAN outputs ─────────────────────────────────────────
    all_can_namespaces: List[str] = []

    for bus_name, msg_fields in can_fields_by_bus.items():
        print(f"\n--- Generating CAN outputs for {bus_name} ---")
        namespace = f"can_{bus_name}"
        all_can_namespaces.append(namespace)

        bus_src_dir   = os.path.join(out_src,   'can', bus_name)
        bus_proto_dir = os.path.join(out_proto,  'can', bus_name)
        os.makedirs(bus_src_dir,   exist_ok=True)
        os.makedirs(bus_proto_dir, exist_ok=True)

        db = can_dbs[bus_name]

        # Classify messages: shared vs bus-specific
        bus_specific_names: List[str] = []
        shared_names:       List[str] = []
        bus_specific_entries: List[dict] = []
        shared_entries:       List[dict] = []

        for msg_name, fields in msg_fields.items():
            msg = db.lookup_by_name(msg_name)
            assert msg is not None
            fp    = can_layout_fingerprint(list(msg.signals.values()))
            canon = fingerprint_registry.get(fp)
            pbmax = _proto_buf_max_can(fields)

            entry_d = dict(
                can_id=msg.can_id,
                is_extended=msg.is_extended,
                message_name=msg_name,
                dlc=msg.dlc,
                proto_buf_max=pbmax,
            )

            if canon is not None and canon.is_shared:
                shared_names.append(msg_name)
                # codec_name = canonical owner's message name (may differ from msg_name)
                entry_d['codec_name'] = canon.name
                shared_entries.append(entry_d)

                # Write shared codecs only if we are the canonical owner.
                # For FlexRay-canonical shared: canon.bus_name='flexray', generated in Step 3.
                # For CAN-canonical shared: canon.bus_name=bus_name AND name matches.
                if canon.name == msg_name and canon.bus_name == bus_name:
                    shared_src_dir   = os.path.join(out_src,   'shared')
                    shared_proto_dir = os.path.join(out_proto,  'shared')

                    if not decode_only:
                        _write(os.path.join(shared_src_dir, f'can_encode_{msg_name}.c'),
                               _shared_can_encode_c(msg_name, msg, fields))

                    if not encode_only:
                        _write(os.path.join(shared_src_dir, f'can_decode_{msg_name}.c'),
                               _shared_can_decode_c(msg_name, msg, fields))

                    _write(os.path.join(shared_proto_dir, f'{msg_name}.proto'),
                           _shared_proto(msg_name, msg, fields))
            else:
                bus_specific_names.append(msg_name)
                bus_specific_entries.append(entry_d)

                # Bus-specific encode
                tmpl_data = dict(
                    namespace=namespace,
                    dbc_source=os.path.basename(db.source_file),
                    msg=msg,
                    fields=[(f.field_number, f.signal, f.proto_type) for f in fields],
                )
                if not decode_only:
                    enc_c = env.get_template('can_encode.c.j2').render(**tmpl_data)
                    _write(os.path.join(bus_src_dir, f'can_encode_{msg_name}.c'), enc_c)

                if not encode_only:
                    dec_c = env.get_template('can_decode.c.j2').render(**tmpl_data)
                    _write(os.path.join(bus_src_dir, f'can_decode_{msg_name}.c'), dec_c)

                # Bus-specific proto
                proto_c = env.get_template('can_proto.j2').render(**tmpl_data)
                _write(os.path.join(bus_proto_dir, f'{msg_name}.proto'), proto_c)

        # ── Dispatch table header ────────────────────────────────────────────
        dt_h_content = env.get_template('can_dispatch_table.h.j2').render(namespace=namespace)
        _write(os.path.join(bus_src_dir, 'can_dispatch_table.h'), dt_h_content)

        # ── Dispatch table .c — mixed shared + bus-specific ─────────────────
        dt_c_content = _bus_dispatch_table_c_with_shared(
            namespace=namespace,
            msg_names_bus_specific=bus_specific_names,
            entries_bus_specific=bus_specific_entries,
            entries_shared=shared_entries,
        )
        _write(os.path.join(bus_src_dir, 'can_dispatch_table.c'), dt_c_content)

        # ── ns_wrapper.h ─────────────────────────────────────────────────────
        all_names = bus_specific_names + shared_names
        ns_w = env.get_template('can_ns_wrapper.h.j2').render(
            namespace=namespace,
            msg_names=all_names,
            entries=bus_specific_entries + shared_entries,
            plugin_include_dir='${CMAKE_SOURCE_DIR}/lib/include',
            dbc_source=os.path.basename(db.source_file),
        )
        _write(os.path.join(bus_src_dir, 'ns_wrapper.h'), ns_w)

        # ── psp_manifest.json ────────────────────────────────────────────────
        manifest_entries = []
        for e in bus_specific_entries:
            name = e['message_name']
            manifest_entries.append({
                'message_name': name,
                'can_id':       e['can_id'],
                'dlc':          e['dlc'],
                'proto_type':   f"{namespace}.{name}",
                'encode_fn':    f"cmp_can_encode_{namespace}_{name}",
                'codec':        'bus_specific',
            })
        for e in shared_entries:
            name = e['message_name']
            cn   = e['codec_name']   # canonical name → actual function name
            manifest_entries.append({
                'message_name': name,
                'can_id':       e['can_id'],
                'dlc':          e['dlc'],
                'proto_type':   f"shared.{cn}",
                'encode_fn':    f"cmp_encode_shared_{cn}",
                'decode_fn':    f"cmp_decode_shared_{cn}",
                'codec':        'shared',
            })
        manifest = {
            'generator':    'gen_platform_protos',
            'namespace':    namespace,
            'source_dbc':   db.source_file,
            'messages':     manifest_entries,
        }
        mpath = os.path.join(bus_src_dir, 'psp_manifest.json')
        with open(mpath, 'w', encoding='utf-8') as mf:
            json.dump(manifest, mf, indent=2)
        print(f"  wrote: {mpath}")

        print(f"  {bus_name}: {len(bus_specific_names)} bus-specific, "
              f"{len(shared_names)} shared messages")

    # ── Step 5: PSP CAN registry ─────────────────────────────────────────────
    if all_can_namespaces:
        print(f"\n--- Generating PSP CAN registry ---")
        lib_include = os.path.join(
            os.path.dirname(_THIS_DIR), 'lib', 'include')
        _write_psp_registry(out_src, all_can_namespaces, lib_include)

    # ── Summary ──────────────────────────────────────────────────────────────
    total_shared = sum(1 for e in fingerprint_registry.values() if e.is_shared)
    total_unique = len(fingerprint_registry) - total_shared
    print(f"\n=== gen_platform_protos summary ===")
    print(f"  Fingerprints total : {len(fingerprint_registry)}")
    print(f"    Shared layouts   : {total_shared}")
    print(f"    Unique layouts   : {total_unique}")
    print(f"  CAN buses          : {list(can_fields_by_bus.keys())}")
    if fibex_path:
        print(f"  FlexRay namespace  : {namespace_fr}")
    print(f"  out-src            : {out_src}")
    print(f"  out-proto          : {out_proto}")
    print("Done.")


# ===========================================================================
# CLI
# ===========================================================================

def _parse_dbc_spec(spec: str) -> Tuple[str, str]:
    """Parse 'path/to/file.dbc:bus_name' → (path, bus_name)."""
    if ':' not in spec:
        # infer bus name from filename stem
        stem = os.path.splitext(os.path.basename(spec))[0].lower()
        return (spec, stem)
    path, bus = spec.rsplit(':', 1)
    return (path, bus.strip())


