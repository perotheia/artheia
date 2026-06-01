"""Smoke tests for the per-machine deploy manifest emitter.

Asserts the **strict** ServiceInstance filter behavior:

- An instance lands in machine ``M``'s ``service.yaml`` if and only if
  its ``remote_machine == M``.
- No loose fallback that spreads compute-only services to every
  machine.

Drives ``artheia generate-manifest demo.manifest.rig`` as a subprocess
to exercise the full CLI → emitter → YAML round-trip.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
RIG_TARGET = "demo.manifest.zonal_rig"  # multi-host spec (central trimmed to single-machine)


def _artheia_bin() -> str | None:
    found = shutil.which("artheia")
    if found:
        return found
    candidate = REPO / ".venv" / "bin" / "artheia"
    if candidate.exists():
        return str(candidate)
    return None


@pytest.fixture
def emitted_manifest(tmp_path):
    """Run ``artheia generate-manifest`` and return the output dir."""
    artheia = _artheia_bin()
    if artheia is None:
        pytest.skip("artheia CLI not on PATH and not in workspace .venv")
    # --rig DemoSoftware: the central/compute/admin multi-host spec.
    # Without it the default *Software ranking picks CentralSoftware
    # (single machine), so the compute_host/admin_host dirs are absent.
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


def _instance_names(manifest_dir: Path, machine: str) -> set[str]:
    """Return the set of ServiceInstance names in ``<machine>/service.json``
    (manifests are JSON-only since #380)."""
    path = manifest_dir / machine / "service.json"
    doc = json.loads(path.read_text())
    names: set[str] = set()
    for sm in doc.get("service_manifests", []) or []:
        for inst in sm.get("instances", []) or []:
            names.add(inst["name"])
    return names


def test_shwa_only_on_compute_host(emitted_manifest):
    """shwa is compute-only: present in compute_host, ABSENT from
    central_host and admin_host."""
    compute = _instance_names(emitted_manifest, "compute_host")
    central = _instance_names(emitted_manifest, "central_host")
    admin = _instance_names(emitted_manifest, "admin_host")

    assert "shwa" in compute, (
        f"shwa must be on compute_host; got {sorted(compute)}"
    )
    assert "shwa" not in central, (
        f"shwa must NOT leak to central_host; got {sorted(central)}"
    )
    assert "shwa" not in admin, (
        f"shwa must NOT leak to admin_host; got {sorted(admin)}"
    )


def test_central_services_pinned_to_central(emitted_manifest):
    """The control-plane FCs (per/log/sm/ucm/com) belong on central_host
    and NOT on compute_host."""
    central = _instance_names(emitted_manifest, "central_host")
    compute = _instance_names(emitted_manifest, "compute_host")

    for svc in ["per", "log", "sm", "ucm"]:  # com retired
        assert svc in central, (
            f"{svc!r} must be on central_host; got {sorted(central)}"
        )
        assert svc not in compute, (
            f"{svc!r} must NOT leak to compute_host; got {sorted(compute)}"
        )


def test_admin_host_has_no_service_instances(emitted_manifest):
    """The admin (HostMachine) doesn't host any FC service instances —
    its service.yaml should be empty."""
    admin = _instance_names(emitted_manifest, "admin_host")
    assert admin == set(), (
        f"admin_host should host no service instances; got {sorted(admin)}"
    )


def test_strict_filter_no_unpinned_instances_anywhere(emitted_manifest):
    """Every emitted ServiceInstance has a remote_machine matching its
    host. Catches regressions where the loose fallback creeps back in."""
    for machine in ["admin_host", "central_host", "compute_host"]:
        path = emitted_manifest / machine / "service.json"
        doc = json.loads(path.read_text())
        for sm in doc.get("service_manifests", []) or []:
            for inst in sm.get("instances", []) or []:
                rm = inst.get("remote_machine", "")
                assert rm == machine, (
                    f"strict-filter violation: {machine}/service.json "
                    f"contains instance {inst['name']!r} with "
                    f"remote_machine={rm!r}"
                )
