"""Emit the per-machine deploy manifest set.

For each :class:`Machine` in the rig we write four YAML files (the
four AUTOSAR manifest kinds, AA-aligned filenames):

  dist/manifest/<machine>/machine.yaml      ← per-ECU config + OS deps
  dist/manifest/<machine>/application.yaml  ← AAs hosted on this ECU
  dist/manifest/<machine>/service.yaml      ← service instances here
  dist/manifest/<machine>/execution.yaml    ← Processes + startup conf

Plus a top-level ``index.yaml`` so Puppet's bootstrap can find each
machine's directory by hostname.

This intentionally REPLACES the legacy single-file output of
``artheia generate-manifest`` — each ECU's Puppet runs reads its own
directory; nothing should consume the all-machines flat YAML.

Filtering rule: an :class:`ApplicationManifest` lands in machine M's
``application.yaml`` iff ``application.host_machine == M.name``.
A :class:`ServiceInstance` lands in M's ``service.yaml`` iff its
``remote_machine == M.name`` (or it has no ``remote_machine`` and the
parent service is bound here by default — for the first pass we
simply include EVERY ServiceManifest in EVERY machine's
``service.yaml``, which is correct for a single-machine rig and
loose-but-safe for multi-machine. The strict filter lands in a
follow-up).
A :class:`Process` lands in M's ``execution.yaml`` iff there's an
entry in ``rig.process_to_machine_mappings`` binding it to M; failing
that, every Process is included on every machine (best-effort
fallback while ``process_to_machine_mappings`` is sparse).
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Dataclass → dict serializer (Enum + IPv4Address aware).
# ---------------------------------------------------------------------------


def _serialize(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {
            f.name: _serialize(getattr(v, f.name))
            for f in dataclasses.fields(v)
        }
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (IPv4Address, IPv6Address)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_serialize(x) for x in v]
    if isinstance(v, dict):
        return {k: _serialize(x) for k, x in v.items()}
    return v


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Per-machine writers.
# ---------------------------------------------------------------------------


def _machine_payload(machine) -> dict:
    """Machine manifest body: the Machine dataclass + a flag noting
    it's the ECU-provisioning view."""
    return {
        "kind": "MachineManifest",
        "machine": _serialize(machine),
    }


def _application_payload(rig, machine_name: str) -> dict:
    """Applications hosted on *machine_name*."""
    apps = [a for a in rig.applications if a.host_machine == machine_name]
    return {
        "kind": "ApplicationManifest",
        "host_machine": machine_name,
        "applications": [_serialize(a) for a in apps],
    }


def _service_payload(rig, machine_name: str) -> dict:
    """Service instances local to or hosted on *machine_name*.

    Strict reading: an instance's ``remote_machine`` either matches
    this machine or is empty (= here by default). Loose fallback for
    rigs that haven't filled in ``remote_machine``: include every
    service manifest. Today we do strict-then-fallback per service
    manifest: if any instance pins to *this* machine, only those are
    included; otherwise the full manifest is.
    """
    payload_services = []
    for sm in rig.service_manifests:
        local_instances = [
            i for i in sm.instances
            if getattr(i, "remote_machine", "") in ("", machine_name)
        ]
        # If the instances declared explicit remote_machines and none of
        # them is THIS machine, skip the manifest entirely.
        if not local_instances and any(
            getattr(i, "remote_machine", "")
            for i in sm.instances
        ):
            continue
        copy = dataclasses.replace(sm, instances=local_instances or list(sm.instances))
        payload_services.append(_serialize(copy))
    return {
        "kind": "ServiceManifest",
        "host_machine": machine_name,
        "service_manifests": payload_services,
    }


def _execution_payload(rig, machine_name: str) -> dict:
    """Processes running on *machine_name*, plus the OTP supervisor
    sub-tree for this machine (if applicable).

    Selection:
      - If ``rig.process_to_machine_mappings`` names processes pinned
        to *machine_name*, use that list.
      - Otherwise (no PTM entry for this machine), emit every Process —
        a best-effort fallback for single-machine rigs.
    """
    ptm_for_machine = [
        m for m in rig.process_to_machine_mappings
        if getattr(m, "machine", "") == machine_name
    ]
    pinned_names = {m.process for m in ptm_for_machine if hasattr(m, "process")}
    if pinned_names:
        procs = [
            p for p in rig.execution_manifests
            if p.name in pinned_names
        ]
    else:
        procs = list(rig.execution_manifests)

    return {
        "kind": "ExecutionManifest",
        "host_machine": machine_name,
        "processes": [_serialize(p) for p in procs],
        "process_to_machine_mappings": [
            _serialize(m) for m in ptm_for_machine
        ],
        "node_to_cpu_mappings": [
            _serialize(m) for m in rig.node_to_cpu_mappings
            if getattr(m, "machine", "") in ("", machine_name)
        ],
    }


# ---------------------------------------------------------------------------
# Top-level entry.
# ---------------------------------------------------------------------------


def emit_dist_manifest(rig, out_dir: Path) -> list[Path]:
    """Write the per-machine manifest set rooted at *out_dir*.

    Returns the list of files written (for CLI output)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # Per-machine: 4 yaml files each.
    machine_names = [m.name for m in rig.machines]
    for machine in rig.machines:
        mdir = out_dir / machine.name
        mdir.mkdir(parents=True, exist_ok=True)
        for fname, payload in [
            ("machine.yaml",     _machine_payload(machine)),
            ("application.yaml", _application_payload(rig, machine.name)),
            ("service.yaml",     _service_payload(rig, machine.name)),
            ("execution.yaml",   _execution_payload(rig, machine.name)),
        ]:
            p = mdir / fname
            p.write_text(_dump(payload))
            written.append(p)

    # Top-level index — Puppet's bootstrap finds the per-host dir here.
    index = {
        "kind": "RigIndex",
        "vehicle": _serialize(rig.vehicle),
        "machines": [
            {
                "name": m.name,
                "kind": m.kind,
                "manifests_dir": m.name,  # relative to out_dir
            }
            for m in rig.machines
        ],
    }
    idx_path = out_dir / "index.yaml"
    idx_path.write_text(_dump(index))
    written.append(idx_path)

    return written
