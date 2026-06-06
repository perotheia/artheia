"""Combined config-schema generator (gen-schema).

Walks a system .art's clusters/compositions, finds every node that binds a
`config <Msg>`, resolves that message's field shape, and emits ONE schema file
describing all FC config types. This is the spine of the migration tooling:

  * tdb get-snapshot decodes each stored config value against its type here.
  * gen-transform / migrate.py validate transform rules against from/to shapes.

The DIGEST is a stable content hash of (config_type, ordered fields) — the same
digest the store tags a value with. It changes iff the shape changes, so a
mismatch between a stored value's digest and the current schema digest is the
exact trigger for a migration.

Output (JSON):

  {
    "package": "<system pkg>",
    "configs": {
      "CounterConfig": {
        "digest": "cfg_3f9a1c…",            # stable shape hash
        "proto_type": "theia_demo_CounterConfig",  # flat nanopb/proto name
        "art_package": "theia.demo",         # message's defining package
        "nodes": ["counter"],                # prototypes bound to this config
        "fields": [
          {"name": "step", "type": "uint32", "repeated": false},
          ...
        ]
      },
      ...
    }
  }
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _compositions(model):
    for el in model.elements:
        if el.__class__.__name__ in ("CompositionDecl", "ClusterDecl"):
            yield el


def _field_shape(msg) -> list[dict]:
    """Ordered REAL field list of a MessageDecl (reserved slots filtered):
    name + scalar/ref type + repeated. Body order = wire order."""
    from artheia.generators.proto import real_fields
    out = []
    for f in real_fields(msg):
        t = f.type
        # PrimitiveType carries `.kind`; a message/enum ref carries `.ref.name`.
        kind = getattr(t, "kind", None)
        if kind is None:
            ref = getattr(t, "ref", None)
            kind = getattr(ref, "name", str(t))
        out.append({
            "name": f.name,
            "type": kind,
            "repeated": bool(getattr(f, "repeated", False)),
        })
    return out


def _digest(config_type: str, fields: list[dict]) -> str:
    """Stable shape hash. Order-sensitive (field order is part of the wire
    shape). Hex-truncated for readability; the full space is ample."""
    h = hashlib.sha256()
    h.update(config_type.encode())
    for fld in fields:
        h.update(b"\0")
        h.update(fld["name"].encode())
        h.update(b":")
        h.update(fld["type"].encode())
        h.update(b"[]" if fld["repeated"] else b"")
    return "cfg_" + h.hexdigest()[:16]


def build_config_schema(model) -> dict:
    """Walk prototypes → node-types with a `config` binding → the config
    message's shape. Keyed by config_type; records every bound prototype."""
    from artheia.model import flatten_composition
    from artheia.generators.proto import _proto_type_for, _proto_package_name  # noqa: F401

    configs: dict[str, dict] = {}
    for comp in _compositions(model):
        try:
            proto_decls, _connects = flatten_composition(comp)
        except Exception:
            continue
        for proto in proto_decls:
            node_type = proto.type
            cfg = getattr(node_type, "config", None)
            if cfg is None:
                continue
            cfg_name = getattr(cfg, "name", None)
            if not cfg_name:
                continue
            fields = _field_shape(cfg)
            # Defining package of the config message (may differ from the node).
            try:
                from textx import get_model
                art_pkg = get_model(cfg).name or (model.name or "")
            except Exception:
                art_pkg = model.name or ""
            flat = _proto_package_name(art_pkg).replace(".", "_") + "_" + cfg_name
            entry = configs.setdefault(cfg_name, {
                "digest": _digest(cfg_name, fields),
                "proto_type": flat,
                "art_package": art_pkg,
                "nodes": [],
                "fields": fields,
            })
            if proto.name not in entry["nodes"]:
                entry["nodes"].append(proto.name)
    return {"package": model.name or "", "configs": configs}


def generate_config_schema(model, out_file: str | Path) -> Path:
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(build_config_schema(model), indent=2, sort_keys=False) + "\n")
    return out_file
