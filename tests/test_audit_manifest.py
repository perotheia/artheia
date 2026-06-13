"""Smoke test for ``artheia audit-manifest``.

The audit left-joins ``platform/system/system.art`` (the workspace
.art tree, transitively resolved via ``import`` lines) against
``apps.manifest.rig`` (the Python-side manifest) and reports gaps —
missing :class:`ApplicationManifest` / :class:`SwComponent` /
:class:`Process` entries for clusters/compositions/prototypes
declared in the .art.

This test asserts that the **demo rig is currently clean** — exit 0,
"✓ no gaps". If a future change drifts the .art away from the rig
(or vice versa), this test fails and prompts the author to update
both halves together.

The audit runs as a real ``artheia`` subprocess so this also
exercises the CLI wiring end-to-end.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
ART_FILE = REPO / "platform" / "system" / "system.art"
RIG_TARGET = "apps.manifest.rig"


def _artheia_bin() -> str | None:
    """Locate the ``artheia`` CLI: prefer $PATH, then the workspace venv."""
    found = shutil.which("artheia")
    if found:
        return found
    candidate = REPO / ".venv" / "bin" / "artheia"
    if candidate.exists():
        return str(candidate)
    return None


def test_audit_manifest_demo_rig_is_clean():
    """``artheia audit-manifest platform/system/system.art
    apps.manifest.rig`` exits 0 with no gaps."""
    artheia = _artheia_bin()
    if artheia is None:
        pytest.skip("artheia CLI not on PATH and not in workspace .venv")
    if not ART_FILE.exists():
        pytest.skip(f"{ART_FILE} not present in this checkout")

    # Run from REPO so Python picks up `apps.manifest.rig` as a top-
    # level module (it lives at REPO/demo/manifest/rig.py).
    #
    # Do NOT add REPO to PYTHONPATH: the repo root contains an
    # `artheia/` subdirectory (the sub-repo), and having it on the
    # path turns `artheia` into a namespace package, shadowing the
    # editable-installed real package and breaking
    # `from . import __version__` in artheia/cli.py.
    # --rig DemoSoftware: the central/compute/admin multi-host spec that
    # covers every cluster member (incl. compute's p3 + shwa). Without
    # it the default *Software ranking picks CentralSoftware (single
    # machine), whose component set omits the compute-pinned members and
    # the audit reports them as gaps.
    result = subprocess.run(
        [artheia, "audit-manifest", str(ART_FILE), RIG_TARGET,
         "--rig", "DemoSoftware"],
        cwd=REPO,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"audit-manifest found gaps between {ART_FILE} and {RIG_TARGET}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert "no gaps" in result.stdout, (
        f"expected 'no gaps' in stdout; got:\n{result.stdout}"
    )
