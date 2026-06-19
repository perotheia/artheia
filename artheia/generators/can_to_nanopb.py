#!/usr/bin/env python3
"""
can_to_nanopb.py  (tools/ version)

Generate proto3 files, proto-wire CAN encoders, and a dispatch table from a
DBC file + signal selection CSV.  Mirrors fibex_to_nanopb.py for CAN buses.

Usage:
    python3 tools/can_to_nanopb.py \\
        --dbc  configs/dbc/Vehicle_Gen2_Vehicle_KCAN_KMatrix_V8.27.01F.dbc \\
        --csv  test/can_subset_kcan.csv \\
        --namespace can_kcan \\
        --out  generated/can_kcan/

    # Generate for ALL messages (skip CSV):
    python3 tools/can_to_nanopb.py \\
        --dbc  configs/dbc/Vehicle_Gen2_Vehicle_KCAN_KMatrix_V8.27.01F.dbc \\
        --namespace can_kcan --all-signals --out src/can/kcan/

CSV format (header required):
    signal_name,message_name
    ACC_07_CRC,ACC_07
    ACC_07_BZ,ACC_07
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    sys.exit("ERROR: Jinja2 not installed — run: pip install Jinja2")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

from ..importers._asam_cmp_parser import DbcDb, DbcMessage, DbcSignal


# ---------------------------------------------------------------------------
# Field descriptor
# ---------------------------------------------------------------------------

@dataclass
class CanField:
    field_number: int
    signal:       DbcSignal
    proto_type:   str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proto_buf_max(fields: List[CanField]) -> int:
    return len(fields) * 12 + 8


def _load_csv(csv_path: str) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sig = row.get('signal_name', '').strip()
            # accept both 'message_name' (CAN-specific) and 'pdu_name'
            # (unified output from psp_signal_filter.py)
            msg = (row.get('message_name') or row.get('pdu_name') or '').strip()
            if sig and msg:
                rows.append((sig, msg))
    return rows


def _resolve_signals(
    db: DbcDb,
    csv_rows: List[Tuple[str, str]],
) -> Dict[str, List[CanField]]:
    by_msg: Dict[str, List[DbcSignal]] = {}
    for sig_name, msg_name in csv_rows:
        msg = db.lookup_by_name(msg_name)
        if msg is None:
            print(f"  WARNING: Message '{msg_name}' not found in DBC")
            continue
        sig = msg.signals.get(sig_name)
        if sig is None:
            print(f"  WARNING: Signal '{sig_name}' not found in message '{msg_name}'")
            continue
        by_msg.setdefault(msg_name, []).append(sig)

    result: Dict[str, List[CanField]] = {}
    for msg_name, signals in by_msg.items():
        seen: dict = {}
        for sig in signals:
            seen[sig.name] = sig
        signals = sorted(seen.values(), key=lambda s: s.start_bit)
        fields = [CanField(fn + 1, sig, sig.proto_type) for fn, sig in enumerate(signals)]
        result[msg_name] = fields

    return result


def _resolve_all_signals(db: DbcDb) -> Dict[str, List[CanField]]:
    """Return a msg_fields dict for every message with ≥1 signal."""
    result: Dict[str, List[CanField]] = {}
    for msg_name, msg in db.messages.items():
        if not msg.signals:
            continue
        signals = sorted(msg.signals.values(), key=lambda s: s.start_bit)
        fields = [CanField(fn + 1, sig, sig.proto_type) for fn, sig in enumerate(signals)]
        result[msg_name] = fields
    return result


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate(
    dbc_path:    str,
    csv_path:    Optional[str],
    namespace:   str,
    out_dir:     str,
    all_signals: bool = False,
    proto_out:   str = None,
    plugin_include_dir: Optional[str] = None,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    _proto_dir = proto_out or out_dir
    if _proto_dir != out_dir:
        os.makedirs(_proto_dir, exist_ok=True)

    print(f"Parsing DBC: {dbc_path}")
    db = DbcDb()
    db.load(dbc_path)
    print(f"  messages  : {db.message_count()}")
    print(f"  signals   : {db.signal_count()}")

    if all_signals:
        print("Mode: --all-signals (skipping CSV)")
        msg_fields = _resolve_all_signals(db)
    else:
        assert csv_path is not None, "--csv is required without --all-signals"
        print(f"Parsing CSV: {csv_path}")
        csv_rows = _load_csv(csv_path)
        print(f"  rows      : {len(csv_rows)}")
        print("Resolving signals …")
        msg_fields = _resolve_signals(db, csv_rows)

    if not msg_fields:
        sys.exit("ERROR: No signals resolved. Check CSV signal_name / message_name values.")

    print(f"  Messages with signals: {len(msg_fields)}")

    tmpl_dir = os.path.join(_THIS_DIR, 'templates')
    env = Environment(
        loader=FileSystemLoader(tmpl_dir),
        keep_trailing_newline=True,
        trim_blocks=False,
    )

    dbc_basename = os.path.basename(dbc_path)
    if plugin_include_dir is None:
        # Legacy default: resolve relative to gateway/pero_cmp_lnx/tools/.
        # Callers that pip-installed artheia must pass --include explicitly.
        plugin_include = os.path.abspath(
            os.path.join(_THIS_DIR, '..', 'lib', 'include')
        )
    else:
        plugin_include = os.path.abspath(plugin_include_dir)

    msg_names: List[str] = []
    dispatch_entries = []

    for msg_name, fields in msg_fields.items():
        msg = db.lookup_by_name(msg_name)
        assert msg is not None

        msg_names.append(msg_name)
        dispatch_entries.append({
            'can_id':        msg.can_id,
            'is_extended':   msg.is_extended,
            'message_name':  msg_name,
            'dlc':           msg.dlc,
            'proto_buf_max': _proto_buf_max(fields),
        })

        tmpl_data = dict(
            namespace=namespace,
            dbc_source=dbc_basename,
            msg=msg,
            fields=[(f.field_number, f.signal, f.proto_type) for f in fields],
        )
        _render(env, 'can_proto.j2',
                os.path.join(_proto_dir, f'{msg_name}.proto'),       tmpl_data)
        _render(env, 'can_encode.c.j2',
                os.path.join(out_dir, f'can_encode_{msg_name}.c'), tmpl_data)
        _render(env, 'can_decode.c.j2',
                os.path.join(out_dir, f'can_decode_{msg_name}.c'), tmpl_data)

    shared = dict(
        namespace=namespace,
        msg_names=msg_names,
        entries=dispatch_entries,
        plugin_include_dir=plugin_include,
        dbc_source=dbc_basename,
    )

    _render(env, 'can_dispatch_table.h.j2',
            os.path.join(out_dir, 'can_dispatch_table.h'), shared)
    _render(env, 'can_dispatch_table.c.j2',
            os.path.join(out_dir, 'can_dispatch_table.c'), shared)
    _render(env, 'can_ns_wrapper.h.j2',
            os.path.join(out_dir, 'ns_wrapper.h'),         shared)
    _render(env, 'can_CMakeLists.j2',
            os.path.join(out_dir, 'CMakeLists.txt'),        shared)

    # psp_manifest.json
    _write_manifest(out_dir, namespace, dbc_path, dispatch_entries)

    print(f"\n--- Generation summary ---")
    print(f"  Output dir   : {out_dir}")
    print(f"  Namespace    : {namespace}")
    print(f"  Messages     : {len(msg_names)}")
    print(f"  Dispatch entries : {len(dispatch_entries)}")
    print("Done.")


def _write_manifest(out_dir: str, namespace: str, dbc_path: str,
                    dispatch_entries: list) -> None:
    messages_list = []
    for e in dispatch_entries:
        messages_list.append({
            "message_name": e['message_name'],
            "can_id":       e['can_id'],
            "dlc":          e['dlc'],
            "proto_type":   f"{namespace}.{e['message_name']}",
            "encode_fn":    f"cmp_can_encode_{namespace}_{e['message_name']}",
        })
    manifest = {
        "generator":  "can_to_nanopb",
        "namespace":  namespace,
        "source_dbc": dbc_path,
        "messages":   messages_list,
    }
    path = os.path.join(out_dir, "psp_manifest.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
    print(f"  wrote: {path}")


def _render(env, template_name: str, out_path: str, data: dict) -> None:
    tmpl = env.get_template(template_name)
    text = tmpl.render(**data)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"  wrote: {out_path}")


