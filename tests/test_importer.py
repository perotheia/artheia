"""ARXML importer regression test.

Pinned to the cantools `system-4.2.arxml` sample (MIT-licensed) we ship at
examples/arxml/. Verifies:
  - extraction finds the 8 CAN-FRAME-TRIGGERINGs as messages,
  - CAN IDs are read correctly,
  - signal layouts have plausible types,
  - the generated .art re-parses through artheia.model,
  - the catalog JSON has matching entries.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from artheia.importers import import_arxml_signals
from artheia.model import parse_file


REPO = Path(__file__).resolve().parents[1]
ARXML = REPO / "examples" / "arxml" / "system-4.2.arxml"


@pytest.mark.skipif(not ARXML.exists(), reason="sample ARXML not present")
def test_import_round_trip(tmp_path):
    art = tmp_path / "gateway_signals.art"
    cat = tmp_path / "gateway_catalog.json"
    import_arxml_signals(ARXML, art, cat, package="gateway.test")

    # generated .art must parse through Artheia
    model = parse_file(art)
    msg_names = [e.name for e in model.elements if e.__class__.__name__ == "MessageDecl"]
    assert "Message1" in msg_names
    assert "Message2" in msg_names
    assert len(msg_names) == 8  # 8 CAN-FRAME-TRIGGERINGs in the sample

    # catalog should match
    catalog = json.loads(cat.read_text())
    assert "Message1" in catalog["messages"]
    m1 = catalog["messages"]["Message1"]
    assert m1["bus_kind"] == "can"
    assert m1["can_id"] == 5
    assert m1["dlc"] == 9
    assert len(m1["fields"]) == 5
    field_names = {f["name"] for f in m1["fields"]}
    assert {"signal1", "signal5", "signal6"}.issubset(field_names)


FR_ARXML = REPO / "examples" / "arxml" / "synthetic_flexray.arxml"


@pytest.mark.skipif(not FR_ARXML.exists(), reason="FlexRay fixture missing")
def test_import_flexray_round_trip(tmp_path):
    art = tmp_path / "gateway_signals.art"
    cat = tmp_path / "gateway_catalog.json"
    import_arxml_signals(FR_ARXML, art, cat, package="gateway.fr")

    model = parse_file(art)
    msg_names = {e.name for e in model.elements if e.__class__.__name__ == "MessageDecl"}
    # Fixture has 3 FLEXRAY-FRAME-TRIGGERINGs (2 on A, 1 on B), each becomes
    # its own message with channel suffix.
    assert msg_names == {"SpeedFrame_A", "TorqueFrame_A", "SpeedFrame_B"}

    catalog = json.loads(cat.read_text())["messages"]

    # The channel + bus mapping must match GwBusId conventions:
    # cluster `MlbFR` + channel A -> bus `mlbfr_a`, etc.
    assert catalog["SpeedFrame_A"]["bus"] == "mlbfr_a"
    assert catalog["SpeedFrame_A"]["channel"] == "A"
    assert catalog["SpeedFrame_A"]["slot_id"] == 15

    assert catalog["SpeedFrame_B"]["bus"] == "mlbfr_b"
    assert catalog["SpeedFrame_B"]["channel"] == "B"
    assert catalog["SpeedFrame_B"]["slot_id"] == 15

    # Same frame, different channels = different bus, same slot.
    assert catalog["TorqueFrame_A"]["slot_id"] == 16
    assert catalog["TorqueFrame_A"]["bus_kind"] == "flexray"

    # Signal field extraction works on FlexRay too.
    speed_fields = {f["name"] for f in catalog["SpeedFrame_A"]["fields"]}
    assert speed_fields == {"speed_kph", "status_flag"}
