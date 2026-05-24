"""Tests for `artheia gen-trace-decoder-subset`.

Smoke + edge cases for the netgraph-driven trace decoder generator.
The output is C++ (which we don't compile here); tests verify the
collected message-type set + the emitted .cc string for syntactic
shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from artheia.generators.trace_decoder_subset import (
    collect_message_types,
    generate,
)


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return p


def test_collect_from_cluster_walks_signals_ports_connections(tmp_path: Path):
    """All three sources contribute to the union."""
    netgraph = _write(tmp_path, "ng.json", {
        "package": "test",
        "nodes": [
            {
                "name": "A",
                "ports": [{"interface": "I", "messages": ["FromPort"]}],
                "signals": {"FromSignal": {"direction": "out",
                                            "destinations": []}},
            },
        ],
        "compositions": [
            {"name": "C", "connections": [{"messages": ["FromConn"]}]},
        ],
    })
    types = collect_message_types(
        cluster_netgraph=netgraph, psp_netgraph=None
    )
    assert types == ["FromConn", "FromPort", "FromSignal"]


def test_collect_from_psp_picks_up_msg_pdu_keys(tmp_path: Path):
    """PSP graphs vary; we extract any string under msg / pdu / signal /
    message keys."""
    psp = _write(tmp_path, "psp.json", {
        "buses": {
            "kcan": {
                "routes": [
                    {"pdu": "BrakePdu", "id": 0x100},
                    {"msg": "SpeedMsg", "id": 0x101},
                ],
            }
        }
    })
    types = collect_message_types(
        cluster_netgraph=None, psp_netgraph=psp
    )
    assert types == ["BrakePdu", "SpeedMsg"]


def test_union_dedups_across_inputs(tmp_path: Path):
    """The same message in both inputs appears once in the output."""
    cluster = _write(tmp_path, "ng.json", {
        "package": "test",
        "nodes": [
            {"name": "A", "ports": [], "signals": {
                "Shared": {"direction": "out", "destinations": []}}}
        ],
    })
    psp = _write(tmp_path, "psp.json", {
        "buses": {"k": {"routes": [{"msg": "Shared"}, {"msg": "PspOnly"}]}}
    })
    types = collect_message_types(
        cluster_netgraph=cluster, psp_netgraph=psp
    )
    assert types == ["PspOnly", "Shared"]


def test_at_least_one_input_required():
    with pytest.raises(ValueError, match="at least one"):
        collect_message_types(cluster_netgraph=None, psp_netgraph=None)


def test_generate_emits_handler_per_message(tmp_path: Path):
    """The generated .cc contains one Handler entry per message,
    properly C-quoted and registered."""
    cluster = _write(tmp_path, "ng.json", {
        "package": "test",
        "nodes": [
            {"name": "A", "ports": [], "signals": {
                "MsgA": {"direction": "out", "destinations": []},
                "MsgB": {"direction": "in", "destinations": []},
            }},
        ],
    })
    out = generate(
        cluster_netgraph=cluster, psp_netgraph=None,
        out_file=tmp_path / "decoders.cc",
    )
    content = out.read_text()
    # The total count line.
    assert "Total: 2 message types." in content
    # Handler entries, both messages present.
    assert '{ "MsgA", &decode_by_name, &stringify_by_name },' in content
    assert '{ "MsgB", &decode_by_name, &stringify_by_name },' in content
    # Header banner.
    assert "AUTO-GENERATED" in content


def test_generate_skips_non_identifier_message_names(tmp_path: Path):
    """Defensive: any message name with non-identifier chars is
    skipped rather than emitted as broken C string. Generated .cc
    must always compile.
    """
    cluster = _write(tmp_path, "ng.json", {
        "package": "test",
        "nodes": [{
            "name": "A",
            "ports": [],
            "signals": {
                "GoodName": {"direction": "out", "destinations": []},
                "bad name with spaces": {"direction": "out",
                                          "destinations": []},
                "also/bad": {"direction": "out", "destinations": []},
            },
        }],
    })
    out = generate(
        cluster_netgraph=cluster, psp_netgraph=None,
        out_file=tmp_path / "decoders.cc",
    )
    content = out.read_text()
    assert '"GoodName"' in content
    assert "bad name" not in content
    assert "also/bad" not in content


def test_generate_against_demo_fixture():
    """End-to-end smoke against the real demo netgraph fixture."""
    fixture = (
        Path(__file__).resolve().parent.parent.parent
        / "testing" / "rf_theia" / "scenarios" / "fixtures"
        / "demo_netgraph.json"
    )
    if not fixture.exists():
        pytest.skip(f"fixture {fixture} not present in this checkout")
    types = collect_message_types(
        cluster_netgraph=fixture, psp_netgraph=None,
    )
    # Demo3Way uses two message types — GetReply and Inc.
    assert set(types) == {"GetReply", "Inc"}, (
        f"demo netgraph yielded unexpected message set: {types}"
    )
