"""--recursive gen-netgraph: follow `import system.x.*` and union
nodes + compositions from every reachable file.

Motivates the prereq for #359 (libtrace_decoder.so): the platform
trace decoder needs a single netgraph that lists every node across
the system, but `platform/system/system.art` declares NO nodes —
it's an import-driven aggregator. Without --recursive the output is
empty and the decoder has nothing to compile against.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from artheia.cli import main
from artheia.model import parse_file
from artheia.generators.netgraph import (
    DuplicateTipcAddress,
    build_netgraph,
)


def _write_pkg(pkg_dir, name, body):
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "package.art").write_text(body)


def _build_minigraph(tmp_path):
    """Lay out platform/system/ with two leaf packages + an aggregator."""
    sysroot = tmp_path / "platform" / "system"

    _write_pkg(
        sysroot / "alpha",
        "alpha",
        """\
package system.alpha

message AlphaSignal { uint32 v }

interface senderReceiver AlphaIf {
    data AlphaSignal sig
}

node atomic AlphaNode {
    tipc type=0x80010001 instance=0
    ports {
        sender out provides AlphaIf
    }
}

composition AlphaComp {
    prototype AlphaNode alpha on process P
}
""",
    )

    _write_pkg(
        sysroot / "beta",
        "beta",
        """\
package system.beta

message BetaSignal { uint32 v }

interface senderReceiver BetaIf {
    data BetaSignal sig
}

node atomic BetaNode {
    tipc type=0x80010002 instance=0
    ports {
        sender out provides BetaIf
    }
}

composition BetaComp {
    prototype BetaNode beta on process P
}
""",
    )

    aggregator = sysroot / "system.art"
    aggregator.write_text(
        """\
package system

import system.alpha.*
import system.beta.*

composition AlphaComp { }
composition BetaComp { }
"""
    )
    return aggregator


def test_recursive_aggregator_unions_nodes(tmp_path):
    """Aggregator file with 0 inline nodes — --recursive must pull
    every NodeDecl + non-stub CompositionDecl from imported packages."""
    aggregator = _build_minigraph(tmp_path)
    out = tmp_path / "netgraph.json"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["gen-netgraph", "--recursive", str(aggregator), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(out.read_text())
    names = sorted(n["name"] for n in data["nodes"])
    assert names == ["AlphaNode", "BetaNode"], names

    # Forward-decl stub compositions in the aggregator must NOT win
    # over the real bodies in alpha/beta — there's only one copy of
    # each name in the output, and connections should be visible.
    comp_names = sorted(c["name"] for c in data["compositions"])
    assert comp_names == ["AlphaComp", "BetaComp"], comp_names


def test_non_recursive_aggregator_is_empty(tmp_path):
    """Without --recursive the same aggregator file is essentially
    empty — proves --recursive is doing real work."""
    aggregator = _build_minigraph(tmp_path)
    out = tmp_path / "netgraph.json"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["gen-netgraph", str(aggregator), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(out.read_text())
    assert data["nodes"] == []
    # Only the two empty stub compositions from the aggregator itself.
    assert sorted(c["name"] for c in data["compositions"]) == ["AlphaComp", "BetaComp"]


def test_recursive_leaf_file_is_noop(tmp_path):
    """For a leaf file with no imports, --recursive must produce
    output identical to the default mode — regression check."""
    _build_minigraph(tmp_path)
    leaf = tmp_path / "platform" / "system" / "alpha" / "package.art"

    runner = CliRunner()
    out_norec = tmp_path / "norec.json"
    out_rec = tmp_path / "rec.json"

    r1 = runner.invoke(main, ["gen-netgraph", str(leaf), "--out", str(out_norec)])
    r2 = runner.invoke(main, ["gen-netgraph", "-R", str(leaf), "--out", str(out_rec)])
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output

    assert out_norec.read_text() == out_rec.read_text()


# ---- TIPC uniqueness (system-wide invariant) -------------------------------

def _write_node_pkg(pkg_dir, pkg_name, node_name, ttype, tinst="0"):
    """One-package, one-node .art with a given TIPC address."""
    _write_pkg(
        pkg_dir,
        pkg_name,
        f"""\
package system.{pkg_name}

interface senderReceiver {node_name}If {{ }}

node atomic {node_name} {{
    tipc type={ttype} instance={tinst}
    ports {{ sender out provides {node_name}If }}
}}
""",
    )


def test_distinct_tipc_across_files_fails(tmp_path):
    """Two separately-valid packages whose nodes collide on a TIPC
    address. Each parses clean on its own (the parse-time per-model
    validator sees no clash), so the system-wide netgraph union is the
    ONLY stage that catches it — that's the invariant this guards."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_node_pkg(a, "a", "Alpha", "0x95000001")
    _write_node_pkg(b, "b", "Beta", "0x95000001")

    model_a = parse_file(str(a / "package.art"))
    model_b = parse_file(str(b / "package.art"))

    # Each alone: fine.
    build_netgraph(model_a)
    build_netgraph(model_b)

    # Unioned (what --recursive does): collision detected.
    with pytest.raises(DuplicateTipcAddress) as ei:
        build_netgraph(model_a, extra_models=[model_b])
    msg = str(ei.value)
    assert "Alpha" in msg and "Beta" in msg
    assert "0x95000001" in msg


def test_distinct_tipc_hex_decimal_equivalence(tmp_path):
    """0x10 and 16 are the SAME address — the check must normalize
    through _parse_hex_or_int before comparing, not compare the raw
    string forms."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_node_pkg(a, "a", "Alpha", "0x10")
    _write_node_pkg(b, "b", "Beta", "16")

    model_a = parse_file(str(a / "package.art"))
    model_b = parse_file(str(b / "package.art"))
    with pytest.raises(DuplicateTipcAddress):
        build_netgraph(model_a, extra_models=[model_b])


def test_distinct_tipc_different_instance_ok(tmp_path):
    """Same type, different instance is a DISTINCT address — must pass.
    The address is the (type, instance) pair, not the type alone."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_node_pkg(a, "a", "Alpha", "0x95000002", tinst="0")
    _write_node_pkg(b, "b", "Beta", "0x95000002", tinst="1")

    model_a = parse_file(str(a / "package.art"))
    model_b = parse_file(str(b / "package.art"))
    doc = build_netgraph(model_a, extra_models=[model_b])
    assert {n["name"] for n in doc["nodes"]} == {"Alpha", "Beta"}


# ---- `artheia check-addresses` CLI gate ------------------------------------

def _aggregator_over(sysroot, *pkg_names):
    """An import-driven aggregator unioning the given sibling packages."""
    agg = sysroot / "system.art"
    imports = "\n".join(f"import system.{p}.*" for p in pkg_names)
    agg.write_text(f"package system\n\n{imports}\n")
    return agg


def test_check_addresses_cli_ok(tmp_path):
    """check-addresses exits 0 + reports the node count when every TIPC
    address across the unioned packages is distinct."""
    sysroot = tmp_path / "platform" / "system"
    _write_node_pkg(sysroot / "a", "a", "Alpha", "0x95000010")
    _write_node_pkg(sysroot / "b", "b", "Beta", "0x95000011")
    agg = _aggregator_over(sysroot, "a", "b")

    res = CliRunner().invoke(main, ["check-addresses", str(agg)])
    assert res.exit_code == 0, res.output
    assert "all TIPC addresses distinct" in res.output


def test_check_addresses_cli_detects_cross_fc_collision(tmp_path):
    """The gate's reason for existing: two SEPARATE packages (the com/per
    case) whose nodes share a TIPC address. Each parses clean alone; the
    recursive union the CLI builds is what catches it. Exit non-zero, name
    both nodes + the address."""
    sysroot = tmp_path / "platform" / "system"
    _write_node_pkg(sysroot / "a", "a", "Alpha", "0x80010008")
    _write_node_pkg(sysroot / "b", "b", "Beta", "0x80010008")
    agg = _aggregator_over(sysroot, "a", "b")

    res = CliRunner().invoke(main, ["check-addresses", str(agg)])
    assert res.exit_code == 1, res.output
    assert "Alpha" in res.output and "Beta" in res.output
    assert "0x80010008" in res.output
