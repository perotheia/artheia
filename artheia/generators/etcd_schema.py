"""etcd schema generator.

Walks all nodes in a model, collects their `params` blocks, and emits a single
JSON document keyed by etcd path. System engineers edit this document at
install time to set per-deployment defaults; at runtime each node subscribes
to its own key prefix and gets a callback on change (ROS2-style).

Key layout: /nodes/<NodeName>/params/<param_name>

Each leaf contains:
  - type:     the declared Artheia type ("uint32", "bool", ...)
  - default:  the parsed default value, JSON-native
  - python_type: a hint for runtime decoders ("int" | "float" | "bool" | "str")

The wire encoding for etcd values is up to the runtime: storing as JSON is the
default expectation, but the runtime is free to pack into protobuf or anything
else, since the schema is self-describing.
"""
from __future__ import annotations

import json
from pathlib import Path


_PY_TYPE = {
    "int32":  "int",
    "int64":  "int",
    "uint32": "int",
    "uint64": "int",
    "float":  "float",
    "double": "float",
    "bool":   "bool",
    "string": "str",
}


def _iter_nodes(model):
    for el in model.elements:
        if el.__class__.__name__ == "NodeDecl":
            yield el


def _coerce_default(param):
    v = param.default.value
    if isinstance(v, str) and v in ("true", "false"):
        return v == "true"
    if param.type in ("int32", "int64", "uint32", "uint64"):
        return int(v)
    if param.type in ("float", "double"):
        return float(v)
    return v


def build_schema(model) -> dict:
    keys: dict[str, dict] = {}
    for node in _iter_nodes(model):
        for p in getattr(node, "params", []) or []:
            key = f"/nodes/{node.name}/params/{p.name}"
            keys[key] = {
                "type": p.type,
                "python_type": _PY_TYPE[p.type],
                "default": _coerce_default(p),
            }
    return {"package": model.name or "", "keys": keys}


def generate_etcd_schema(model, out_file: str | Path) -> Path:
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(build_schema(model), indent=2, sort_keys=False) + "\n")
    return out_file
