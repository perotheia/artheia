"""Per-machine supervisor-tree slicing — regression tests.

Phase C of docs/tasks/PROGRESS/per-app-supervisor/. Asserts that
``build_supervisor_tree(rig, machine=...)`` produces the right
sub-tree for each machine in the demo rig:

  - central_host gets the platform FCs + supervisor/gateway app_sup
  - compute_host gets shwa + the three demo binaries
  - admin_host gets an empty tree (no supervised processes)

These tests also exercise the dist_manifest's per-machine
``execution.json`` emission (the file the supervisor-gui reads to
render each machine's tree side-by-side).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
    # --rig DemoSoftware: the central/compute/admin multi-host spec.
    # Without it the default *Software ranking picks CentralSoftware
    # (single machine), which has no compute_host/admin_host dirs.
    result = subprocess.run(
        [artheia, "generate-manifest", RIG_TARGET,
         "--rig", "DemoSoftware", "--out", str(tmp_path)],
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


def _execution_doc(emitted: Path, machine: str) -> dict:
    """Load ``<machine>/execution.json`` (manifests are JSON-only since
    #380; the dist emitter no longer writes YAML siblings)."""
    return json.loads((emitted / machine / "execution.json").read_text())


def _supervisor_tree(emitted: Path, machine: str) -> dict:
    """Read the supervisor sub-tree from ``<machine>/execution.json``."""
    return _execution_doc(emitted, machine).get("supervisor_tree") or {}


def _process_names(emitted: Path, machine: str) -> set[str]:
    """Read the per-machine Process list."""
    doc = _execution_doc(emitted, machine)
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


def test_platform_fabric_not_in_supervised_tree(emitted):
    """``supervisor`` and ``gateway`` are platform fabric — managed by
    systemd as opkg units (see _PLATFORM_OPKG_ARTIFACTS), NOT supervised
    leaves under app_sup. ``supervisor`` IS the tree root; ``gateway``
    boots beside it. They must not appear as leaves on any machine.

    (They used to be synthesized as app_sup leaves by the dropped
    AUTO_APPS_CHILDREN expansion. With apps now resolving through real
    execution-manifest Processes, only genuine supervised children — the
    demo binaries — land under app_sup.)"""
    for machine in ("central_host", "compute_host"):
        leaves = _tree_leaf_names(_supervisor_tree(emitted, machine))
        for fabric in ("supervisor", "gateway"):
            assert fabric not in leaves, (
                f"{fabric!r} is systemd-managed fabric, not a supervised "
                f"leaf; found it in {machine} tree: {sorted(leaves)}"
            )


def test_demo_binaries_pinned_to_compute(emitted):
    """The three demo per-process binaries (compute_app) all land on
    compute_host and on NO other machine. Idents come from `cluster
    Applications` in the .art (p1/p2/p3 — the generated applications.py
    drives them now, no longer the hand-written demo_p* names)."""
    compute_leaves = _tree_leaf_names(_supervisor_tree(emitted, "compute_host"))
    central_leaves = _tree_leaf_names(_supervisor_tree(emitted, "central_host"))
    for demo in ("p1", "p2", "p3"):
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
    """The ``processes`` list in each machine's execution.json is in
    lockstep with the surviving leaves of its sliced supervisor tree.

    Walks the tree, collects leaf names; compares to the processes list.
    """
    for machine in ("central_host", "compute_host", "admin_host"):
        tree = _supervisor_tree(emitted, machine)
        leaves = _tree_leaf_names(tree)
        procs = _process_names(emitted, machine)
        # Every leaf — FC or application — resolves through a Process
        # in the execution manifest now (no synthetic SwComponent
        # expansions), so the Process list should appear among the
        # leaves. We keep the subset assertion (rather than strict
        # equality) because an FC declared in the tree but with no
        # built binary still emits a leaf without a matching Process.
        assert procs.issubset(leaves), (
            f"{machine}: processes {sorted(procs - leaves)!r} are in "
            f"the Process list but missing from the supervisor tree"
        )
