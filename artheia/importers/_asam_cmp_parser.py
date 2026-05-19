"""
asam_cmp_parser.py

Vendored verbatim from theia (gateway/pero_cmp_lnx/tools/asam_cmp_parser.py
at commit 8835770). Refresh by re-copying when theia's parser changes — this
file should never be edited locally; if you need to tweak the parser, fix it
upstream in theia and re-vendor.

THE unified signal parser for the ASAM-CMP platform.
Parses ALL signal sources used on the platform and exposes a single
PlatformDb that covers every bus:

    db = PlatformDb()
    db.load_fibex("configs/MLBevo_Gen2_...xml")
    db.load_dbc("configs/dbc/MLBevo_..._KCAN_...dbc", bus="kcan")
    db.load_dbc("configs/dbc/MLBevo_..._HCAN_...dbc", bus="hcan")

    # FlexRay lookup
    frame = db.flexray.lookup(channel_idx=0, slot_id=4, cycle=0)

    # CAN lookup by bus + CAN-ID
    msg = db.can("kcan").lookup_by_id(0x12E)

Replaces the old fibex_parser.py + dbc_parser.py split.
Those files are kept as one-line backward-compat shims.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ===========================================================================
# Shared proto-type helper (used by both FIBEX and DBC side)
# ===========================================================================

def _proto_type_for(bit_length: int, is_signed: bool,
                    scale: float, offset: float,
                    has_values: bool = False) -> str:
    """Map signal encoding to a proto3 scalar type."""
    if scale != 1.0 or offset != 0.0:
        return 'float'
    if has_values:
        return 'uint32'          # TEXTTABLE / VAL_ — keep raw integer
    if is_signed:
        return 'int32' if bit_length <= 32 else 'int64'
    if bit_length == 1:
        return 'bool'
    return 'uint32' if bit_length <= 32 else 'uint64'


# ===========================================================================
# FIBEX / FlexRay data classes
# ===========================================================================

@dataclass
class CodingInfo:
    id: str
    name: str
    bit_length: int
    encoding: str           # UNSIGNED | SIGNED | FLOAT
    method: str             # LINEAR | TEXTTABLE | IDENTICAL
    scale: float = 1.0
    offset: float = 0.0
    text_table: List[Tuple[float, str]] = field(default_factory=list)


@dataclass
class SignalInfo:
    id: str
    name: str
    coding_ref: str
    coding: Optional[CodingInfo] = None


@dataclass
class SignalInstance:
    instance_id: str
    bit_position: int
    motorola_byte_order: bool
    signal_ref: str
    signal: Optional[SignalInfo] = None


@dataclass
class PduInfo:
    id: str
    name: str
    byte_length: int
    pdu_type: str
    signal_instances: List[SignalInstance] = field(default_factory=list)


@dataclass
class PduInstance:
    instance_id: str
    bit_position: int
    motorola_byte_order: bool
    pdu_ref: str
    pdu: Optional[PduInfo] = None


@dataclass
class FrameInfo:
    id: str
    name: str
    byte_length: int
    pdu_instances: List[PduInstance] = field(default_factory=list)


@dataclass
class FrameTrigger:
    channel_idx: int
    slot_id: int
    base_cycle: int
    cycle_repetition: int
    frame_ref: str
    frame: Optional[FrameInfo] = None


# ===========================================================================
# FibexDb  (FlexRay / FIBEX 3.1)
# ===========================================================================

class FibexDb:
    """Parse a FIBEX XML and provide (channel, slot, cycle) → FrameInfo lookup."""

    def __init__(self) -> None:
        self.frames:         Dict[str, FrameInfo]   = {}
        self.pdus:           Dict[str, PduInfo]     = {}
        self.signals:        Dict[str, SignalInfo]  = {}
        self.codings:        Dict[str, CodingInfo]  = {}
        self.frame_triggers: List[FrameTrigger]     = []
        self._channel_map:   Dict[str, int]         = {}
        self._cache:         Dict[Tuple[int,int,int], FrameInfo] = {}
        self.fibex_version:  str = ""

    def load(self, xml_path: str) -> None:
        self._parse(xml_path)
        self._resolve_refs()
        self._build_cache()

    def lookup(self, channel_idx: int, slot_id: int, cycle: int) -> Optional[FrameInfo]:
        key = (channel_idx, slot_id, cycle)
        if key in self._cache:
            return self._cache[key]
        for ft in self.frame_triggers:
            if ft.channel_idx != channel_idx or ft.slot_id != slot_id:
                continue
            rep = max(ft.cycle_repetition, 1)
            if cycle >= ft.base_cycle and (cycle - ft.base_cycle) % rep == 0:
                if ft.frame:
                    self._cache[key] = ft.frame
                    return ft.frame
        return None

    # ── internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _ln(tag: str) -> str:
        if '}' in tag:  return tag.split('}', 1)[1]
        if ':' in tag:  return tag.split(':', 1)[1]
        return tag

    @staticmethod
    def _attr(elem: ET.Element, *names: str) -> str:
        for n in names:
            v = elem.get(n, '')
            if v: return v.strip()
        return ''

    @staticmethod
    def _txt(elem: ET.Element) -> str:
        return (elem.text or '').strip()

    def _child_text(self, elem: ET.Element, *tags: str) -> str:
        for child in elem:
            if self._ln(child.tag) in tags:
                return self._txt(child)
        return ''

    def _child(self, elem: ET.Element, *tags: str) -> Optional[ET.Element]:
        for child in elem:
            if self._ln(child.tag) in tags:
                return child
        return None

    # ── single-pass iterparse ────────────────────────────────────────────────

    def _parse(self, xml_path: str) -> None:
        _handlers = {
            'CLUSTER':  self._handle_cluster,
            'CHANNEL':  self._handle_channel,
            'FRAME':    self._handle_frame,
            'PDU':      self._handle_pdu,
            'SIGNAL':   self._handle_signal,
            'CODING':   self._handle_coding,
        }
        context = ET.iterparse(xml_path, events=('start', 'end'))
        _, root = next(context)
        self.fibex_version = root.get('VERSION', root.get('version', ''))
        depth = 0; accum_tag = ''; accum_depth = 0; accum_elem = None
        for event, elem in context:
            ln = self._ln(elem.tag)
            if event == 'start':
                depth += 1
                if not accum_tag and ln in _handlers:
                    accum_tag = ln; accum_depth = depth; accum_elem = elem
            else:
                if accum_tag and ln == accum_tag and depth == accum_depth:
                    _handlers[accum_tag](accum_elem)
                    accum_tag = ''; accum_depth = 0; accum_elem = None
                    elem.clear()
                depth -= 1

    def _handle_cluster(self, elem: ET.Element) -> None:
        refs = self._child(elem, 'CHANNEL-REFS')
        if refs is None:
            for child in elem.iter():
                if self._ln(child.tag) == 'CHANNEL-REFS':
                    refs = child; break
        if refs is None: return
        idx = 0
        for child in refs:
            if self._ln(child.tag) == 'CHANNEL-REF':
                ref = self._attr(child, 'ID-REF') or self._txt(child)
                if ref and ref not in self._channel_map:
                    self._channel_map[ref] = idx; idx += 1

    def _handle_channel(self, elem: ET.Element) -> None:
        chan_id  = self._attr(elem, 'ID', 'id')
        chan_idx = self._channel_map.get(chan_id, len(self._channel_map))
        if chan_id and chan_id not in self._channel_map:
            self._channel_map[chan_id] = chan_idx
        for child in elem.iter():
            if self._ln(child.tag) != 'FRAME-TRIGGERING': continue
            slot_id = base_cycle = 0; cycle_rep = 1; frame_ref = ''
            for sub in child.iter():
                sl = self._ln(sub.tag)
                if sl == 'SLOT-ID':
                    try: slot_id = int(self._txt(sub))
                    except ValueError: pass
                elif sl == 'BASE-CYCLE':
                    try: base_cycle = int(self._txt(sub))
                    except ValueError: pass
                elif sl == 'CYCLE-REPETITION':
                    try: cycle_rep = int(self._txt(sub))
                    except ValueError: pass
                elif sl == 'FRAME-REF':
                    frame_ref = self._attr(sub, 'ID-REF') or self._txt(sub)
            if slot_id > 0 and frame_ref:
                self.frame_triggers.append(FrameTrigger(
                    channel_idx=chan_idx, slot_id=slot_id,
                    base_cycle=base_cycle, cycle_repetition=max(cycle_rep, 1),
                    frame_ref=frame_ref))

    def _handle_frame(self, elem: ET.Element) -> None:
        frame_id = self._attr(elem, 'ID', 'id')
        name     = self._child_text(elem, 'SHORT-NAME')
        bl = 0
        for child in elem:
            if self._ln(child.tag) == 'BYTE-LENGTH':
                try: bl = int(self._txt(child))
                except ValueError: pass
        fi = FrameInfo(id=frame_id, name=name, byte_length=bl)
        for child in elem.iter():
            if self._ln(child.tag) != 'PDU-INSTANCE': continue
            inst_id = self._attr(child, 'ID', 'id')
            bit_pos = 0; motorola = False; pdu_ref = ''
            for sub in child:
                sl = self._ln(sub.tag)
                if sl == 'BIT-POSITION':
                    try: bit_pos = int(self._txt(sub))
                    except ValueError: pass
                elif sl == 'IS-HIGH-LOW-BYTE-ORDER':
                    motorola = self._txt(sub).lower() == 'true'
                elif sl == 'PDU-REF':
                    pdu_ref = self._attr(sub, 'ID-REF') or self._txt(sub)
            if pdu_ref:
                fi.pdu_instances.append(PduInstance(
                    instance_id=inst_id, bit_position=bit_pos,
                    motorola_byte_order=motorola, pdu_ref=pdu_ref))
        if frame_id: self.frames[frame_id] = fi

    def _handle_pdu(self, elem: ET.Element) -> None:
        pdu_id   = self._attr(elem, 'ID', 'id')
        name     = self._child_text(elem, 'SHORT-NAME')
        pdu_type = self._child_text(elem, 'PDU-TYPE')
        bl = 0
        for child in elem:
            if self._ln(child.tag) == 'BYTE-LENGTH':
                try: bl = int(self._txt(child))
                except ValueError: pass
        pi = PduInfo(id=pdu_id, name=name, byte_length=bl, pdu_type=pdu_type)
        for child in elem.iter():
            if self._ln(child.tag) != 'SIGNAL-INSTANCE': continue
            inst_id = self._attr(child, 'ID', 'id')
            bit_pos = 0; motorola = False; sig_ref = ''
            for sub in child:
                sl = self._ln(sub.tag)
                if sl == 'BIT-POSITION':
                    try: bit_pos = int(self._txt(sub))
                    except ValueError: pass
                elif sl == 'IS-HIGH-LOW-BYTE-ORDER':
                    motorola = self._txt(sub).lower() == 'true'
                elif sl == 'SIGNAL-REF':
                    sig_ref = self._attr(sub, 'ID-REF') or self._txt(sub)
            if sig_ref:
                pi.signal_instances.append(SignalInstance(
                    instance_id=inst_id, bit_position=bit_pos,
                    motorola_byte_order=motorola, signal_ref=sig_ref))
        if pdu_id: self.pdus[pdu_id] = pi

    def _handle_signal(self, elem: ET.Element) -> None:
        sig_id = self._attr(elem, 'ID', 'id')
        name   = self._child_text(elem, 'SHORT-NAME')
        coding_ref = ''
        for child in elem:
            if self._ln(child.tag) == 'CODING-REF':
                coding_ref = self._attr(child, 'ID-REF') or self._txt(child)
        if sig_id:
            self.signals[sig_id] = SignalInfo(id=sig_id, name=name, coding_ref=coding_ref)

    def _handle_coding(self, elem: ET.Element) -> None:
        coding_id = self._attr(elem, 'ID', 'id')
        name      = self._child_text(elem, 'SHORT-NAME')
        bl = 0; encoding = 'UNSIGNED'; method = 'IDENTICAL'
        scale = 1.0; offset = 0.0
        text_table: List[Tuple[float, str]] = []
        for child in elem:
            ln = self._ln(child.tag)
            if ln == 'CODED-TYPE':
                enc = child.get('ENCODING', '').upper()
                if 'FLOAT' in enc:         encoding = 'FLOAT'
                elif 'SIGNED' in enc and 'UNSIGNED' not in enc: encoding = 'SIGNED'
                else:                      encoding = 'UNSIGNED'
                for sub in child:
                    if self._ln(sub.tag) == 'BIT-LENGTH':
                        try: bl = int(self._txt(sub))
                        except ValueError: pass
            elif ln == 'COMPU-METHODS':
                for cm in child:
                    if self._ln(cm.tag) != 'COMPU-METHOD': continue
                    for sub in cm:
                        if self._ln(sub.tag) == 'CATEGORY':
                            cat = self._txt(sub).upper()
                            if cat in ('LINEAR', 'RAT-FUNC'):       method = 'LINEAR'
                            elif cat in ('TEXTTABLE', 'TAB-NOINTP'): method = 'TEXTTABLE'
                    citp = self._child(cm, 'COMPU-INTERNAL-TO-PHYS')
                    if citp is None: continue
                    scales_elem = self._child(citp, 'COMPU-SCALES')
                    if scales_elem is None: continue
                    for scale_elem in scales_elem:
                        if self._ln(scale_elem.tag) != 'COMPU-SCALE': continue
                        lower: Optional[float] = None; vt = ''
                        for sv in scale_elem:
                            svl = self._ln(sv.tag)
                            if svl == 'LOWER-LIMIT':
                                try: lower = float(self._txt(sv))
                                except ValueError: pass
                            elif svl == 'COMPU-CONST':
                                for vt_e in sv:
                                    if self._ln(vt_e.tag) == 'VT':
                                        vt = self._txt(vt_e)
                            elif svl == 'COMPU-RATIONAL-COEFFS':
                                num = self._child(sv, 'COMPU-NUMERATOR')
                                if num:
                                    coeffs = []
                                    for v in num:
                                        if self._ln(v.tag) == 'V':
                                            try: coeffs.append(float(self._txt(v)))
                                            except ValueError: pass
                                    if len(coeffs) >= 2: offset = coeffs[0]; scale = coeffs[1]
                                    elif len(coeffs) == 1: offset = coeffs[0]
                        if lower is not None and vt:
                            text_table.append((lower, vt))
        if coding_id:
            self.codings[coding_id] = CodingInfo(
                id=coding_id, name=name, bit_length=bl, encoding=encoding,
                method=method, scale=scale, offset=offset, text_table=text_table)

    def _resolve_refs(self) -> None:
        for sig in self.signals.values():
            sig.coding = self.codings.get(sig.coding_ref)
        for pdu in self.pdus.values():
            for si in pdu.signal_instances:
                si.signal = self.signals.get(si.signal_ref)
        for frame in self.frames.values():
            for pi in frame.pdu_instances:
                pi.pdu = self.pdus.get(pi.pdu_ref)
        for ft in self.frame_triggers:
            ft.frame = self.frames.get(ft.frame_ref)

    def _build_cache(self) -> None:
        for ft in self.frame_triggers:
            if ft.frame is None: continue
            key = (ft.channel_idx, ft.slot_id, 0)
            if key not in self._cache:
                self._cache[key] = ft.frame


# ===========================================================================
# DBC / CAN data classes
# ===========================================================================

@dataclass
class DbcSignalValue:
    raw: float
    label: str


@dataclass
class DbcSignal:
    name: str
    start_bit: int
    bit_length: int
    byte_order: int     # 0=Motorola MSB  1=Intel LSB
    value_type: str     # '+' unsigned  '-' signed
    factor: float
    offset: float
    min_val: float
    max_val: float
    unit: str
    values: List[DbcSignalValue] = field(default_factory=list)

    @property
    def motorola_byte_order(self) -> bool:
        return self.byte_order == 0

    @property
    def is_signed(self) -> bool:
        return self.value_type == '-'

    @property
    def proto_type(self) -> str:
        return _proto_type_for(
            self.bit_length, self.is_signed,
            self.factor, self.offset,
            has_values=bool(self.values))


@dataclass
class DbcMessage:
    raw_id: int
    name: str
    dlc: int
    sender: str
    signals: Dict[str, DbcSignal] = field(default_factory=dict)

    @property
    def can_id(self) -> int:
        return self.raw_id & 0x1FFFFFFF

    @property
    def is_extended(self) -> bool:
        return bool(self.raw_id & 0x80000000)


# ===========================================================================
# DbcDb  (CAN / DBC)
# ===========================================================================

_MSG_RE  = re.compile(r'^BO_ (\d+) (\w+)\s*:\s*(\d+)\s+(\S+)')
_SIG_RE  = re.compile(
    r'^\s+SG_ (\w+)\s*(?:M|m\d+|m\d+M)?\s*:\s*'
    r'(\d+)\|(\d+)@([01])([+-])\s*'
    r'\(([^,]+),([^)]+)\)\s*\[([^|]+)\|([^\]]+)\]\s*"([^"]*)"\s*(.*)')
_VAL_RE  = re.compile(r'^VAL_\s+(\d+)\s+(\w+)\s+(.*?)\s*;')
_VNUM_RE = re.compile(r'(-?\d+(?:\.\d+)?)\s+"([^"]*)"')


class DbcDb:
    """Parse a DBC file and provide CAN-ID / message-name lookup."""

    def __init__(self) -> None:
        self.messages:    Dict[str, DbcMessage]  = {}   # by name
        self._by_id:      Dict[int,  DbcMessage]  = {}   # by can_id
        self.source_file: str = ''
        self.bus_name:    str = ''                       # e.g. 'kcan'

    def load(self, dbc_path: str, bus_name: str = '') -> None:
        self.source_file = dbc_path
        self.bus_name    = bus_name
        cur_msg: Optional[DbcMessage] = None
        with open(dbc_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n')
                m = _MSG_RE.match(line)
                if m:
                    raw_id  = int(m.group(1))
                    cur_msg = DbcMessage(raw_id=raw_id, name=m.group(2),
                                         dlc=int(m.group(3)), sender=m.group(4))
                    self.messages[cur_msg.name]    = cur_msg
                    self._by_id[cur_msg.can_id]    = cur_msg
                    continue
                s = _SIG_RE.match(line)
                if s and cur_msg is not None:
                    try:
                        sig = DbcSignal(
                            name=s.group(1), start_bit=int(s.group(2)),
                            bit_length=int(s.group(3)), byte_order=int(s.group(4)),
                            value_type=s.group(5), factor=float(s.group(6)),
                            offset=float(s.group(7)), min_val=float(s.group(8)),
                            max_val=float(s.group(9)), unit=s.group(10))
                        cur_msg.signals[sig.name] = sig
                    except ValueError:
                        pass
                    continue
                if not line.strip():
                    cur_msg = None
                    continue
                v = _VAL_RE.match(line)
                if v:
                    raw_id = int(v.group(1))
                    msg    = self._by_id.get(raw_id)
                    if msg and v.group(2) in msg.signals:
                        sig = msg.signals[v.group(2)]
                        for nm in _VNUM_RE.finditer(v.group(3)):
                            sig.values.append(DbcSignalValue(float(nm.group(1)), nm.group(2)))

    def lookup_by_name(self, name: str) -> Optional[DbcMessage]:
        return self.messages.get(name)

    def lookup_by_id(self, can_id: int) -> Optional[DbcMessage]:
        return self._by_id.get(can_id)

    def message_count(self) -> int:  return len(self.messages)
    def signal_count(self)  -> int:  return sum(len(m.signals) for m in self.messages.values())


# ===========================================================================
# PlatformDb  — unified multi-bus database
# ===========================================================================

class PlatformDb:
    """
    Holds FlexRay (FIBEX) and any number of CAN (DBC) databases for a
    platform.  Single entry point for multi-bus signal generators.

    Example:
        db = PlatformDb()
        db.load_fibex("configs/MLBevo_...xml")
        db.load_dbc("configs/dbc/MLBevo_..._KCAN_...dbc", bus="kcan")
        db.load_dbc("configs/dbc/MLBevo_..._HCAN_...dbc", bus="hcan")

        frame = db.flexray.lookup(0, 4, 0)          # FlexRay slot 4
        msg   = db.can("kcan").lookup_by_id(0x12E)  # KCAN CAN-ID 302
    """

    def __init__(self) -> None:
        self.flexray: Optional[FibexDb]       = None
        self._can_dbs: Dict[str, DbcDb]        = {}   # bus_name → DbcDb

    def load_fibex(self, xml_path: str) -> 'PlatformDb':
        db = FibexDb()
        db.load(xml_path)
        self.flexray = db
        return self

    def load_dbc(self, dbc_path: str, bus: str) -> 'PlatformDb':
        db = DbcDb()
        db.load(dbc_path, bus_name=bus)
        self._can_dbs[bus] = db
        return self

    def can(self, bus: str) -> DbcDb:
        db = self._can_dbs.get(bus)
        if db is None:
            raise KeyError(f"CAN bus '{bus}' not loaded. "
                           f"Call load_dbc(..., bus='{bus}') first.")
        return db

    @property
    def can_buses(self) -> List[str]:
        return list(self._can_dbs.keys())

    def summary(self) -> str:
        lines = ['PlatformDb:']
        if self.flexray:
            lines.append(
                f'  FlexRay (FIBEX {self.flexray.fibex_version}): '
                f'{len(self.flexray.frames)} frames, '
                f'{len(self.flexray.signals)} signals')
        for bus, db in self._can_dbs.items():
            lines.append(
                f'  CAN {bus}: {db.message_count()} messages, '
                f'{db.signal_count()} signals')
        return '\n'.join(lines)
