"""Per-machine supervisor-tree slicing — regression tests.

Phase C of docs/tasks/PROGRESS/per-app-supervisor/. Asserts that
``build_supervisor_tree(rig, machine=...)`` produces the right
sub-tree for each machine in the demo rig:

  - central_host gets the platform FCs + supervisor/gateway app_sup
  - compute_host gets shwa + the three demo binaries
  - admin_host gets an empty tree (no supervised processes)

These tests also exercise the dist_manifest's per-machine
``execution.yaml`` emission (the file the supervisor-gui reads to
render each machine's tree side-by-side).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
RIG_TARGET = "demo.manifest.rig"


def _artheia_bin() -> str | None:
    found = shutil.which("artheia")
    if found:
        return found
    candidate = REPO / ".venv" / "bin" / "artheia"
    if candidate.exists():
        return str(candidate)
    return None


@pytest.fixture
def emitted(tmp_path):
    """Run ``artheia generate-manifest`` and return the output dir."""
    artheia = _artheia_bin()
    if artheia is None:
        pytest.skip("artheia CLI not on PATH and not in workspace .venv")
    result = subprocess.run(
        [artheia, "generate-manifest", RIG_TARGET, "--out", str(tmp_path)],
        cwd=REPO,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"generate-manifest failed:\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return tmp_path


def _tree_leaf_names(node: dict) -> set[str]:
    """Flatten a supervisor tree dict (the YAML shape) into the set of
    leaf names. Sub-supervisors have ``children``; leaves don't."""
    out: set[str] = set()

    def _walk(n: dict) -> None:
        if "children" in n:
            for c in n.get("children", []) or []:
                _walk(c)
        else:
            out.add(n["name"])

    _walk(node)
    return out


def _supervisor_tree(emitted: Path, machine: str) -> dict:
    """Read the supervisor sub-tree from ``<machine>/execution.yaml``."""
    path = emitted / machine / "execution.yaml"
    doc = yaml.safe_load(path.read_text())
    return doc.get("supervisor_tree") or {}


def _process_names(emitted: Path, machine: str) -> set[str]:
    """Read the per-machine Process list."""
    path = emitted / machine / "execution.yaml"
    doc = yaml.safe_load(path.read_text())
    return {p["name"] for p in (doc.get("processes") or [])}


def test_shwa_pinned_to_compute(emitted):
    """``shwa`` (the only compute-pinned FC) lands on compute_host and
    is NOT in central_host's tree."""
    central_leaves = _tree_leaf_names(_supervisor_tree(emitted, "central_host"))
    compute_leaves = _tree_leaf_names(_supervisor_tree(emitted, "compute_host"))
    assert "shwa" in compute_leaves, (
        f"shwa must be on compute_host; got {sorted(compute_leaves)}"
    )
    assert "shwa" not in central_leaves, (
        f"shwa must NOT leak to central_host; got {sorted(central_leaves)}"
    )


def test_platform_fabric_pinned_to_central(emitted):
    """``supervisor`` and ``gateway`` are platform fabric — both on
    central, neither on compute. Their PTM entries override the AA's
    bare host_machine fallback."""
    central_leaves = _tree_leaf_names(_supervisor_tree(emitted, "central_host"))
    compute_leaves = _tree_leaf_names(_supervisor_tree(emitted, "compute_host"))
    for fabric in ("supervisor", "gateway"):
        assert fabric in central_leaves, (
            f"{fabric!r} must be on central_host; got {sorted(central_leaves)}"
        )
        assert fabric not in compute_leaves, (
            f"{fabric!r} must NOT be on compute_host; got "
            f"{sorted(compute_leaves)}"
        )


def test_demo_binaries_pinned_to_compute(emitted):
    """The three demo per-process binaries (compute_app) all land on
    compute_host and on NO other machine."""
    compute_leaves = _tree_leaf_names(_supervisor_tree(emitted, "compute_host"))
    central_leaves = _tree_leaf_names(_supervisor_tree(emitted, "central_host"))
    for demo in ("demo_p1", "demo_p2", "demo_p3"):
        assert demo in compute_leaves, (
            f"{demo!r} must be on compute_host; got {sorted(compute_leaves)}"
        )
        assert demo not in central_leaves, (
            f"{demo!r} must NOT leak to central_host; got "
            f"{sorted(central_leaves)}"
        )


def test_admin_host_has_empty_supervisor_tree(emitted):
    """The HOST/admin machine runs no supervisor of its own — its
    tree is just the root with no children."""
    tree = _supervisor_tree(emitted, "admin_host")
    assert tree.get("name") == "root", (
        f"admin_host tree's root should be named 'root'; got {tree}"
    )
    assert (tree.get("children") or []) == [], (
        f"admin_host tree must have no children; got "
        f"{tree.get('children')}"
    )


def test_processes_list_matches_tree_leaves(emitted):
    """The ``processes`` list in each machine's execution.yaml is in
    lockstep with the surviving leaves of its sliced supervisor tree.

    Walks the tree, collects leaf names; compares to the processes list.
    """
    for machine in ("central_host", "compute_host", "admin_host"):
        tree = _supervisor_tree(emitted, machine)
        leaves = _tree_leaf_names(tree)
        procs = _process_names(emitted, machine)
        # Some leaves (AUTO_APPS_CHILDREN expansions) point at
        # SwComponents that don't have a matching Process — those
        # appear in the tree but not in processes. So procs is a
        # subset of (FC) leaves; we don't assert strict equality.
        # But every Process should appear in the tree.
        assert procs.issubset(leaves), (
            f"{machine}: processes {sorted(procs - leaves)!r} are in "
            f"the Process list but missing from the supervisor tree"
        )
