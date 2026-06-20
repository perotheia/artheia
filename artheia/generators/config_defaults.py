"""Config-defaults generator (gen-config-defaults).

The first-boot half of the config side, parallel to gen-params for the static
side. A node's `config <Msg>` is etcd-backed (services/per) and proto3 has NO
field defaults — a node with no stored value would otherwise boot at zeros. This
emits the DECLARED defaults (the `.art` `field = value`, same syntax as a node
`params` entry) so a seeder can PutConfig them on first boot.

Output shape (keyed by PROTOTYPE name == the per store key / kNodeName):

    {
      "package": "system.app",
      "configs": {
        "my_node": {
          "config_type": "MyConfig",
          "digest": "cfg_…",                 # the v* digest to tag the seed with
          "values": { "step": 1, "max_value": 100, "wrap": false,
                      "label": "my_node", "hysteresis": 3 }
        }
      }
    }

Only fields with a declared default appear in `values`; an all-undeclared config
contributes an empty `values` (the node still boots at proto3 zeros for those —
declaring a default is opt-in, exactly like params). A node-type with no config
binding contributes no section. Reuses gen-schema's field-shape walk so the
defaults + digest are computed identically (single source of truth).
"""
from __future__ import annotations

import json
from pathlib import Path

from .config_schema import _field_shape, _digest, _compositions


def build_config_defaults(model) -> dict:
    """Walk prototypes → node-types with a `config` binding → emit
    {prototype: {config_type, digest, values}} from the declared field
    defaults."""
    from artheia.model import flatten_composition

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
            values = {f["name"]: f["default"]
                      for f in fields if "default" in f}
            # Defining package + flat proto symbol of the config message (mirrors
            # gen-schema's art_package/proto_type). Lets a seeder resolve the
            # proto class DYNAMICALLY via the probe codec
            # (codec.encode(art_package, proto_type, **values)) for ANY FC,
            # instead of a hardcoded app-specific class map. The config message may
            # be defined in a different package than the node.
            from artheia.generators.proto import _proto_package_name
            try:
                from textx import get_model
                art_pkg = get_model(cfg).name or (model.name or "")
            except Exception:
                art_pkg = model.name or ""
            flat = _proto_package_name(art_pkg).replace(".", "_") + "_" + cfg_name
            configs[proto.name] = {
                "config_type": cfg_name,
                "art_package": art_pkg,
                "proto_type": flat,
                "digest": _digest(cfg_name, fields),
                "values": values,
            }
    return {"package": model.name or "", "configs": configs}


def generate_config_defaults(model, out_file: str | Path) -> Path:
    """Emit the config-defaults JSON at `out_file`."""
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(build_config_defaults(model), indent=2, sort_keys=False)
        + "\n")
    return out_file
