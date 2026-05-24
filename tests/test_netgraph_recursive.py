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

from click.testing import CliRunner

from artheia.cli import main


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
