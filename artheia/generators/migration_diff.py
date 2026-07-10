"""gen-migration — diff two config-schemas and scaffold per-node transforms.

The migration tooling's missing link: turning a *schema bump* into the
per-node ``transform.json`` files (and the BUILD plugin entries) that the rest
of the chain consumes. Given ``gen-schema`` output for the OLD and NEW shapes,
this:

  * compares each ``config_type`` present in BOTH whose shape DIGEST changed,
  * emits one ``<node>_v1_to_v2.json`` per such config — pre-filled with the
    auto-derivable rules + the from/to digests,
  * (optionally) regenerates the migration ``BUILD.bazel`` plugin entries.

Proto tags are POSITIONAL (field order == tag 1..N), so we diff by INDEX:

  * same index, SAME name, SAME type      -> carried (no rule needed).
  * same index, DIFFERENT name            -> ``rename`` (wire-compatible: the
                                             old bytes already decode into the
                                             new member). The common, safe case.
  * same index, same name, DIFF type      -> a TYPE CHANGE: not expressible as a
                                             flat carry; emit a ``custom`` hook
                                             stub + a TODO (the human writes the
                                             reshape on the typed structs).
  * index only in NEW (appended field)    -> ``add`` with a type default.
  * index only in OLD (truncated tail)    -> ``remove``.

Renames + type changes are HEURISTICS — every emitted transform carries a
``"_review"`` note listing what was guessed, so the architect confirms intent
before ``gen-transform`` turns it into code. A config whose digest is unchanged
emits nothing (no migration). A config only in NEW is a fresh binding (no stored
old value) and is skipped; one only in OLD means the node/config was dropped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def _default_for(ftype: str, repeated: bool) -> Any:
    """A neutral default for an ADDED field, by .art scalar type."""
    if repeated:
        return []
    if ftype in ("bool",):
        return False
    if ftype in ("string",):
        return ""
    if ftype in ("bytes",):
        return ""
    if ftype in ("float", "double"):
        return 0.0
    # all int kinds (uint32/int32/sint32/uint64/.../enum-as-int)
    return 0


def _diff_fields(old_fields: list[dict], new_fields: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (rules, review_notes) from a positional field diff."""
    rules: list[dict] = []
    notes: list[str] = []
    n = max(len(old_fields), len(new_fields))
    for i in range(n):
        o = old_fields[i] if i < len(old_fields) else None
        v = new_fields[i] if i < len(new_fields) else None
        tag = i + 1
        if o and v:
            if o["name"] == v["name"]:
                if o.get("type") != v.get("type") or \
                        o.get("repeated") != v.get("repeated"):
                    # type/cardinality change at the same tag — not a flat carry.
                    rules.append({"op": "custom",
                                  "fn": f"fixup_{v['name']}",
                                  "_note": f"tag {tag} '{v['name']}' type "
                                           f"{o.get('type')}->{v.get('type')} — "
                                           f"implement the reshape in the "
                                           f"_custom.cc sidecar"})
                    notes.append(f"CONFIRM type change at tag {tag} "
                                 f"'{v['name']}' ({o.get('type')}->"
                                 f"{v.get('type')}): wrote a custom hook stub.")
                # else: identical field — carried by default, no rule.
            else:
                # same tag, different name -> rename (carry-preserving).
                rules.append({"op": "rename",
                              "from": o["name"], "to": v["name"]})
                notes.append(f"CONFIRM rename at tag {tag}: "
                             f"'{o['name']}' -> '{v['name']}' (assumed same "
                             f"field renamed; if it's an unrelated "
                             f"remove+add, split into two rules).")
        elif v and not o:
            # appended field in NEW. Prefer the field's DECLARED default (from
            # the .art `= value`, carried by gen-schema) — declare the value
            # ONCE and both the migration add-rule + first-boot seed use it.
            # Fall back to a neutral zero/""/false when none is declared.
            dflt = v["default"] if "default" in v else \
                _default_for(v.get("type", ""), v.get("repeated", False))
            rules.append({"op": "add", "field": v["name"], "default": dflt})
        elif o and not v:
            # truncated tail in NEW.
            rules.append({"op": "remove", "field": o["name"]})
    return rules, notes


def diff_schemas(from_schema: dict, to_schema: dict) -> dict[str, dict]:
    """Return {config_type -> transform_dict} for every config whose digest
    changed between the two schemas. Skips unchanged, new-only, and dropped
    configs."""
    out: dict[str, dict] = {}
    fc = from_schema.get("configs", {})
    tc = to_schema.get("configs", {})
    for ct, tinfo in tc.items():
        finfo = fc.get(ct)
        if finfo is None:
            continue  # fresh binding — no stored old value to migrate
        if finfo.get("digest") == tinfo.get("digest"):
            continue  # unchanged shape — no migration
        rules, notes = _diff_fields(finfo.get("fields", []),
                                    tinfo.get("fields", []))
        transform: dict[str, Any] = {
            "config_type": ct,
            "from_digest": finfo.get("digest"),
            "to_digest": tinfo.get("digest"),
            "rules": rules,
        }
        if notes:
            transform["_review"] = notes
        out[ct] = transform
    return out


def _node_key(to_schema: dict, config_type: str) -> str:
    """The first bound node prototype for a config_type (the plugin/file key)."""
    info = to_schema["configs"].get(config_type, {})
    nodes = info.get("nodes") or []
    return nodes[0] if nodes else config_type.lower()


def generate_migrations(from_schema_path: str, to_schema_path: str,
                        out_dir: str,
                        emit_build: bool = True) -> dict[str, str]:
    """Diff the two schema files and write one <node>_v1_to_v2.json per changed
    config into out_dir. Optionally (re)write out_dir/BUILD.bazel's plugin
    entries. Returns {config_type -> transform_json_path}."""
    from_schema = json.loads(Path(from_schema_path).read_text())
    to_schema = json.loads(Path(to_schema_path).read_text())
    transforms = diff_schemas(from_schema, to_schema)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    nodes: list[str] = []
    for ct, t in transforms.items():
        node = _node_key(to_schema, ct)
        nodes.append(node)
        p = out / f"{node}_v1_to_v2.json"
        # WRITE-ONCE: a transform is a REVIEWED artifact (the scaffold's
        # heuristic rules carry _review notes the author resolves). A re-run
        # (e.g. `theia release-swp` re-checking the digest gate) must never
        # clobber the reviewed rules — delete the file to re-scaffold.
        if p.exists():
            written[ct] = str(p)
            continue
        p.write_text(json.dumps(t, indent=2) + "\n")
        written[ct] = str(p)

    if emit_build and nodes:
        _rewrite_build(out, sorted(set(nodes)), to_schema, list(transforms))
    return written


# The managed region in migration/BUILD.bazel between these markers holds the
# per-node migration_plugin() entries. gen-migration MERGES its diff's nodes
# into whatever's already there (so a v2->v3 run doesn't drop the v1->v2 nodes)
# — anything outside the markers (the cc_library + macro load) is preserved.
_BEGIN = "# >>> gen-migration plugins (managed) >>>"
_END = "# <<< gen-migration plugins (managed) <<<"


def _parse_managed_nodes(block: str) -> list[str]:
    """Extract the node names from existing migration_plugin() lines."""
    import re
    return re.findall(r'migration_plugin\(name\s*=\s*"([^"]+)"', block)


def _render_block(nodes: list[str]) -> str:
    lines = [_BEGIN]
    for n in nodes:
        lines.append(f'migration_plugin(name = "{n}", '
                     f'src = "{n}_v1_to_v2.cc")')
    lines.append(_END)
    return "\n".join(lines) + "\n"


def _rewrite_build(out: Path, nodes: list[str],
                   to_schema: dict | None = None,
                   config_types: list | None = None) -> None:
    build = out / "BUILD.bazel"
    if build.exists():
        text = build.read_text()
        if _BEGIN in text and _END in text:
            existing = _parse_managed_nodes(
                text[text.index(_BEGIN):text.index(_END)])
            merged = sorted(set(existing) | set(nodes))
            pre = text[: text.index(_BEGIN)]
            post = text[text.index(_END) + len(_END):].lstrip("\n")
            build.write_text(pre + _render_block(merged) + post)
            return
        # markers absent — append the managed block.
        build.write_text(text.rstrip() + "\n\n" + _render_block(sorted(set(nodes))))
        return
    block_text = _render_block(sorted(set(nodes)))
    # no BUILD yet — scaffold one. The preamble (hand-owned after this first
    # write) defines the per-package :pb_hdr/:pb_c defaults the macro's managed
    # entries lean on; derived from the FIRST diff'd config's art_package (a
    # multi-package migrations dir edits the preamble by hand).
    pkg = ""
    if to_schema and config_types:
        pkg = (to_schema.get("configs", {})
                        .get(config_types[0], {})
                        .get("art_package", "")) or ""
    leaf = pkg.split(".")[-1] if pkg else "apps"
    proto_pkg = "//proto/" + pkg.replace(".", "/") if pkg else "//proto/system/apps"
    build.write_text(
        "# Scaffolded ONCE by `artheia gen-migration` — preamble is HAND-OWNED\n"
        "# (edit :pb_hdr/:pb_c if your proto tree differs); the block between\n"
        "# the markers is MANAGED (gen-migration merges plugin entries).\n"
        "# Each plugin is a self-contained .so per dlopen's at MigrateBulk time\n"
        "# (see @pero_theia//rules:migration.bzl).\n"
        'load("@rules_cc//cc:defs.bzl", "cc_library")\n'
        'load("@pero_theia//rules:migration.bzl", "migration_plugin")\n\n'
        'package(default_visibility = ["//visibility:public"])\n\n'
        f'# {leaf}.pb.h include path WITHOUT a runtime shared-lib dependency.\n'
        'cc_library(\n'
        '    name = "pb_hdr",\n'
        f'    hdrs = ["{proto_pkg}:{leaf}_pb_h"],\n'
        '    strip_include_prefix = "/proto",\n'
        ')\n\n'
        'alias(\n'
        '    name = "pb_c",\n'
        f'    actual = "{proto_pkg}:{leaf}_pb_c",\n'
        ')\n\n'
        + block_text)
