"""Fusée importer regression tests.

These exercise the proto2 → Artheia transcription paths that are
non-obvious: enum hoisting, enum value-prefix stripping, the digit-safe
fallback for stripped names that would otherwise start with a digit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from artheia.importers.fusee import (
    _enum_value_prefix,
    _strip_enum_value_prefix,
    parse_proto_file,
    emit_package_art,
)
from artheia.model import parse_string


def test_enum_value_prefix():
    assert _enum_value_prefix("VehicleStateMode") == "VEHICLE_STATE_MODE_"
    assert _enum_value_prefix("GPSFixEnum") == "GPS_FIX_ENUM_"
    assert _enum_value_prefix("DriveMode") == "DRIVE_MODE_"


def test_strip_enum_value_prefix_common_case():
    assert _strip_enum_value_prefix(
        "VEHICLE_STATE_MODE_DRIVE", "VehicleStateMode"
    ) == "DRIVE"


def test_strip_enum_value_prefix_no_match_keeps_original():
    assert _strip_enum_value_prefix("UNKNOWN", "VehicleStateMode") == "UNKNOWN"


def test_strip_enum_value_prefix_keeps_when_stripped_starts_with_digit():
    """`GPS_FIX_ENUM_2D_FIX` would strip to `2D_FIX`, which is not a valid
    identifier (can't start with a digit). The original is preserved."""
    assert _strip_enum_value_prefix(
        "GPS_FIX_ENUM_2D_FIX", "GPSFixEnum"
    ) == "GPS_FIX_ENUM_2D_FIX"


def test_strip_enum_value_prefix_keeps_when_strip_would_empty():
    assert _strip_enum_value_prefix("VEHICLE_STATE_MODE_", "VehicleStateMode") == "VEHICLE_STATE_MODE_"


def test_emit_package_with_enum_round_trips(tmp_path):
    """A minimal proto2 file with an enum + a message referencing it should
    transcribe to an Artheia .art file that parses cleanly via parse_string."""
    proto_src = tmp_path / "drive_mode.proto"
    proto_src.write_text(
        'syntax = "proto2";\n'
        'package moz.msg;\n'
        '\n'
        'enum DriveModeState {\n'
        '  DRIVE_MODE_STATE_NORMAL = 0;\n'
        '  DRIVE_MODE_STATE_SPORT = 1;\n'
        '  DRIVE_MODE_STATE_RACETRACK = 2;\n'
        '}\n'
        '\n'
        'message DriveMode {\n'
        '  required DriveModeState drive_mode = 1;\n'
        '}\n'
    )
    pf = parse_proto_file(proto_src)
    art = emit_package_art("transmission", [pf], [])
    model = parse_string(art)
    kinds = [e.__class__.__name__ for e in model.elements]
    assert "EnumDecl" in kinds
    assert "MessageDecl" in kinds
    enum_el = next(e for e in model.elements if e.__class__.__name__ == "EnumDecl")
    msg_el = next(e for e in model.elements if e.__class__.__name__ == "MessageDecl")
    assert enum_el.name == "DriveModeState"
    # Values keep their numbers; names lose the `DRIVE_MODE_STATE_` prefix.
    assert [(v.name, v.number) for v in enum_el.values] == [
        ("NORMAL", 0), ("SPORT", 1), ("RACETRACK", 2),
    ]
    # The message field's type cross-reference resolves to the enum.
    assert msg_el.fields[0].type.ref is enum_el
