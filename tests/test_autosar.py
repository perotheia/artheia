"""DBC + FIBEX importer regression tests.

The DBC and FIBEX parsers are vendored from theia. We don't re-test them
here — only the Artheia-side translation: opaque-message .art emission,
catalog shape, and round-trip through `parse_file`.
"""
from __future__ import annotations

import json
from pathlib import Path

from artheia.importers import import_dbc, import_fibex
from artheia.model import parse_file


# Minimal hand-written DBC fixture. Two CAN messages with a couple of
# signals each — enough to verify frame names, ids, dlcs, and that
# signal rows land in catalog.json.
_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: ECU1 ECU2

BO_ 100 SpeedFrame: 8 ECU1
 SG_ speed_kph : 0|16@1+ (1,0) [0|400] "kph" ECU2
 SG_ direction : 16|1@1+ (1,0) [0|1] "" ECU2

BO_ 256 BrakeFrame: 4 ECU1
 SG_ brake_pressure : 0|16@1+ (0.1,0) [0|6553.5] "bar" ECU2
"""


def test_import_dbc_minimal(tmp_path):
    dbc_path = tmp_path / "fixture.dbc"
    dbc_path.write_text(_DBC)
    res = import_dbc(dbc_path, "kcan", tmp_path / "out")

    # Two CAN frames defined.
    assert res.frame_count == 2

    # The .art file parses through Artheia and contains the two messages.
    model = parse_file(str(res.art))
    msg_names = sorted(
        e.name for e in model.elements if e.__class__.__name__ == "MessageDecl"
    )
    assert msg_names == ["BrakeFrame", "SpeedFrame"]
    # Each frame now carries one MessageField per signal so callers can
    # reference signals by name. Bit layout still lives in catalog.json.
    msgs = {
        el.name: el
        for el in model.elements
        if el.__class__.__name__ == "MessageDecl"
    }
    assert len(msgs["BrakeFrame"].fields) >= 1
    assert len(msgs["SpeedFrame"].fields) >= 1
    assert model.name == "vendor.autosar.kcan"

    # The catalog carries bus metadata + per-signal layout.
    catalog = json.loads(res.catalog.read_text())
    assert catalog["bus"] == "kcan"
    assert catalog["bus_kind"] == "can"
    speed = catalog["messages"]["SpeedFrame"]
    assert speed["can_id"] == 100
    assert speed["dlc"] == 8
    sig_names = sorted(f["name"] for f in speed["fields"])
    assert sig_names == ["direction", "speed_kph"]
    brake = catalog["messages"]["BrakeFrame"]
    assert brake["can_id"] == 256
    assert brake["dlc"] == 4


def test_import_dbc_with_csv_filter(tmp_path):
    """When a CSV is supplied, only listed frames are emitted."""
    dbc_path = tmp_path / "fixture.dbc"
    dbc_path.write_text(_DBC)
    csv_path = tmp_path / "filter.csv"
    csv_path.write_text("signal_name,message_name\nspeed_kph,SpeedFrame\n")

    res = import_dbc(dbc_path, "kcan", tmp_path / "out", signal_csv=csv_path)

    assert res.frame_count == 1
    model = parse_file(str(res.art))
    msg_names = [
        e.name for e in model.elements if e.__class__.__name__ == "MessageDecl"
    ]
    assert msg_names == ["SpeedFrame"]


# Minimal FIBEX 3.1 fixture. One channel, one frame with one PDU and one
# signal, triggered on slot 5 cycle 0 with repetition 1. Real FIBEX is
# much larger but our parser only needs these elements to populate a
# FrameTrigger → FrameInfo → PduInstance → SignalInstance chain.
_FIBEX = """<?xml version="1.0" encoding="UTF-8"?>
<fx:FIBEX xmlns:fx="http://www.asam.net/xml/fbx"
          xmlns:ho="http://www.asam.net/xml/fbx/ho"
          xmlns:flexray="http://www.asam.net/xml/fbx/flexray"
          VERSION="3.1.0">
  <fx:ELEMENTS>
    <fx:CLUSTERS>
      <fx:CLUSTER ID="cluster_0">
        <ho:SHORT-NAME>test_cluster</ho:SHORT-NAME>
        <fx:CHANNEL-REFS>
          <fx:CHANNEL-REF ID-REF="ch_a"/>
        </fx:CHANNEL-REFS>
      </fx:CLUSTER>
    </fx:CLUSTERS>
    <fx:CHANNELS>
      <fx:CHANNEL ID="ch_a">
        <ho:SHORT-NAME>A</ho:SHORT-NAME>
        <fx:FRAME-TRIGGERINGS>
          <fx:FRAME-TRIGGERING ID="trig_0">
            <fx:TIMINGS>
              <fx:ABSOLUTELY-SCHEDULED-TIMING>
                <fx:SLOT-ID>5</fx:SLOT-ID>
                <fx:BASE-CYCLE>0</fx:BASE-CYCLE>
                <fx:CYCLE-REPETITION>1</fx:CYCLE-REPETITION>
              </fx:ABSOLUTELY-SCHEDULED-TIMING>
            </fx:TIMINGS>
            <fx:FRAME-REF ID-REF="frame_0"/>
          </fx:FRAME-TRIGGERING>
        </fx:FRAME-TRIGGERINGS>
      </fx:CHANNEL>
    </fx:CHANNELS>
    <fx:FRAMES>
      <fx:FRAME ID="frame_0">
        <ho:SHORT-NAME>SpeedFrame_A</ho:SHORT-NAME>
        <fx:BYTE-LENGTH>4</fx:BYTE-LENGTH>
        <fx:PDU-INSTANCES>
          <fx:PDU-INSTANCE ID="pi_0">
            <fx:PDU-REF ID-REF="pdu_0"/>
            <fx:BIT-POSITION>0</fx:BIT-POSITION>
            <fx:IS-HIGH-LOW-BYTE-ORDER>false</fx:IS-HIGH-LOW-BYTE-ORDER>
          </fx:PDU-INSTANCE>
        </fx:PDU-INSTANCES>
      </fx:FRAME>
    </fx:FRAMES>
    <fx:PDUS>
      <fx:PDU ID="pdu_0">
        <ho:SHORT-NAME>SpeedPDU</ho:SHORT-NAME>
        <fx:BYTE-LENGTH>4</fx:BYTE-LENGTH>
        <fx:PDU-TYPE>APPLICATION</fx:PDU-TYPE>
        <fx:SIGNAL-INSTANCES>
          <fx:SIGNAL-INSTANCE ID="si_0">
            <fx:SIGNAL-REF ID-REF="sig_0"/>
            <fx:BIT-POSITION>0</fx:BIT-POSITION>
            <fx:IS-HIGH-LOW-BYTE-ORDER>false</fx:IS-HIGH-LOW-BYTE-ORDER>
          </fx:SIGNAL-INSTANCE>
        </fx:SIGNAL-INSTANCES>
      </fx:PDU>
    </fx:PDUS>
    <fx:SIGNALS>
      <fx:SIGNAL ID="sig_0">
        <ho:SHORT-NAME>speed_kph</ho:SHORT-NAME>
        <fx:CODING-REF ID-REF="coding_0"/>
      </fx:SIGNAL>
    </fx:SIGNALS>
  </fx:ELEMENTS>
  <fx:PROCESSING-INFORMATION>
    <ho:CODINGS>
      <ho:CODING ID="coding_0">
        <ho:SHORT-NAME>uint16_le</ho:SHORT-NAME>
        <ho:CODED-TYPE
            ho:BASE-DATA-TYPE="A_UINT32"
            ho:CATEGORY="STANDARD-LENGTH-TYPE"
            ho:ENCODING="UNSIGNED">
          <ho:BIT-LENGTH>16</ho:BIT-LENGTH>
        </ho:CODED-TYPE>
      </ho:CODING>
    </ho:CODINGS>
  </fx:PROCESSING-INFORMATION>
</fx:FIBEX>
"""


def test_import_fibex_minimal(tmp_path):
    fx_path = tmp_path / "fixture.xml"
    fx_path.write_text(_FIBEX)
    res = import_fibex(fx_path, "test_cluster", tmp_path / "out")

    assert res.frame_count == 1
    model = parse_file(str(res.art))
    msg_names = [
        e.name for e in model.elements if e.__class__.__name__ == "MessageDecl"
    ]
    assert msg_names == ["SpeedFrame_A"]
    assert model.name == "vendor.autosar.test_cluster"

    catalog = json.loads(res.catalog.read_text())
    assert catalog["bus"] == "test_cluster"
    assert catalog["bus_kind"] == "flexray"
    entry = catalog["messages"]["SpeedFrame_A"]
    assert entry["slot_id"] == 5
    assert entry["cycle"] == 0
    assert entry["cycle_repetition"] == 1
    # Channel "name" comes from the FIBEX element ID, not its SHORT-NAME.
    # Real cluster files name channels via ID; this fixture uses `ch_a`.
    assert entry["channel"] == "ch_a"
    assert entry["byte_length"] == 4
    # One signal at bit 0, 16 bits, uint32.
    assert len(entry["fields"]) == 1
    field = entry["fields"][0]
    assert field["name"] == "speed_kph"
    assert field["bit_length"] == 16
    assert field["proto_type"] == "uint32"
