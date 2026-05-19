"""Tests for the bus-catalog loader (gw_bus_types.h reader)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from textx import TextXSemanticError

from artheia.model import parse_string
from artheia.model.bus_catalog import _SNAPSHOT, load_buses, parse_header


HEADER_SAMPLE = """\
/* AUTO-GENERATED */
#pragma once
typedef enum {
    GW_BUS_INVALID = 0,
    GW_BUS_CAN_DIAGCAN = 1,
    GW_BUS_CAN_DCAN = 2,
    GW_BUS_CAN_HCAN = 3,
    GW_BUS_CAN_KCAN = 6,
    GW_BUS_CAN_FUTURECAN = 9,
    GW_BUS_MLBEVO_GEN2_A = 128,
    GW_BUS_MLBEVO_GEN2_B = 129,
} GwBusId;
"""


def test_parse_header_recognizes_can_and_flexray(tmp_path: Path):
    h = tmp_path / "gw_bus_types.h"
    h.write_text(HEADER_SAMPLE)
    buses = parse_header(h)
    # CAN buses dropped the GW_BUS_ + CAN_ prefix and are lowercased.
    assert buses["kcan"] == "can"
    assert buses["diagcan"] == "can"
    assert buses["futurecan"] == "can"
    # FlexRay buses keep the channel suffix.
    assert buses["mlbevo_gen2_a"] == "flexray"
    assert buses["mlbevo_gen2_b"] == "flexray"
    # GW_BUS_INVALID is dropped.
    assert "invalid" not in buses


def test_load_buses_with_env_override_picks_up_new_bus(tmp_path: Path, monkeypatch):
    h = tmp_path / "gw_bus_types.h"
    h.write_text(HEADER_SAMPLE)
    monkeypatch.setenv("ARTHEIA_GW_BUS_TYPES_H", str(h))
    buses = load_buses()
    # The sample header has a bus the snapshot does not know about.
    assert buses.get("futurecan") == "can"


def test_load_buses_falls_back_to_snapshot_when_no_header(monkeypatch):
    monkeypatch.setenv("ARTHEIA_GW_BUS_TYPES_H", "/nonexistent/path")
    # Drop HOME so the home-relative candidates also miss.
    monkeypatch.setenv("HOME", "/nonexistent")
    buses = load_buses()
    # Should contain every snapshot entry.
    for k, v in _SNAPSHOT.items():
        assert buses[k] == v


def test_validator_still_accepts_snapshot_buses_without_header(monkeypatch):
    """Smoke: kcan must keep working even if the header isn't present."""
    monkeypatch.setenv("ARTHEIA_GW_BUS_TYPES_H", "/nonexistent/path")
    monkeypatch.setenv("HOME", "/nonexistent")
    # The validator imported its dict at module load time, so we have to
    # exercise the loader directly here. The integration test (parse a real
    # .art with bus=kcan) is covered elsewhere.
    buses = load_buses()
    assert "kcan" in buses
