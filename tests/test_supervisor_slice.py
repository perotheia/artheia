"""Per-machine supervisor-tree slicing — regression tests.

Phase C of docs/tasks/PROGRESS/per-app-supervisor/. Asserts that
``build_supervisor_tree(rig, machine=...)`` produces the right
sub-tree for each machine in the demo rig:

  - central gets the platform FCs + supervisor/gateway app_sup
  - compute gets shwa + the three demo binaries
  - admin gets an empty tree (no supervised processes)

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
# The multi-host (central + compute + admin) spec these slice tests assert on
# now lives in zonal_rig.py — apps.manifest.rig was trimmed to single-machine
# (central only). zonal_rig carries DemoSoftware / ComputeHost / AdminHost.
RIG_TARGET = "apps.manifest.zonal_rig"


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
    # (single machine), which has no compute/admin dirs.
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
    """``shwa`` (the only compute-pinned FC) lands on compute and
    is NOT in central's tree."""
    central_leaves = _tree_leaf_names(_supervisor_tree(emitted, "central"))
    compute_leaves = _tree_leaf_names(_supervisor_tree(emitted, "compute"))
    assert "shwa" in compute_leaves, (
        f"shwa must be on compute; got {sorted(compute_leaves)}"
    )
    assert "shwa" not in central_leaves, (
        f"shwa must NOT leak to central; got {sorted(central_leaves)}"
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
    for machine in ("central", "compute"):
        leaves = _tree_leaf_names(_supervisor_tree(emitted, machine))
        for fabric in ("supervisor", "gateway"):
            assert fabric not in leaves, (
                f"{fabric!r} is systemd-managed fabric, not a supervised "
                f"leaf; found it in {machine} tree: {sorted(leaves)}"
            )


def test_demo_binaries_split_central_compute(emitted):
    """The demo per-process binaries split across the two TARGET machines per the
    rig's _COMPUTE_APPS partition: p1/p2/p4 run on central, p3 on compute (the
    accelerator box, alongside shwa). Each lands on its OWN machine and no other.
    Idents come from `cluster Applications` in the .art via the generated
    applications.py."""
    compute_leaves = _tree_leaf_names(_supervisor_tree(emitted, "compute"))
    central_leaves = _tree_leaf_names(_supervisor_tree(emitted, "central"))
    # p3 → compute only.
    assert "p3" in compute_leaves, f"p3 must be on compute; got {sorted(compute_leaves)}"
    assert "p3" not in central_leaves, f"p3 must NOT leak to central; got {sorted(central_leaves)}"
    # p1/p2/p4 → central only.
    for demo in ("p1", "p2", "p4"):
        assert demo in central_leaves, (
            f"{demo!r} must be on central; got {sorted(central_leaves)}"
        )
        assert demo not in compute_leaves, (
            f"{demo!r} must NOT leak to compute; got {sorted(compute_leaves)}"
        )


def test_admin_has_empty_supervisor_tree(emitted):
    """The HOST/admin machine runs no supervisor of its own — its
    tree is just the root with no children."""
    tree = _supervisor_tree(emitted, "admin")
    assert tree.get("name") == "root", (
        f"admin tree's root should be named 'root'; got {tree}"
    )
    assert (tree.get("children") or []) == [], (
        f"admin tree must have no children; got "
        f"{tree.get('children')}"
    )


def test_processes_list_matches_tree_leaves(emitted):
    """The ``processes`` list in each machine's execution.json is in
    lockstep with the surviving leaves of its sliced supervisor tree.

    Walks the tree, collects leaf names; compares to the processes list.
    """
    for machine in ("central", "compute", "admin"):
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


# ---------------------------------------------------------------------------
# THEIA_LOGGER — per-process logger sink (rig default + per-process override)
# ---------------------------------------------------------------------------

def test_logger_for_process_resolution():
    """_logger_for_process expands a file:<dir> to a per-process file, leaves
    explicit .log paths and other kinds untouched."""
    from artheia.manifest.supervisor import _logger_for_process as f
    assert f("file:/var/log/theia", "sm") == "file:/var/log/theia/sm.log"
    assert f("file:/tmp/theia", "p1") == "file:/tmp/theia/p1.log"
    assert f("file:/tmp/x.log", "sm") == "file:/tmp/x.log"   # explicit leaf kept
    assert f("syslog", "sm") == "syslog"
    assert f("stdio", "sm") == "stdio"


def test_logger_precedence_fallback_and_rig_and_process():
    """THEIA_LOGGER precedence: Process.logger > rig.logger > /tmp/theia."""
    import dataclasses
    from artheia.manifest.supervisor import build_supervisor_tree
    import apps.manifest.rig as R

    def _logger_of(spec, name):
        stack = [spec]
        while stack:
            n = stack.pop()
            for c in getattr(n, "children", []):
                if getattr(c, "name", "") == name:
                    return (getattr(c, "env", None) or {}).get("THEIA_LOGGER")
                stack.append(c)
        return None

    rig = R.CentralRig

    # 1. no rig logger → /tmp/theia/<name>.log fallback
    assert _logger_of(build_supervisor_tree(rig), "sm") == "file:/tmp/theia/sm.log"

    # 2. rig-level default → <dir>/<name>.log for every process
    rig2 = dataclasses.replace(rig, logger="file:/var/log/theia")
    assert _logger_of(build_supervisor_tree(rig2), "per") == \
        "file:/var/log/theia/per.log"

    # 3. per-process override beats the rig default
    ems = [dataclasses.replace(em, logger="syslog")
           if getattr(em, "name", "") == "sm" else em
           for em in rig2.execution_manifests]
    rig3 = dataclasses.replace(rig2, execution_manifests=ems)
    spec3 = build_supervisor_tree(rig3)
    assert _logger_of(spec3, "sm") == "syslog"                      # override
    assert _logger_of(spec3, "per") == "file:/var/log/theia/per.log"  # rig default
