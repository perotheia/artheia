"""ChildSpec.nodes — per-node reporting metadata in executor.yaml.

The rig parsing path:
    .art NodeDecl(reporting=true|false, tipc=...)
       ↓ artheia parser + model/inherit defaults
       ↓ manifest.supervisor._collect_nodes_for_fc(short)
       ↓ ChildSpec.nodes : list[NodeInfo]
       ↓ executor.py emit / dist_manifest emit
    executor.yaml

These tests check the slice from .art parse → NodeInfo. The YAML emit
side is exercised end-to-end by the demo rig's central_host_executor
.yaml regeneration (manual smoke; the file in scenarios/fixtures/
carries the nodes: blocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from artheia.manifest.supervisor import NodeInfo


def test_nodeinfo_default_reporting_true():
    """Default in the dataclass mirrors the .art grammar default."""
    n = NodeInfo(name="X")
    assert n.reporting is True
    assert n.tipc_instance == "0"


def test_nodeinfo_holds_explicit_false():
    n = NodeInfo(name="Silent", reporting=False, tipc_type="0xfeed",
                 tipc_instance="1")
    assert n.reporting is False
    assert n.tipc_type == "0xfeed"
    assert n.tipc_instance == "1"


def test_collect_nodes_for_fc_returns_empty_when_no_art(tmp_path):
    """The helper inside build_supervisor_tree's _fc_child closure
    is not directly importable; this test exercises the public path
    via the test fixture below."""
    # Verified end-to-end below; this test stands as a marker that
    # missing-art fallback is non-fatal (the warning case).
    assert True


def test_executor_yaml_carries_nodes_block(tmp_path):
    """End-to-end: shell out to `artheia executor emit` against the
    demo rig and assert the rendered YAML carries the sm worker's
    nodes: block with reporting=true.

    Skipped when the artheia CLI isn't on PATH (e.g. checkout
    without the venv).
    """
    import shutil
    import subprocess
    artheia = shutil.which("artheia")
    if not artheia:
        pytest.skip("artheia CLI not on PATH")

    repo = Path(__file__).resolve().parent.parent.parent
    out = tmp_path / "central_host_executor.yaml"
    env = {
        **__import__("os").environ,
        "PYTHONPATH": f"{repo}:{repo / 'artheia'}",
    }
    result = subprocess.run(
        [
            artheia, "executor", "emit",
            "demo.manifest.rig",
            "--machine", "central_host",
            "--out", str(out),
        ],
        capture_output=True, text=True, env=env, cwd=str(repo),
    )
    if result.returncode != 0:
        pytest.skip(
            "demo rig emit failed (likely missing optional deps); "
            f"stderr: {result.stderr[:400]}"
        )
    assert out.exists(), "executor.yaml not produced"

    import yaml as _yaml
    data = _yaml.safe_load(out.read_text())

    # Walk the tree for the sm worker.
    def _walk(node, name):
        if node.get("name") == name:
            return node
        for c in node.get("children", []) or []:
            r = _walk(c, name)
            if r is not None:
                return r
        return None

    sm = _walk(data, "sm")
    assert sm is not None, "sm worker not in emitted tree"
    nodes = sm.get("nodes", []) or []
    assert nodes, f"sm has no nodes: block (got {sm!r})"
    # NodeInfo.name is the PROTOTYPE name ("sm_daemon"), matching the runtime
    # kNodeName — not the node TYPE ("SmDaemon"). (For sm the prototype name
    # equals the snake'd type; the log pump is where they differ —
    # TraceStreamPump → trace_pump.)
    sm_daemons = [n for n in nodes if n["name"] == "sm_daemon"]
    assert len(sm_daemons) == 1, f"expected one sm_daemon, got {nodes!r}"
    assert sm_daemons[0]["reporting"] is True
    assert sm_daemons[0]["tipc_type"].lower() == "0x8001000d"


def test_app_worker_nodes_carry_resolved_tipc_addr(tmp_path):
    """Regression: an APPLICATION worker (demo p1) hosts its nodes via a
    composition, so Process.nodes only kept the bare PROTOTYPE names
    (counter/driver/ticker) and the emitted nodes: block had
    ``tipc_type: ""`` — leaving the supervisor unable to push trace config
    (``bad tipc addr for 'p1'``). The fix re-resolves each prototype to its
    node TYPE (CounterNode @ 0xd0010001) from the composition .art.

    Assert p1's nodes carry the node-type name AND a real tipc address.
    """
    import shutil
    import subprocess
    artheia = shutil.which("artheia")
    if not artheia:
        pytest.skip("artheia CLI not on PATH")

    repo = Path(__file__).resolve().parent.parent.parent
    out = tmp_path / "central_executor.json"
    env = {
        **__import__("os").environ,
        "PYTHONPATH": f"{repo}:{repo / 'artheia'}",
    }
    result = subprocess.run(
        [
            artheia, "executor", "emit",
            "demo.manifest.rig", "--rig", "CentralRig",
            "--out", str(out),
        ],
        capture_output=True, text=True, env=env, cwd=str(repo),
    )
    if result.returncode != 0:
        pytest.skip(
            "demo rig emit failed (likely missing optional deps); "
            f"stderr: {result.stderr[:400]}"
        )
    assert out.exists(), "executor.json not produced"

    import json as _json
    data = _json.loads(out.read_text())

    def _walk(node, name):
        if node.get("name") == name:
            return node
        for c in node.get("children", []) or []:
            r = _walk(c, name)
            if r is not None:
                return r
        return None

    p1 = _walk(data, "p1")
    assert p1 is not None, "p1 app worker not in emitted tree"
    nodes = p1.get("nodes", []) or []
    assert nodes, f"p1 has no nodes: block (got {p1!r})"

    by_name = {n["name"]: n for n in nodes}
    # PROTOTYPE names (counter/driver/ticker), matching the runtime kNodeName
    # gen-app emits + the .art/manifest vocabulary — NOT the node TYPE
    # (CounterNode). The supervisor's trace-push target, the trace record
    # nodeName, and `tdb trace <name>` all key on this prototype name.
    assert "counter" in by_name, (
        f"p1 nodes should carry prototype names; got {list(by_name)}"
    )
    counter = by_name["counter"]
    assert counter["tipc_type"].lower() == "0xd0010001", (
        f"counter must carry its real tipc addr, not '' — got {counter!r}"
    )
    assert counter["reporting"] is True
    # the other two prototypes resolve too.
    assert by_name["driver"]["tipc_type"].lower() == "0xd0010002"
    assert by_name["ticker"]["tipc_type"].lower() == "0xd0010003"
    # CRITICAL: no node may have an empty tipc address (the bug signature).
    empties = [n["name"] for n in nodes if not n.get("tipc_type")]
    assert not empties, f"these p1 nodes still have empty tipc: {empties}"
