"""Emit the per-machine deploy manifest set.

For each :class:`Machine` in the rig we write four JSON manifests
(the four AUTOSAR manifest kinds, AA-aligned filenames):

  dist/manifest/<machine>/machine.json      ← per-ECU config + OS deps
  dist/manifest/<machine>/application.json  ← AAs hosted on this ECU
  dist/manifest/<machine>/service.json      ← service instances here
  dist/manifest/<machine>/execution.json    ← Processes + startup conf

Plus a top-level ``index.json`` so Puppet's bootstrap can find each
machine's directory by hostname.

**JSON is the single canonical format.** YAML was the original
encoding; we dropped it across the system because:

- C++ consumers (platform/supervisor, supervisor-gui) parse JSON
  with a header-only library (nlohmann/json) — no link-time
  yaml-cpp dep.
- Programmatic tooling (Robot, schema validators, downstream
  codegen) gets typed parse natively.
- One format means one schema source of truth, no encoder drift to
  test against.

Human readability is fine — ``json.dumps(indent=2)`` is diff-clean
and renders well in editors.

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
import json
from enum import Enum
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any


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


def _dump_json(obj: Any) -> str:
    # indent=2 keeps it diff-friendly + readable. sort_keys=False so
    # the column order matches dataclass-field order (top-level kind
    # first, nested dicts as declared).
    return json.dumps(obj, indent=2, sort_keys=False) + "\n"


def _write_json(path_no_ext: Path, payload: dict) -> Path:
    """Write <path>.json and return it."""
    jp = path_no_ext.with_suffix(".json")
    jp.write_text(_dump_json(payload))
    return jp


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
    """Service instances pinned to *machine_name*.

    **Strict filter:** an instance ships in this machine's
    ``service.yaml`` if and only if its ``remote_machine == machine_name``.
    Empty ``remote_machine`` means "not pinned" — those instances are
    dropped entirely (operator must pin them to surface them anywhere).
    See ``docs/tasks/DONE/04-service-instance-remote-machine.md`` for
    the migration that switched the filter from loose to strict.

    Rationale: the loose fallback (include-everywhere when no pin)
    silently spreads compute-only services like ``shwa`` to every
    machine's service.yaml in a multi-machine rig. Strict mode forces
    the rig author to be explicit; the Phase 0 audit will catch
    omissions.
    """
    payload_services = []
    for sm in rig.service_manifests:
        local_instances = [
            i for i in sm.instances
            if getattr(i, "remote_machine", "") == machine_name
        ]
        if not local_instances:
            continue
        copy = dataclasses.replace(sm, instances=local_instances)
        payload_services.append(_serialize(copy))
    return {
        "kind": "ServiceManifest",
        "host_machine": machine_name,
        "service_manifests": payload_services,
    }


def _execution_payload(rig, machine_name: str) -> dict:
    """Processes + supervisor sub-tree for *machine_name*.

    Three pieces in the payload:

    - ``supervisor_tree``: result of
      :func:`build_supervisor_tree(rig, machine=machine_name)` — the
      OTP-style supervision tree sliced to this machine. Empty
      ``children`` list = this machine runs no supervised processes
      (e.g. a HOST/admin machine).
    - ``processes``: the Process entries whose names appear as leaves
      in the sliced supervisor tree. Walked from the sliced tree so
      they stay in lockstep — no risk of emitting a Process whose
      supervisor declaration didn't make the slice.
    - ``process_to_machine_mappings`` / ``node_to_cpu_mappings``:
      the affinity entries for this machine (PTM that names
      ``machine_name``, NTM with no machine pin or matching).

    Falls back gracefully:

    - If the rig has no supervisors declared, the tree is omitted
      entirely and every Process is emitted (single-machine rig that
      hasn't migrated to the supervisor DSL yet).
    """
    from artheia.manifest.supervisor import build_supervisor_tree

    # Build the sliced supervisor tree, if the rig has any.
    if getattr(rig, "supervisors", []):
        sliced = build_supervisor_tree(rig, machine=machine_name)
        # Walk the sliced tree to collect surviving Process names.
        surviving_names: set[str] = set()

        def _walk(n) -> None:
            if hasattr(n, "children"):
                # Sub-supervisor: descend.
                for c in n.children:
                    _walk(c)
            else:
                # Leaf ChildSpec.
                surviving_names.add(n.name)

        _walk(sliced)
        procs = [p for p in rig.execution_manifests if p.name in surviving_names]
        supervisor_tree_payload = _supervisor_spec_to_dict(sliced)
    else:
        procs = list(rig.execution_manifests)
        supervisor_tree_payload = None

    ptm_for_machine = [
        m for m in rig.process_to_machine_mappings
        if getattr(m, "machine", "") == machine_name
    ]

    # This machine's supervisor TIPC instance (ARA Executor identity). rig.
    # supervisor maps machine name → Supervisor; default 0 if unset. run-
    # supervisor.sh reads this from execution.json → THEIA_SUPERVISOR_INSTANCE.
    sup = getattr(rig, "supervisor", {}).get(machine_name)
    sup_instance = getattr(sup, "instance", 0) if sup is not None else 0

    # This machine's logger SINK policy — the MACHINE-level THEIA_LOGGER before
    # per-node expansion (e.g. "file:/var/log/theia" or "syslog"). The per-process
    # THEIA_LOGGER in the supervisor tree expands this to <dir>/<node>.log, but
    # the log[logging] hose needs the un-expanded policy to know WHAT to tail (a
    # directory of *.log, or journald) — it can't reverse a single node's path.
    # run-supervisor.sh exports this as THEIA_LOGGER_POLICY; GetLoggerPolicy
    # serves it back to log[]. Precedence mirrors build_supervisor_tree:
    # Machine.logger > rig.logger > file:/tmp/theia.
    logger_policy = ""
    for m in getattr(rig, "machines", []) or []:
        if getattr(m, "name", "") == machine_name:
            logger_policy = (getattr(m, "logger", "") or "").strip()
            break
    if not logger_policy:
        logger_policy = (getattr(rig, "logger", "") or "").strip()
    if not logger_policy:
        logger_policy = "file:/tmp/theia"

    payload = {
        "kind": "ExecutionManifest",
        "host_machine": machine_name,
        "supervisor_instance": sup_instance,
        "logger_policy": logger_policy,
        "processes": [_serialize(p) for p in procs],
        "process_to_machine_mappings": [
            _serialize(m) for m in ptm_for_machine
        ],
        "node_to_cpu_mappings": [
            _serialize(m) for m in rig.node_to_cpu_mappings
            if getattr(m, "machine", "") in ("", machine_name)
        ],
    }
    if supervisor_tree_payload is not None:
        payload["supervisor_tree"] = supervisor_tree_payload
    return payload


def _supervisor_spec_to_dict(node) -> dict:
    """Render a :class:`SupervisorSpec` / :class:`ChildSpec` tree as
    nested dicts ready for YAML. Mirrors the logic in
    ``artheia executor emit`` so the in-execution-yaml shape matches.
    """
    d: dict = {"name": node.name}
    if hasattr(node, "children"):
        d["strategy"] = node.strategy.value
        d["max_restarts"] = node.max_restarts
        d["max_seconds"] = node.max_seconds
        if getattr(node, "tombstone_dir", ""):
            d["tombstone_dir"] = node.tombstone_dir
        d["children"] = [_supervisor_spec_to_dict(c) for c in node.children]
    else:
        d["start_cmd"] = list(node.start_cmd)
        d["restart"] = node.restart.value
        d["shutdown"] = node.shutdown
        d["type"] = node.type.value
        if node.modules:
            d["modules"] = list(node.modules)
        if getattr(node, "env", None):
            d["env"] = dict(node.env)
        if getattr(node, "working_dir", ""):
            d["working_dir"] = node.working_dir
        if getattr(node, "shall_run_on", None):
            d["shall_run_on"] = list(node.shall_run_on)
        if getattr(node, "shall_not_run_on", None):
            d["shall_not_run_on"] = list(node.shall_not_run_on)
        # Per-node reporting metadata (#366). Empty for non-FC
        # children (vendor apps with no .art declaration).
        nodes = getattr(node, "nodes", None) or []
        if nodes:
            d["nodes"] = [
                {
                    "name": ni.name,
                    "reporting": ni.reporting,
                    "tipc_type": ni.tipc_type,
                    "tipc_instance": ni.tipc_instance,
                }
                for ni in nodes
            ]
    return d


# ---------------------------------------------------------------------------
# Top-level entry.
# ---------------------------------------------------------------------------


def emit_dist_manifest(rig, out_dir: Path) -> list[Path]:
    """Write the per-machine manifest set rooted at *out_dir*.

    Returns the list of files written (for CLI output)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # Per-machine: 4 JSON manifests each, plus a ready-to-run executor.json.
    machine_names = [m.name for m in rig.machines]
    for machine in rig.machines:
        mdir = out_dir / machine.name
        mdir.mkdir(parents=True, exist_ok=True)
        execution = _execution_payload(rig, machine.name)
        for stem, payload in [
            ("machine",     _machine_payload(machine)),
            ("application", _application_payload(rig, machine.name)),
            ("service",     _service_payload(rig, machine.name)),
            ("execution",   execution),
        ]:
            written.append(_write_json(mdir / stem, payload))
        # executor.json — the supervisor tree extracted from execution.json,
        # ready to drop at /opt/theia/config/executor.json (puppet orchestrate
        # copies it verbatim; the supervisor reads it). Saves a JSON re-serialize
        # in puppet (no stdlib to_json needed).
        tree = execution.get("supervisor_tree")
        if tree is not None:
            written.append(_write_json(mdir / "executor", tree))

    # Top-level machines manifest — Puppet's bootstrap finds the per-host dir
    # here. (Also the GUI's machine index; the GUI is the sole consumer today.)
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
    written.append(_write_json(out_dir / "machines", index))

    return written
