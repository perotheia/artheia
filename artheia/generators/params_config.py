"""Per-FC params config generator.

Emits ONE JSON file per FC, with a section per NODE keyed by the node's
*prototype* name (which is what the C++ side's `kNodeName` holds — see the
knodename-prototype design). Each section maps a node-type's `params {}` block
(declared on the node-type in the .art) to its default values.

This is the **static-deploy** half of the params/config lifecycle (see
docs/tasks/TODO/services-db-state-gatekeeper.md):

  params  — static deployment knobs. Generated to this per-FC JSON, read ONCE
            at process boot by the runtime config singleton; a change requires
            a restart. NOT etcd, NOT services/per.

Output shape (one file per FC, staged at /ROOT/<machine>/config/<fc>.json):

    {
      "package": "system.services.per",
      "nodes": {
        "per_client":  { "push_connect_ms": 250, "etcd_endpoint": "127.0.0.1:2379" },
        "per_manager": { ... }
      }
    }

Keyed by PROTOTYPE name (per_client), not node-type (PerClient), because the
runtime looks the section up by kNodeName, which is the prototype/instance name.
A node-type with no params contributes no section. The runtime reader
(get_config().node(kNodeName)) returns an empty view for an absent section, so a
node always gets its .art defaults via the typed getters' fallbacks.
"""
from __future__ import annotations

import json
from pathlib import Path

# Reuse the param-default coercion from the etcd schema generator so JSON
# values match the .art declared types (uint->int, bool, string, float).
from .etcd_schema import _coerce_default


def _compositions(model):
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl":
            yield el


def build_params(model) -> dict:
    """Walk every composition's prototypes, emit {prototype_name: {param: val}}
    for each node-type that declares a params block. Prototypes of a param-less
    node-type are omitted (no section)."""
    from artheia.model import flatten_composition

    nodes: dict[str, dict] = {}
    for comp in _compositions(model):
        proto_decls, _connects = flatten_composition(comp)
        for proto in proto_decls:
            node_type = proto.type
            params = getattr(node_type, "params", None) or []
            if not params:
                continue
            nodes[proto.name] = {
                p.name: _coerce_default(p) for p in params
            }
    return {"package": model.name or "", "nodes": nodes}


def generate_params_config(model, out_file: str | Path) -> Path:
    """Emit the per-FC params JSON at `out_file`
    (convention: <ROOT>/<machine>/config/<fc>.json)."""
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(build_params(model), indent=2, sort_keys=False) + "\n")
    return out_file
