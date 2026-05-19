#!/usr/bin/env python3
"""
fibex_to_nanopb.py  (tools/ version)

Generates C/proto source files for the CMP signal decode pipeline.

Usage:
    python3 tools/fibex_to_nanopb.py \\
        --fibex path/to/cluster.xml \\
        --csv   path/to/signals.csv \\
        --namespace mlbevo_v8_17 \\
        --out   generated/mlbevo_v8_17/

    # Generate for ALL PDUs (skip CSV):
    python3 tools/fibex_to_nanopb.py \\
        --fibex path/to/cluster.xml \\
        --namespace mlbevo_gen2 --all-signals --out src/flexray/

Outputs (in --out directory):
    <pdu>.proto           — proto3 message per used PDU
    encode_<pdu>.c        — FlexRay → proto wire encoder
    dispatch_table.h      — build-time dispatch table declaration
    dispatch_table.c      — dispatch table definition + extern declarations
    ns_wrapper.h          — C++ using-type aliases
    hercules_filter.h     — static slot list header for the app
    psp_manifest.json     — machine-readable manifest for gen_app_dispatch
    CMakeLists.txt        — builds libcmp_<namespace>.so
"""

from __future__ import annotations

import csv
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    sys.exit("ERROR: jinja2 is required.  Run: pip install jinja2")

# Support running from tools/ or tools/generator/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

from ..importers._asam_cmp_parser import (
    CodingInfo, FibexDb, FrameTrigger, PduInfo,
    SignalInfo, SignalInstance,
)


# ---------------------------------------------------------------------------
# Proto type determination
# ---------------------------------------------------------------------------

def _proto_type(coding: CodingInfo) -> str:
    if coding.scale != 1.0 or coding.offset != 0.0:
        return "float"
    if coding.encoding == "FLOAT":
        return "float"
    enc = coding.encoding
    bl  = coding.bit_length
    m   = coding.method
    if m == "TEXTTABLE":
        return "uint32"
    if enc == "SIGNED":
        return "int32" if bl <= 32 else "int64"
    if bl == 1:
        return "bool"
    return "uint32" if bl <= 32 else "uint64"


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_csv(csv_path: str) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sig = row.get("signal_name", "").strip()
            pdu = row.get("pdu_name", "").strip()
            if sig and pdu:
                entries.append((sig, pdu))
    return entries


# ---------------------------------------------------------------------------
# Signal resolution from CSV
# ---------------------------------------------------------------------------

def resolve_signals(
    db: FibexDb,
    csv_entries: List[Tuple[str, str]],
) -> Dict[str, List[Tuple[SignalInstance, SignalInfo, CodingInfo]]]:
    pdu_by_name: Dict[str, PduInfo] = {}
    for pdu in db.pdus.values():
        if pdu.name and pdu.name not in pdu_by_name:
            pdu_by_name[pdu.name] = pdu

    result: Dict[str, List[Tuple[SignalInstance, SignalInfo, CodingInfo]]] = {}
    missing_signals: List[str] = []
    missing_pdus:    List[str] = []

    for sig_name, pdu_name in csv_entries:
        if pdu_name not in pdu_by_name:
            if pdu_name not in missing_pdus:
                missing_pdus.append(pdu_name)
                print(f"  WARNING: PDU '{pdu_name}' not found in FIBEX", file=sys.stderr)
            continue

        pdu = pdu_by_name[pdu_name]
        found_si: Optional[SignalInstance] = None
        for si in pdu.signal_instances:
            if si.signal is not None and si.signal.name == sig_name:
                found_si = si
                break

        if found_si is None:
            if sig_name not in missing_signals:
                missing_signals.append(sig_name)
                print(f"  WARNING: Signal '{sig_name}' not found in PDU '{pdu_name}'",
                      file=sys.stderr)
            continue

        signal = found_si.signal
        assert signal is not None
        if signal.coding is None:
            print(f"  WARNING: Signal '{sig_name}' has no coding resolved", file=sys.stderr)
            continue

        coding = signal.coding
        pdu_list = result.setdefault(pdu_name, [])
        already = any(s.instance_id == found_si.instance_id for s, _, _ in pdu_list)
        if not already:
            pdu_list.append((found_si, signal, coding))

    for pdu_name in result:
        result[pdu_name].sort(key=lambda t: t[0].bit_position)

    return result


# ---------------------------------------------------------------------------
# Signal resolution from ALL APPLICATION PDUs
# ---------------------------------------------------------------------------

def resolve_all_signals(
    db: FibexDb,
) -> Dict[str, List[Tuple[SignalInstance, SignalInfo, CodingInfo]]]:
    """Return a pdu_signals dict for every APPLICATION PDU with ≥1 signal."""
    result: Dict[str, List[Tuple[SignalInstance, SignalInfo, CodingInfo]]] = {}
    seen_names: set = set()
    for pdu in db.pdus.values():
        if pdu.pdu_type.upper() != 'APPLICATION':
            continue
        if not pdu.signal_instances:
            continue
        # Use first PDU with this name
        if pdu.name in seen_names:
            continue
        seen_names.add(pdu.name)
        entries: List[Tuple[SignalInstance, SignalInfo, CodingInfo]] = []
        for si in pdu.signal_instances:
            signal = si.signal
            if signal is None:
                continue
            coding = signal.coding
            if coding is None:
                continue
            entries.append((si, signal, coding))
        if entries:
            entries.sort(key=lambda t: t[0].bit_position)
            result[pdu.name] = entries
    return result


# ---------------------------------------------------------------------------
# Frame trigger lookup
# ---------------------------------------------------------------------------

def find_frame_triggers_for_pdu(db: FibexDb, pdu_name: str) -> List[FrameTrigger]:
    pdu_obj = next((p for p in db.pdus.values() if p.name == pdu_name), None)
    if pdu_obj is None:
        return []
    frame_ids_with_pdu: set = set()
    for frame in db.frames.values():
        for pi in frame.pdu_instances:
            if pi.pdu is not None and pi.pdu.id == pdu_obj.id:
                frame_ids_with_pdu.add(frame.id)
    return [ft for ft in db.frame_triggers
            if ft.frame is not None and ft.frame.id in frame_ids_with_pdu]


# ---------------------------------------------------------------------------
# Dispatch entry helper
# ---------------------------------------------------------------------------

class DispatchEntry:
    def __init__(self, slot_id: int, channel_idx: int, pdu_name: str,
                 pdu_byte_offset: int, pdu_byte_length: int, num_fields: int) -> None:
        self.slot_id         = slot_id
        self.channel_idx     = channel_idx
        self.pdu_name        = pdu_name
        self.pdu_byte_offset = pdu_byte_offset
        self.pdu_byte_length = pdu_byte_length
        self.proto_buf_max   = num_fields * 10 + 16


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate(
    fibex_path:  str,
    csv_path:    Optional[str],
    namespace:   str,
    out_dir:     str,
    proto_out:   str = None,
    all_signals: bool = False,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    if proto_out and proto_out != out_dir:
        os.makedirs(proto_out, exist_ok=True)

    # Step 1: Parse FIBEX
    print(f"Parsing FIBEX: {fibex_path}")
    db = FibexDb()
    db.load(fibex_path)
    fibex_version = db.fibex_version or os.path.basename(fibex_path)
    print(f"  version     : {fibex_version}")
    print(f"  frames      : {len(db.frames)}")
    print(f"  PDUs        : {len(db.pdus)}")
    print(f"  signals     : {len(db.signals)}")
    print(f"  codings     : {len(db.codings)}")
    print(f"  triggers    : {len(db.frame_triggers)}")

    # Step 2: Resolve signals
    if all_signals:
        print("Mode: --all-signals (skipping CSV)")
        pdu_signals = resolve_all_signals(db)
    else:
        assert csv_path is not None, "--csv is required without --all-signals"
        print(f"Parsing CSV: {csv_path}")
        csv_entries = parse_csv(csv_path)
        print(f"  rows        : {len(csv_entries)}")
        print("Resolving signals …")
        pdu_signals = resolve_signals(db, csv_entries)

    if not pdu_signals:
        print("ERROR: No signals resolved.", file=sys.stderr)
        sys.exit(1)
    print(f"  PDUs with signals: {len(pdu_signals)}")

    # Step 3: Build per-PDU fields
    pdu_fields: Dict[str, list] = {}
    for pdu_name, sig_list in pdu_signals.items():
        fields = []
        for fn, (si, signal, coding) in enumerate(sig_list, start=1):
            pt = _proto_type(coding)
            fields.append((fn, si, signal, coding, pt))
        pdu_fields[pdu_name] = fields

    # Step 4: Build dispatch entries
    dispatch_entries: List[DispatchEntry] = []
    slot_set: set = set()

    pdu_by_name: Dict[str, PduInfo] = {}
    for pdu in db.pdus.values():
        if pdu.name and pdu.name not in pdu_by_name:
            pdu_by_name[pdu.name] = pdu

    for pdu_name in pdu_signals:
        pdu_obj = pdu_by_name.get(pdu_name)
        if pdu_obj is None:
            continue
        triggers = find_frame_triggers_for_pdu(db, pdu_name)
        if not triggers:
            print(f"  WARNING: No FrameTriggers found for PDU '{pdu_name}'", file=sys.stderr)

        for ft in triggers:
            if ft.frame is None:
                continue
            pdu_byte_offset = 0
            for pi in ft.frame.pdu_instances:
                if pi.pdu is not None and pi.pdu.id == pdu_obj.id:
                    pdu_byte_offset = pi.bit_position // 8
                    break
            num_fields = len(pdu_fields.get(pdu_name, []))
            entry = DispatchEntry(
                slot_id=ft.slot_id, channel_idx=ft.channel_idx, pdu_name=pdu_name,
                pdu_byte_offset=pdu_byte_offset, pdu_byte_length=pdu_obj.byte_length,
                num_fields=num_fields,
            )
            dispatch_entries.append(entry)
            slot_set.add(ft.slot_id)

    # Deduplicate
    seen_keys: set = set()
    unique_entries: List[DispatchEntry] = []
    for e in dispatch_entries:
        key = (e.slot_id, e.channel_idx, e.pdu_name)
        if key not in seen_keys:
            seen_keys.add(key)
            unique_entries.append(e)
    dispatch_entries = unique_entries
    slots_sorted = sorted(slot_set)

    # Step 5: Render templates
    templates_dir = os.path.join(_THIS_DIR, 'templates')
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )

    pdu_names = list(pdu_signals.keys())
    plugin_include_dir = "${CMAKE_SOURCE_DIR}/lib/include"
    test_src_dir       = "${CMAKE_SOURCE_DIR}/test"

    # Proto files
    proto_tmpl = env.get_template("proto.j2")
    for pdu_name, sig_list in pdu_signals.items():
        pdu_obj = pdu_by_name.get(pdu_name)
        if pdu_obj is None:
            continue
        rendered = proto_tmpl.render(
            fibex_version=fibex_version, namespace=namespace,
            pdu=pdu_obj, fields=pdu_fields[pdu_name],
        )
        _write(proto_out or out_dir, f"{pdu_name}.proto", rendered)

    # Encode C files
    encode_tmpl = env.get_template("pdu_encode.c.j2")
    decode_tmpl = env.get_template("fr_decode.c.j2")
    for pdu_name, sig_list in pdu_signals.items():
        pdu_obj = pdu_by_name.get(pdu_name)
        if pdu_obj is None:
            continue
        rendered = encode_tmpl.render(
            fibex_version=fibex_version, namespace=namespace,
            pdu=pdu_obj, fields=pdu_fields[pdu_name],
        )
        _write(out_dir, f"encode_{pdu_name}.c", rendered)
        rendered_dec = decode_tmpl.render(
            fibex_version=fibex_version, namespace=namespace,
            pdu=pdu_obj, fields=pdu_fields[pdu_name],
        )
        _write(out_dir, f"decode_{pdu_name}.c", rendered_dec)

    # dispatch_table.h
    dth_tmpl = env.get_template("dispatch_table.h.j2")
    _write(out_dir, "dispatch_table.h", dth_tmpl.render(namespace=namespace))

    # dispatch_table.c
    dtc_tmpl = env.get_template("dispatch_table.c.j2")
    _write(out_dir, "dispatch_table.c", dtc_tmpl.render(
        namespace=namespace, pdu_names=pdu_names,
        entries=dispatch_entries, fibex_version=fibex_version,
    ))

    # ns_wrapper.h
    nsw_tmpl = env.get_template("ns_wrapper.h.j2")
    _write(out_dir, "ns_wrapper.h", nsw_tmpl.render(namespace=namespace, pdu_names=pdu_names))

    # hercules_filter.h
    hfh_tmpl = env.get_template("hercules_filter.h.j2")
    _write(out_dir, "hercules_filter.h", hfh_tmpl.render(
        namespace=namespace, slots=slots_sorted, fibex_version=fibex_version,
    ))

    # CMakeLists.txt
    cmake_tmpl = env.get_template("CMakeLists.j2")
    _write(out_dir, "CMakeLists.txt", cmake_tmpl.render(
        namespace=namespace, pdu_names=pdu_names,
        plugin_include_dir=plugin_include_dir, test_src_dir=test_src_dir,
    ))

    # psp_manifest.json
    _write_manifest(out_dir, namespace, fibex_path, fibex_version,
                    dispatch_entries, pdu_by_name)

    # Summary
    print("\n--- Generation summary ---")
    print(f"  Output dir     : {out_dir}")
    print(f"  Namespace      : {namespace}")
    print(f"  PDUs generated : {len(pdu_names)}")
    print(f"  Dispatch entries : {len(dispatch_entries)}")
    print(f"  Hercules slots   : {slots_sorted}")
    print(f"  FIBEX version    : {fibex_version}")
    print("Done.")


def _write_manifest(out_dir: str, namespace: str, fibex_path: str,
                    fibex_version: str, dispatch_entries: List[DispatchEntry],
                    pdu_by_name: Dict[str, PduInfo]) -> None:
    pdus_list = []
    for e in dispatch_entries:
        pdus_list.append({
            "pdu_name":        e.pdu_name,
            "slot_id":         e.slot_id,
            "channel_idx":     e.channel_idx,
            "pdu_byte_offset": e.pdu_byte_offset,
            "pdu_byte_length": e.pdu_byte_length,
            "proto_type":      f"{namespace}.{e.pdu_name}",
            "encode_fn":       f"cmp_encode_{namespace}_{e.pdu_name}",
        })
    manifest = {
        "generator":    "fibex_to_nanopb",
        "namespace":    namespace,
        "source_fibex": fibex_path,
        "fibex_version": fibex_version,
        "pdus":         pdus_list,
    }
    path = os.path.join(out_dir, "psp_manifest.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
    print(f"  wrote: {path}")


def _write(directory: str, filename: str, content: str) -> None:
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  wrote: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

