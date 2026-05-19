"""ARXML → Artheia gateway-signal importer.

Reads an AUTOSAR 4.x system ARXML using `autosar-data` and emits:
  - a generated `.art` stub file with one `message` declaration per gateway
    frame/PDU,
  - a JSON catalog with routing metadata (bus, can_id|slot, dlc, etc.) keyed
    by message name for the netgraph generator and LSP completion.

Scope (Job A, see project memory): we extract ONLY what the netgraph and
completion need to be accurate — names, addresses, bus assignments,
signal field layout. We do NOT translate SWCs, runnables, datatypes, etc.

Concept mapping:
  AUTOSAR CAN-FRAME  -> Artheia `message` (one per frame)
  AUTOSAR CAN-FRAME-TRIGGERING.IDENTIFIER  -> catalog can_id
  AUTOSAR PDU-TO-FRAME-MAPPING.PDU-REF  -> link frame to PDU
  AUTOSAR I-SIGNAL-TO-I-PDU-MAPPING  -> Artheia message field
  AUTOSAR I-SIGNAL.LENGTH (bits)  -> Artheia proto3 type
       1            -> bool
       2..32        -> uint32
       33..64       -> uint64
       (signed flag detected via BASE-TYPE, fallback unsigned)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import autosar_data as ad  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "autosar-data is required for the ARXML importer. "
        "Install it with: pip install 'artheia[importers]'"
    ) from exc


# ---- intermediate model ----------------------------------------------------

@dataclass
class Field:
    name: str
    proto_type: str
    bit_position: int
    bit_length: int

    def proto_number(self, idx: int) -> int:
        return idx + 1


@dataclass
class Message:
    name: str
    bus: str
    bus_kind: str  # "can" or "flexray"
    can_id: int | None = None
    dlc: int | None = None
    slot_id: int | None = None
    channel: str | None = None        # FlexRay only: "A" | "B"
    cycle: int | None = None          # FlexRay base cycle
    pdu_offset: int | None = None     # FlexRay PDU offset (default 0)
    fields: list[Field] = field(default_factory=list)


# ---- element-tree helpers --------------------------------------------------

def _short_name(el) -> str | None:
    for child in el.sub_elements:
        if child.element_name == "SHORT-NAME":
            return child.character_data
    return None


def _child(el, name: str):
    for c in el.sub_elements:
        if c.element_name == name:
            return c
    return None


def _resolve_ref(model, ref_el):
    """Resolve any *-REF element to its target."""
    if ref_el is None:
        return None
    try:
        return ref_el.reference_target
    except Exception:
        return None


def _walk(el, name: str):
    for _, sub in el.elements_dfs:
        if sub.element_name == name:
            yield sub


# ---- bit length → proto3 type ----------------------------------------------

def _proto_type_for(bits: int, signed: bool) -> str:
    if bits == 1:
        return "bool"
    if bits <= 32:
        return "sint32" if signed else "uint32"
    if bits <= 64:
        return "sint64" if signed else "uint64"
    return "bytes"


def _signal_signed(isignal) -> bool:
    """Best-effort: chase BASE-TYPE-REF to see if it's signed."""
    base = _child(isignal, "NETWORK-REPRESENTATION-PROPS")
    if base is None:
        # try direct base type
        for path in ("BASE-TYPE-REF",):
            ref = _child(isignal, path)
            tgt = _resolve_ref(None, ref)
            if tgt is not None:
                enc = _child(tgt, "BASE-TYPE-ENCODING")
                if enc and enc.character_data and "signed" in str(enc.character_data).lower():
                    return True
        return False
    # walk into props
    for _, sub in base.elements_dfs:
        if sub.element_name == "BASE-TYPE-ENCODING":
            cd = sub.character_data
            if cd and "signed" in str(cd).lower():
                return True
    return False


# ---- main extraction -------------------------------------------------------

def _bus_name_from_cluster(cluster) -> str:
    """Lowercase the cluster SHORT-NAME for bus identifier."""
    n = _short_name(cluster) or "bus"
    return n.lower().replace("-", "_")


def _frame_to_isignal_pdus(frame):
    """Yield I-SIGNAL-I-PDUs reachable via PDU-TO-FRAME-MAPPING from frame."""
    for m in _walk(frame, "PDU-TO-FRAME-MAPPING"):
        ref = _child(m, "PDU-REF")
        tgt = _resolve_ref(None, ref)
        if tgt is None:
            continue
        if tgt.element_name == "I-SIGNAL-I-PDU":
            yield tgt
        else:
            # Container/multiplexed: walk for I-SIGNAL-I-PDU descendants.
            for child in _walk(tgt, "I-SIGNAL-I-PDU"):
                yield child


def _ipdu_fields(ipdu) -> list[Field]:
    out: list[Field] = []
    for m in _walk(ipdu, "I-SIGNAL-TO-I-PDU-MAPPING"):
        sig_ref = _child(m, "I-SIGNAL-REF")
        signal = _resolve_ref(None, sig_ref)
        if signal is None:
            continue
        name = _short_name(signal) or "signal"
        length_el = _child(signal, "LENGTH")
        bits = int(length_el.character_data) if length_el and length_el.character_data else 0
        pos_el = _child(m, "START-POSITION")
        bit_pos = int(pos_el.character_data) if pos_el and pos_el.character_data else 0
        signed = _signal_signed(signal)
        out.append(Field(
            name=name,
            proto_type=_proto_type_for(bits, signed),
            bit_position=bit_pos,
            bit_length=bits,
        ))
    # textX-friendly: stable order by bit position then name
    out.sort(key=lambda f: (f.bit_position, f.name))
    return out


def _extract_can(model, cluster) -> Iterable[Message]:
    bus = _bus_name_from_cluster(cluster)
    for ft in _walk(cluster, "CAN-FRAME-TRIGGERING"):
        # frame name and CAN id
        id_el = _child(ft, "IDENTIFIER")
        can_id = int(id_el.character_data) if id_el and id_el.character_data else None
        frame_ref = _child(ft, "FRAME-REF")
        frame = _resolve_ref(None, frame_ref)
        if frame is None or can_id is None:
            continue
        frame_name = _short_name(frame) or _short_name(ft) or f"FRAME_{can_id:x}"
        length_el = _child(frame, "FRAME-LENGTH")
        dlc = int(length_el.character_data) if length_el and length_el.character_data else None
        # collect fields from every IPDU mapped onto the frame
        fields: list[Field] = []
        for ipdu in _frame_to_isignal_pdus(frame):
            fields.extend(_ipdu_fields(ipdu))
        # dedup field names (multiple PDUs in one frame can collide; suffix)
        seen: dict[str, int] = {}
        for f in fields:
            n = seen.get(f.name, 0)
            seen[f.name] = n + 1
            if n:
                f.name = f"{f.name}_{n}"
        yield Message(
            name=_sanitize(frame_name),
            bus=bus,
            bus_kind="can",
            can_id=can_id,
            dlc=dlc,
            fields=fields,
        )


def _bus_name_from_flexray(cluster, channel_short: str) -> str:
    """A FlexRay bus identifier carries the channel (A/B) suffix.

    The Theia GwBusId enum encodes channel-A/channel-B as adjacent values
    (see gw_bus_types.h: GW_BUS_MLBEVO_GEN2_A / _B), so the DSL identifier
    needs to match that convention.
    """
    cluster_name = (_short_name(cluster) or "fr").lower().replace("-", "_")
    # CHANNEL-A -> "a", CHANNEL-B -> "b". Tolerate either case.
    suffix = "a" if "A" in channel_short.upper() else "b"
    return f"{cluster_name}_{suffix}"


def _channel_letter(channel_name_text: str | None) -> str | None:
    """Extract the channel letter from AUTOSAR enum values like 'CHANNEL-A',
    'CHANNEL-B', or short-names like 'ChannelA' / 'Channel_B'.

    The 'A' and 'B' must be at a token boundary — naive substring matching
    fails because the string 'CHANNEL-B' contains both 'A' and 'B'.
    """
    if not channel_name_text:
        return None
    s = channel_name_text.upper()
    # Strict suffix / boundary check.
    for letter in ("A", "B"):
        if s.endswith(letter) or s.endswith(f"-{letter}") or s.endswith(f"_{letter}"):
            return letter
    return None


def _extract_flexray(cluster) -> Iterable[Message]:
    for chan in _walk(cluster, "FLEXRAY-PHYSICAL-CHANNEL"):
        chan_name_el = _child(chan, "CHANNEL-NAME")
        chan_name_txt = (
            chan_name_el.character_data if chan_name_el is not None else None
        )
        # CHANNEL-NAME can come back as an enum object or a plain string.
        chan_str = str(chan_name_txt) if chan_name_txt is not None else ""
        letter = _channel_letter(chan_str)
        if letter is None:
            # Fall back to the channel SHORT-NAME (e.g. "ChannelA").
            sn = _short_name(chan) or ""
            letter = _channel_letter(sn) or "A"
        bus = _bus_name_from_flexray(cluster, letter)

        for ft in _walk(chan, "FLEXRAY-FRAME-TRIGGERING"):
            frame = _resolve_ref(None, _child(ft, "FRAME-REF"))
            if frame is None:
                continue
            frame_name = _short_name(frame) or _short_name(ft) or "FRAME"

            # Slot + base cycle from the absolute timing.
            slot_id: int | None = None
            base_cycle: int | None = None
            for timing in _walk(ft, "FLEXRAY-ABSOLUTELY-SCHEDULED-TIMING"):
                sid = _child(timing, "SLOT-ID")
                if sid is not None and sid.character_data is not None:
                    slot_id = int(sid.character_data)
                cc = _child(timing, "COMMUNICATION-CYCLE")
                if cc is not None:
                    cnt = _child(cc, "CYCLE-COUNTER")
                    if cnt is not None:
                        inner = _child(cnt, "CYCLE-COUNTER")
                        if inner is not None and inner.character_data is not None:
                            base_cycle = int(inner.character_data)
                break

            if slot_id is None:
                continue  # nothing useful to publish

            length_el = _child(frame, "FRAME-LENGTH")
            frame_length = (
                int(length_el.character_data) if length_el and length_el.character_data else None
            )

            fields: list[Field] = []
            for ipdu in _frame_to_isignal_pdus(frame):
                fields.extend(_ipdu_fields(ipdu))
            seen: dict[str, int] = {}
            for f in fields:
                n = seen.get(f.name, 0)
                seen[f.name] = n + 1
                if n:
                    f.name = f"{f.name}_{n}"

            # Channel A and B can carry the same frame at the same slot — that
            # is the normal redundancy mode. We disambiguate the *message*
            # name with a `_A` / `_B` suffix when both channels carry it; the
            # caller's de-duplication pass would otherwise collide.
            msg_name = _sanitize(f"{frame_name}_{letter}")
            yield Message(
                name=msg_name,
                bus=bus,
                bus_kind="flexray",
                slot_id=slot_id,
                channel=letter,
                cycle=base_cycle if base_cycle is not None else 0,
                pdu_offset=0,
                dlc=frame_length,
                fields=fields,
            )


_ID_RE_BAD = set("- ./\\:")


def _sanitize(s: str) -> str:
    out = []
    for ch in s:
        out.append("_" if ch in _ID_RE_BAD else ch)
    name = "".join(out)
    if name and not (name[0].isalpha() or name[0] == "_"):
        name = "_" + name
    return name or "_anon"


# ---- output emission -------------------------------------------------------

_ART_HEADER = """\
// AUTO-GENERATED by artheia import-arxml — DO NOT EDIT
// source: {source}

package {package}

"""


def _emit_art(messages: list[Message], package: str, source: str) -> str:
    out = [_ART_HEADER.format(source=source, package=package)]
    for msg in messages:
        if msg.bus_kind == "can":
            out.append(f"// bus={msg.bus} can_id=0x{msg.can_id:x} dlc={msg.dlc}\n")
        else:
            out.append(
                f"// bus={msg.bus} slot={msg.slot_id} "
                f"channel={msg.channel} cycle={msg.cycle} dlc={msg.dlc}\n"
            )
        out.append(f"message {msg.name} {{\n")
        if not msg.fields:
            out.append("    // (no I-SIGNAL mappings found in ARXML)\n")
        else:
            for i, f in enumerate(msg.fields):
                out.append(
                    f"    {f.proto_type} {_sanitize(f.name)}"
                    f"  // bit={f.bit_position} len={f.bit_length}\n"
                )
        out.append("}\n\n")
    return "".join(out)


def _emit_catalog(messages: list[Message]) -> dict:
    entries: dict[str, dict] = {}
    for m in messages:
        entry: dict = {"bus": m.bus, "bus_kind": m.bus_kind}
        if m.can_id is not None:
            entry["can_id"] = m.can_id
        if m.slot_id is not None:
            entry["slot_id"] = m.slot_id
        if m.channel is not None:
            entry["channel"] = m.channel
        if m.cycle is not None:
            entry["cycle"] = m.cycle
        if m.pdu_offset is not None:
            entry["pdu_offset"] = m.pdu_offset
        if m.dlc is not None:
            entry["dlc"] = m.dlc
        entry["fields"] = [
            {"name": f.name, "proto_type": f.proto_type,
             "bit_position": f.bit_position, "bit_length": f.bit_length}
            for f in m.fields
        ]
        entries[m.name] = entry
    return {"messages": entries}


# ---- public entry point ----------------------------------------------------

def import_arxml_signals(
    arxml_path: str | Path,
    out_art: str | Path,
    out_catalog: str | Path,
    package: str = "gateway.signals",
) -> tuple[Path, Path]:
    arxml_path = Path(arxml_path)
    out_art = Path(out_art)
    out_catalog = Path(out_catalog)

    model = ad.AutosarModel()
    arxmlfile, warnings = model.load_file(str(arxml_path), False)

    messages: list[Message] = []
    for _, el in model.root_element.elements_dfs:
        n = el.element_name
        if n == "CAN-CLUSTER":
            messages.extend(_extract_can(model, el))
        elif n == "FLEXRAY-CLUSTER":
            messages.extend(_extract_flexray(el))

    out_art.parent.mkdir(parents=True, exist_ok=True)
    out_art.write_text(_emit_art(messages, package=package, source=str(arxml_path)))

    out_catalog.parent.mkdir(parents=True, exist_ok=True)
    out_catalog.write_text(json.dumps(_emit_catalog(messages), indent=2) + "\n")

    return out_art, out_catalog
