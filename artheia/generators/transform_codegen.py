"""gen-transform — emit a migration-plugin .cc (+ a custom sidecar stub) from a
transform.json rule-set.

RUNTIME model: the plugin runs in svc/per (MigrateBulk) and must be FAST — so it
works directly on the nanopb STRUCTS, NO JSON, NO libprotobuf. JSON lives only in
tools/migrate/migrate.py (the architect's design/test bench). The generated
transform:

  1. nanopb-decodes the FROM config struct from the input bytes,
  2. builds the TO struct: default-CARRY every field, then apply the rules as
     direct struct-member assignments (to.X = from.Y, set, etc.),
  3. for an {op:"custom", fn:"name"} rule, call an EXTERN function the user
     implements in a write-once sidecar (`<name>_custom.cc`): the codegen emits
     `extern "C" void name(const FromCfg*, ToCfg*);` and a stub body once,
  4. nanopb-encodes the TO struct to the output bytes.

gen-transform needs the field shapes (member names + C types) of the from/to
configs, so it takes a --schema (gen-schema output). config_type's entry gives
the proto_type (nanopb struct name) + ordered fields.

transform.json: see tools/migrate/migrate.py. Two layers must agree on the rule
SEMANTICS (migrate.py = JSON reference; this = nanopb runtime) — keep them in
lockstep.

Outputs:
  <out>.cc            the plugin (regenerated freely)
  <out>_custom.cc     custom-hook stubs (WRITE-ONCE — user owns it)
"""
from __future__ import annotations

import json
from pathlib import Path


# .art scalar type -> (C type, copy-style). copy-style: "scalar" (=) | "string"
# (char[] -> strncpy) | "bytes" (sized struct -> memcpy).
_C_SCALAR = {
    "int32": "int32_t", "int64": "int64_t",
    "uint32": "uint32_t", "uint64": "uint64_t",
    "float": "float", "double": "double",
    "bool": "bool",
}


def _field_kind(f: dict) -> str:
    t = f["type"]
    if t in _C_SCALAR:
        return "scalar"
    if t == "string":
        return "string"
    if t == "bytes":
        return "bytes"
    return "message"  # nested / enum ref — not handled by struct-copy (custom)


def _carry(member: str, kind: str) -> str:
    """Default carry: to.<member> = from.<member> (type-correct)."""
    if kind == "scalar":
        return f"    to.{member} = from.{member};"
    if kind == "string":
        return (f'    std::strncpy(to.{member}, from.{member}, '
                f"sizeof(to.{member}) - 1);")
    if kind == "bytes":
        return f"    to.{member} = from.{member};"  # sized struct: value copy
    return f"    // carry {member}: nested/enum — use a custom hook"


def _assign_scalar(member: str, value) -> str:
    if isinstance(value, bool):
        return f"    to.{member} = {str(value).lower()};"
    return f"    to.{member} = {value};"


def _emit_rules(rules, fields_by_name, customs) -> list[str]:
    """Emit the rule ops as struct-member statements. `customs` collects
    (fn_name) of every {op:custom} for the extern decls + sidecar."""
    out = []
    def kind_of(name):
        f = fields_by_name.get(name)
        return _field_kind(f) if f else "scalar"
    def member(path):
        # JSONPath/dotted -> a flat member name. Nested ($.a.b) isn't a struct
        # member here (struct-copy is flat); such a rule should be {op:custom}.
        p = path[2:] if path.startswith("$.") else path
        return p  # dotted stays as-is; if it has a '.', it won't compile -> custom

    for r in rules:
        op = r.get("op")
        if op == "rename":
            s, d = member(r["from"]), member(r["to"])
            # The plugin decodes BOTH from/to with the SAME (to-version) nanopb
            # struct — migration relies on proto FIELD-NUMBER stability. So if a
            # rename keeps the field number (the common case: just a name change),
            # the old bytes already decoded into the NEW member via default carry,
            # and there is no `from.<old>` member to read. Detect that: when the
            # source name is NOT a struct field but the destination IS, the value
            # is already carried — emit a no-op note instead of `to.d = from.s`
            # (which wouldn't compile). A true field-NUMBER change is a `copy`
            # between two members that both exist, or a {op:custom}.
            if s not in fields_by_name and d in fields_by_name:
                out.append(f"    // rename {s}->{d}: same field number, value "
                           f"already carried (from.{d}).")
            else:
                out.append(_field_copy(d, s, kind_of(s)))
        elif op == "copy":
            s, d = member(r["from"]), member(r["to"])
            out.append(_field_copy(d, s, kind_of(s)))
        elif op == "set":
            m = member(r.get("field") or r.get("path"))
            out.append(_set_value(m, r["value"], kind_of(m)))
        elif op == "add":
            # add = set when the FROM lacks it; on a carry-by-default struct the
            # field already exists, so `add` is a conditional default. For a flat
            # struct we treat it as: if from didn't set it, use default. nanopb
            # has_<field> only exists for optional/message; for proto3 scalars we
            # just set the default (idempotent-ish). Keep it as a plain set.
            m = member(r.get("field") or r.get("path"))
            out.append("    // add (default-if-absent; proto3 scalar -> set):")
            out.append(_set_value(m, r.get("default"), kind_of(m)))
        elif op == "remove":
            m = member(r.get("field") or r.get("path"))
            out.append(f"    to.{m} = {{}};   // remove -> zero/clear")
        elif op == "transform":
            m = member(r.get("path") or r.get("field"))
            out.append(_emit_value_map(m, r))
        elif op == "custom":
            fn = r["fn"]
            customs.append(fn)
            out.append(f"    {fn}(&from, &to);   // user sidecar")
        else:
            raise ValueError(f"unknown rule op: {op!r}")
    return out


def _field_copy(dst: str, src: str, kind: str) -> str:
    if kind == "string":
        return (f'    std::strncpy(to.{dst}, from.{src}, '
                f"sizeof(to.{dst}) - 1);")
    return f"    to.{dst} = from.{src};"


def _set_value(member: str, value, kind: str) -> str:
    if kind == "string":
        v = json.dumps(value if isinstance(value, str) else str(value))
        return (f'    std::strncpy(to.{member}, {v}, '
                f"sizeof(to.{member}) - 1);")
    return _assign_scalar(member, value)


def _emit_value_map(member: str, rule: dict) -> str:
    """Value/enum remap on a scalar member: an if-chain over the mapping."""
    mapping = rule.get("map") or rule.get("expression") or {}
    lines = ["    {  // transform (value map) " + member]
    first = True
    for k, v in mapping.items():
        cond = "if" if first else "else if"
        first = False
        # numeric or string compare on from.<member>
        try:
            kc = str(int(k))
            cmp = f"from.{member} == {kc}"
        except (TypeError, ValueError):
            cmp = f'from.{member} == {json.dumps(k)}'
        if isinstance(v, bool):
            vc = str(v).lower()
        elif isinstance(v, (int, float)):
            vc = str(v)
        else:
            vc = json.dumps(v)
        lines.append(f"        {cond} ({cmp}) to.{member} = {vc};")
    if "default" in rule:
        d = rule["default"]
        dv = str(d).lower() if isinstance(d, bool) else (
            str(d) if isinstance(d, (int, float)) else json.dumps(d))
        lines.append(f"        else to.{member} = {dv};")
    lines.append("    }")
    return "\n".join(lines)


def _schema_entry(schema: dict, config_type: str) -> dict:
    e = (schema or {}).get("configs", {}).get(config_type)
    if e is None:
        raise KeyError(f"config_type {config_type!r} not in schema")
    return e


def generate_transform_plugin(transform: dict, out_file, schema: dict,
                              src: str = "") -> Path:
    """Emit the plugin .cc (+ a custom sidecar stub) from a transform dict +
    a gen-schema schema (for the from/to struct field shapes)."""
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    ct = transform["config_type"]
    frm = transform.get("from_digest", "")
    to = transform.get("to_digest", "")
    rules = transform.get("rules", [])

    # Both from + to share the config_type's CURRENT shape in the schema (the
    # .art is post-migration); the from-struct is the same nanopb type. (A true
    # type-rename across protos is a custom hook.) proto_type = nanopb struct.
    entry = _schema_entry(schema, ct)
    pb = entry["proto_type"]
    fields = entry["fields"]
    fields_by_name = {f["name"]: f for f in fields}

    customs: list[str] = []
    carry = [_carry(f["name"], _field_kind(f)) for f in fields]
    rule_lines = _emit_rules(rules, fields_by_name, customs)

    # nanopb message-desc symbol: <proto_type>_fields / <proto_type>_msg.
    extern_decls = "\n".join(
        f"extern \"C\" void {fn}(const {pb}* in, {pb}* out);" for fn in customs)

    body = f'''// AUTO-GENERATED by `artheia gen-transform` — DO NOT EDIT.
// source transform: {src or "(inline)"}
//
// Migration plugin: reshapes {ct} from digest {frm} to {to}. dlopen'd by per
// (MigrateBulk). Works on the nanopb STRUCT — no JSON, no libprotobuf. Mirrors
// the field ops in tools/migrate/migrate.py (the JSON design/test bench).

#include "impl/migration_plugin_api.h"
#include "{pb_header(pb, entry)}"

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstdlib>
#include <cstring>

// Custom hooks (rules the declarative codegen can't express) — implemented by
// the user in the write-once sidecar {out_file.stem}_custom.cc.
{extern_decls if customs else "// (no custom hooks)"}

extern "C" int transform_{ct}(const char* in, size_t in_len,
                             char** out, size_t* out_len) {{
    {pb} from = {pb}_init_zero;
    {pb} to   = {pb}_init_zero;

    pb_istream_t is = pb_istream_from_buffer(
        reinterpret_cast<const pb_byte_t*>(in), in_len);
    if (!pb_decode(&is, {pb}_fields, &from)) return 1;

    // Default carry: every field keeps its value unless a rule overrides it.
{chr(10).join(carry)}

    // ---- rules (same semantics as tools/migrate/migrate.py) ----
{chr(10).join(rule_lines) if rule_lines else "    // (no rules)"}

    // Encode the TO struct.
    pb_ostream_t os = pb_ostream_from_buffer(
        reinterpret_cast<pb_byte_t*>(*out = static_cast<char*>(std::malloc(4096))),
        4096);
    if (!*out) return 1;
    if (!pb_encode(&os, {pb}_fields, &to)) {{ std::free(*out); return 1; }}
    *out_len = os.bytes_written;
    return 0;
}}

extern "C" void per_register_migrations(const per_migration_api* api) {{
    if (!api || api->abi_version != PER_MIGRATION_ABI_VERSION) return;
    api->add_edge(api->host, "{frm}", "{to}", &transform_{ct});
}}
'''
    out_file.write_text(body)

    # Write-once custom sidecar stub (never clobber a user's implementation).
    if customs:
        side = out_file.with_name(out_file.stem + "_custom.cc")
        if not side.exists():
            stub = f'''// User-owned migration custom hooks for {ct} ({frm} -> {to}).
// WRITE-ONCE: `artheia gen-transform` creates this stub once and never
// overwrites it. Implement each hook on the typed nanopb structs.

#include "{pb_header(pb, entry)}"

'''
            for fn in dict.fromkeys(customs):
                stub += (f'extern "C" void {fn}(const {pb}* in, {pb}* out) {{\n'
                         f"    (void)in; (void)out;\n"
                         f"    // TODO: implement the {fn} reshape.\n"
                         f"}}\n\n")
            side.write_text(stub)
    return out_file


def pb_header(pb: str, entry: dict) -> str:
    """Include path for the config's nanopb header. The flat proto_type maps to
    the proto subdir of its defining package (e.g. system.app ->
    system/app/app.pb.h)."""
    from artheia.generators.proto import package_subdir
    pkg = entry.get("art_package", "")
    sub = package_subdir(pkg).as_posix() if pkg else ""
    leaf = pkg.split(".")[-1] if pkg else "config"
    return f"{sub}/{leaf}.pb.h" if sub else f"{leaf}.pb.h"


def generate_transform_from_file(transform_path, out_file,
                                 schema_path) -> Path:
    transform = json.loads(Path(transform_path).read_text())
    schema = json.loads(Path(schema_path).read_text())
    return generate_transform_plugin(transform, out_file, schema,
                                     src=str(transform_path))
