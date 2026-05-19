"""Tests for the LSP's internal logic (parsing, position math, catalog scan).

We don't drive pygls itself end-to-end — that's third-party glue. We test the
pieces our `server.py` adds on top, which is where bugs in this codebase live.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from artheia.lsp.server import (
    _find_definition,
    _identifier_at,
    _offset_to_position,
    _parse,
    _range_from_obj,
    _range_for_textx_error,
    _scan_workspace_catalogs,
)


def test_offset_to_position_basic():
    text = "line0\nline1\nline2"
    p = _offset_to_position(text, 0)
    assert (p.line, p.character) == (0, 0)
    p = _offset_to_position(text, len("line0\n"))
    assert (p.line, p.character) == (1, 0)
    p = _offset_to_position(text, len("line0\nline1\n") + 3)
    assert (p.line, p.character) == (2, 3)


def test_identifier_at_simple():
    src = "node atomic SpeedPub { tipc type=0x1 instance=0 }"
    # cursor in the middle of "SpeedPub"
    name, _, _ = _identifier_at(src, src.index("SpeedPub") + 3)
    assert name == "SpeedPub"
    # cursor at end-of-token resolves to the token (LSP convention)
    name, _, _ = _identifier_at(src, src.index("node") + 4)
    assert name == "node"
    # cursor strictly inside whitespace (no adjacent word char) -> None
    multi_ws = "  hello  "
    name, _, _ = _identifier_at(multi_ws, 1)  # between the two leading spaces
    assert name is None


def test_parse_clean_and_diagnostic_paths():
    src = "package p\nnode atomic N { tipc type=0x1 instance=0 }\n"
    model, err = _parse(src)
    assert err is None
    assert model is not None
    assert _find_definition(model, "N") is not None
    assert _find_definition(model, "missing") is None


def test_parse_surfaces_textx_error_with_position():
    src = "package p\nnode atomic N { tipc type=0x1 instance=0\n"  # missing }
    model, err = _parse(src)
    assert model is None
    assert err is not None
    r = _range_for_textx_error(src, err)
    assert r.start.line >= 0
    assert r.start.character >= 0


def test_range_from_obj_against_real_model():
    src = "package p\nnode atomic SpeedPub { tipc type=0x1 instance=0 }\n"
    model, _ = _parse(src)
    node = model.elements[0]
    r = _range_from_obj(src, node)
    # "node" keyword starts at line 1 (0-indexed) col 0
    assert r.start.line == 1
    assert r.start.character == 0


def test_scan_workspace_catalogs(tmp_path: Path):
    cat = {"messages": {"ACC_07": {}, "ABS_Pulse": {}}}
    (tmp_path / "gateway_catalog.json").write_text(json.dumps(cat))
    (tmp_path / "nested").mkdir()
    # gateway_catalog_*.json is the canonical pattern; nested dirs work too.
    cat2 = {"messages": {"FrontRadar": {}}}
    (tmp_path / "nested" / "gateway_catalog_radar.json").write_text(json.dumps(cat2))
    syms = _scan_workspace_catalogs(tmp_path)
    assert syms == {"ACC_07", "ABS_Pulse", "FrontRadar"}


def test_scan_catalogs_ignores_unrelated_json(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
    # Tightened glob: a generic "*catalog*.json" no longer matches.
    (tmp_path / "other_catalog.json").write_text(
        json.dumps({"messages": {"NotGateway": {}}})
    )
    assert _scan_workspace_catalogs(tmp_path) == set()


def test_server_constructs():
    """Smoke: the server module imports and constructs without binding I/O."""
    from artheia.lsp import create_server
    s = create_server()
    assert s is not None
