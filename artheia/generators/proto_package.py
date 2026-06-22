"""Per-package proto3 generator: one combined .proto per .art package.

Unlike `proto.py` (which emits one .proto per message), this generator
emits a single .proto file per source package, laid out under the
output root in directories that mirror the package path:

    package app.system      → <out>/app/system/system.proto
    package gateway.system  → <out>/gateway/system/system.proto

This matches the runtime convention used by libgw / odd_path_client —
includes are `#include "<pkg-path>/<file>.pb.h"` so the .art package
hierarchy and the C++ include hierarchy stay symmetric.

Field numbering: 1..N in declaration order. Field types follow proto3
scalars verbatim (the .art grammar's PrimitiveType set is a strict
subset of proto3's). References to other messages/enums become
imports (under the same package only, for now).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from textx import metamodel_from_file

# Shared with proto.py: rewrite leading .art package segments that
# would collide with libc identifiers when protoc maps them to C++
# namespaces (see proto.py's docstring for the full rationale).
from .proto import _proto_package_name


_GRAMMAR = (Path(__file__).resolve().parent.parent /
            "grammar" / "artheia.tx")


@dataclass
class _Field:
    name: str
    number: int
    proto_type: str
    repeated: bool


@dataclass
class _Message:
    name: str
    fields: List[_Field]
    reserved: List[int] = None  # tags of deleted fields (proto3 reserved)


@dataclass
class _Enum:
    name: str
    values: list


def _ref_package(ref) -> str:
    """The .art package that DEFINES a referenced decl, via the import-
    following scope provider. "" if undeterminable (same-package)."""
    try:
        from textx import get_model
        return get_model(ref).name or ""
    except Exception:
        return ""


def _proto_type_for(field, cur_package: str):
    """Return (proto_type, import_path_or_None).

    Same-package ref → bare name, no import. CROSS-package ref → the type is
    qualified with the other package's FLAT proto name
    (platform_runtime.TraceControlPush) and import_path points at that package's
    BUNDLED proto file (<pkg-subdir>/<leaf>.proto), so protoc resolves it.
    """
    t = field.type
    ref = getattr(t, "ref", None)
    if ref is None:
        return t.kind, None
    ref_pkg = _ref_package(ref)
    if ref_pkg and ref_pkg != cur_package:
        flat = _proto_package_name(ref_pkg).replace(".", "_")
        leaf = ref_pkg.split(".")[-1]
        # The protoc import path MUST match the bundled proto's on-disk layout
        # under the proto_root AND the C++ #include the fc_app codec emits
        # (Codecs.hh: `#include "<subpath>/<leaf>.pb.h"`). fc_app uses the DOTTED
        # subpath for a normal package (platform.msgs.sensor → platform/msgs/
        # sensor) and the FLAT-package dir ONLY for platform.runtime (which ships
        # under proto/platform_runtime/). Mirror that here so import + include
        # resolve to the same file — a flat import for a dotted-laid-out package
        # would not be found by protoc / would mismatch the .pb.h include.
        if ref_pkg == "platform.runtime":
            subpath = flat                       # platform_runtime/<leaf>.proto
        else:
            subpath = "/".join(ref_pkg.split("."))   # platform/msgs/sensor/<leaf>.proto
        return f"{flat}.{ref.name}", f"{subpath}/{leaf}.proto"
    return ref.name, None


def no_arg_op_request_names(model, already_declared=frozenset()):
    """Names of the implicit request messages a model's NO-ARG clientServer
    operations need. `operation Stop() returns ControlReply` has no `in` param,
    so codegen names its request after the operation (`message Stop {}`); gen-app
    references it (register_call<Stop, ...>). The proto generators must EMIT that
    empty message or the C type (e.g. system_supervisor_Stop) is undeclared.
    Shared by both the per-package (gen-app) and per-message (gen-proto) emitters
    so the two stay consistent. Skips names already declared as real messages.
    """
    seen = set(already_declared)
    out = []
    for el in model.elements:
        # clientServer interfaces carry operations (the grammar's InterfaceDecl
        # resolves to ClientServerInterface / SenderReceiverInterface; only the
        # former has `operations`).
        if el.__class__.__name__ != "ClientServerInterface":
            continue
        for op in getattr(el, "operations", []) or []:
            has_in = any(getattr(p, "direction", "") == "in"
                         for p in getattr(op, "params", []) or [])
            if not has_in and op.name not in seen:
                out.append(op.name)
                seen.add(op.name)
    return out


def _harvest(model):
    messages: List[_Message] = []
    enums: List[_Enum] = []
    used_types: set[str] = set()
    imports: set[str] = set()
    cur_pkg = model.name or ""
    for el in model.elements:
        kind = el.__class__.__name__
        if kind == "MessageDecl":
            fields = []
            reserved: list[int] = []
            # POSITIONAL tags: each body item (field OR `reserved` marker)
            # consumes the next tag, 1..N in body order. A `reserved` slot
            # advances the tag + emits `reserved <tag>;` so deleting a field
            # (→ reserved) doesn't shift later fields' tags. See proto.py.
            for tag, item in enumerate(el.fields, start=1):
                if item.__class__.__name__ == "MessageReserved":
                    reserved.append(tag)
                    continue
                f = item
                pt, imp = _proto_type_for(f, cur_pkg)
                # used_types is for LOCAL enum pruning — only same-package bare
                # names count; a cross-package qualified type (a.b.X) is defined
                # elsewhere and pulled in by `imp`, so don't track it locally.
                if imp is None:
                    used_types.add(pt)
                else:
                    imports.add(imp)
                fields.append(_Field(
                    name=f.name,
                    number=tag,
                    proto_type=pt,
                    repeated=bool(f.repeated),
                ))
            messages.append(_Message(name=el.name, fields=fields,
                                     reserved=reserved))
        elif kind == "EnumDecl":
            enums.append(_Enum(name=el.name, values=el.values))
    # Synthesize the implicit request message for a NO-ARG clientServer
    # operation. `operation Stop() returns ControlReply` has no `in` param, so
    # codegen treats the request as `message <OpName> {}` (named after the op).
    # gen-app's _cs_ops references it (register_call<Stop, ...>); without emitting
    # it here the proto lacks `message Stop {}` and the C type
    # system_supervisor_Stop is undeclared. Add an empty message for each no-arg
    # op whose name isn't already a declared message. (Same package only — the
    # op's request lives in the iface's own package, which is this model.)
    for name in no_arg_op_request_names(model, {m.name for m in messages}):
        messages.append(_Message(name=name, fields=[]))
    # Emit ONLY enums actually used as a field type. AUTOSAR value tables
    # (the bulk of an autosar bus package) are imported as companion enums
    # but the message fields stay scalar (uint32/float) — the enum is never
    # a field type, just documentation. Emitting all of them drags in
    # thousands of proto3-conformance headaches (sibling-scoping name
    # clashes, case-insensitive collisions, keyword labels) for symbols no
    # message references. Drop the unreferenced ones.
    enums = [e for e in enums if e.name in used_types]
    return messages, enums, sorted(imports)


def _proto_enum_value(enum_name: str, value_name: str) -> str:
    """proto3 enum values use C++ sibling scoping — a value identifier
    must be unique across the WHOLE proto package, not just its enum, and
    must not collide with a proto keyword. AUTOSAR value tables freely
    reuse labels (`Neutralwert`, `Fehler`, `aus`, ...) across enums and
    even use keywords (`reserved`). Prefixing each value with its enum
    name makes it package-unique and keyword-safe in one move. The label
    is documentation only (the wire field stays scalar), so the rename is
    harmless."""
    return f"{enum_name}_{value_name}"


_DEFAULT_STR_MAX = 64    # nanopb char[] size for an unpinned string/bytes field
_DEFAULT_ARRAY_MAX = 16  # nanopb array cap for an unpinned `repeated` field

import re as _re

# Inline nanopb options we recognise in an .art field's `[...]` annotation and
# pass straight through to the .options file (besides the size/count defaults
# above). The .art is the single source of truth: declare the wire shape on the
# field and gen-app emits the matching .options line — NO hand-editing of the
# generated .options to clobber on the next regen. Maps an .art option name to
# (nanopb_name, value_kind): "int" pins a number, "verbatim" passes the token
# unchanged (e.g. type:FT_POINTER), "bool" pins true/false.
_NANOPB_PASSTHROUGH = {
    "max_count":    ("max_count", "int"),      # REPEATED field array cap (else callback)
    "fixed_count":  ("fixed_count", "bool"),   # array is always exactly max_count
    "fixed_length": ("fixed_length", "bool"),  # bytes field is fixed-size
    "int_size":     ("int_size", "verbatim"),  # IS_8/16/32/64
    "type":         ("type", "verbatim"),      # FT_STATIC / FT_POINTER / FT_CALLBACK
}


def _parse_inline_opts(opt: str) -> "dict[str, str]":
    """Parse an .art field `[k:v, k2=v2, flag]` annotation into {key: value}.
    Accepts ':' or '=' and comma/space separation. A BARE token (no value, e.g.
    ``[callback]``) becomes ``{token: ""}`` so flags are expressible too."""
    out: dict[str, str] = {}
    opt = opt or ""
    for m in _re.finditer(r"(\w+)\s*[:=]\s*([A-Za-z0-9_]+)", opt):
        out[m.group(1)] = m.group(2)
    # Bare flags: tokens not part of a k:v pair (e.g. `callback`).
    for m in _re.finditer(r"\b([A-Za-z_]\w*)\b(?!\s*[:=])", opt):
        tok = m.group(1)
        if tok not in out:
            out.setdefault(tok, "")
    return out


def _nanopb_opts_for(field) -> "list[tuple[str, str]]":
    """The nanopb .options entries for ONE message field, as (option, value)
    pairs, derived ENTIRELY from the field's type + its inline `.art` options.
    Empty list = a plain scalar that needs no .options line.

    Rules (the .art is the source of truth):
      - string/bytes  → ``max_size:N`` (inline ``[max_size:N]`` / ``[max_length:N]``
        else the package default). Without it nanopb makes the field a
        pb_callback_t — unusable by strncpy / the migration copy / the trace
        decoder.
      - repeated      → ``max_count:N`` (inline ``[max_count:N]`` else the default
        cap). A repeated field with no max_count is a pb_callback_t; the default
        keeps a regen from silently dropping to a callback.
      - any field     → any recognised inline option (fixed_count, fixed_length,
        int_size, type:FT_*) passed straight through.
    """
    t = getattr(field, "type", None)
    kind = getattr(t, "kind", None)
    repeated = bool(getattr(field, "repeated", False))
    inline = _parse_inline_opts(getattr(field, "options", "") or "")
    pairs: list[tuple[str, str]] = []

    # `[callback]` — the field is DELIBERATELY left a pb_callback_t (a variable-
    # size payload the runtime streams, e.g. log's TraceRecord bytes/strings).
    # Emit NO .options line: an unpinned string/bytes/repeated field is a callback
    # by nanopb default, so the opt-out is simply the absence of a size/count —
    # and stating it on the .art keeps the regen from re-pinning it.
    if "callback" in inline:
        return []

    # string/bytes → fixed char[] (max_size).
    if kind in ("string", "bytes"):
        n = inline.get("max_size") or inline.get("max_length")
        pairs.append(("max_size", str(int(n)) if n else str(_DEFAULT_STR_MAX)))

    # repeated → fixed array (max_count). Default unless the .art pins it below.
    if repeated and "max_count" not in inline:
        pairs.append(("max_count", str(_DEFAULT_ARRAY_MAX)))

    # explicit inline nanopb options (incl. max_count when the .art set it).
    for art_key, (nano_name, value_kind) in _NANOPB_PASSTHROUGH.items():
        if art_key not in inline:
            continue
        v = inline[art_key]
        if value_kind == "int":
            v = str(int(v))
        elif value_kind == "bool":
            v = "true" if v.lower() in ("true", "1", "yes") else "false"
        pairs.append((nano_name, v))

    return pairs


# Back-compat shim: a few callers still import _str_max_for. Keep it thin.
def _str_max_for(field) -> "int | None":
    """DEPRECATED — the max_size for a string/bytes field, else None. Superseded
    by :func:`_nanopb_opts_for` (which also handles repeated/passthrough)."""
    kind = getattr(getattr(field, "type", None), "kind", None)
    if kind not in ("string", "bytes"):
        return None
    for opt, val in _nanopb_opts_for(field):
        if opt == "max_size":
            return int(val)
    return _DEFAULT_STR_MAX


def _render_options(model, flat_package: str) -> str:
    """Emit a nanopb .options file from the .art message fields. nanopb_generator
    auto-loads the same-basename .options next to the .proto. One line per field
    constraint: ``<flat_pkg>.<Message>.<field>  <opt>:<val>`` for every
    (option, value) :func:`_nanopb_opts_for` derives — string/bytes (max_size),
    REPEATED (max_count), and any inline nanopb option. The .art is thus the
    single source of truth: a no-force regen never clobbers a hand-tuned line,
    because there are no hand-tuned lines. Header-only when nothing needs an
    entry."""
    rows: List[str] = []
    for el in model.elements:
        if el.__class__.__name__ != "MessageDecl":
            continue
        for item in getattr(el, "fields", []) or []:
            if item.__class__.__name__ == "MessageReserved":
                continue
            # One line per field carrying ALL its options (nanopb accepts a
            # field's options on one line, space-separated), so a field with
            # both max_size + max_count emits `… max_size:N max_count:M`.
            pairs = _nanopb_opts_for(item)
            if pairs:
                opts = " ".join(f"{o}:{v}" for o, v in pairs)
                rows.append(f"{flat_package}.{el.name}.{item.name}   {opts}")
    head = [
        "# AUTO-GENERATED by `artheia gen-app` — nanopb field constraints, fully",
        "# derived from the .art (the single source of truth). nanopb_generator",
        "# auto-loads this (same basename next to the .proto). A string/bytes or",
        "# repeated field without a fixed size/count becomes a pb_callback_t;",
        "# pinning it makes it a plain member (strncpy-able, trace-decodable).",
        f"# Defaults: max_size {_DEFAULT_STR_MAX}, max_count {_DEFAULT_ARRAY_MAX};",
        "# override per field with an .art `[max_size:N]` / `[max_count:N]` option",
        "# (also: fixed_count, fixed_length, int_size, type:FT_*).",
        "",
    ]
    return "\n".join(head + rows) + ("\n" if rows else "")


def _render_proto(model, package: str, source_file: str) -> str:
    messages, enums, imports = _harvest(model)
    lines: List[str] = []
    lines.append("// AUTO-GENERATED by `artheia gen-proto-package` — DO NOT EDIT")
    lines.append(f"// source: {source_file}")
    lines.append("")
    lines.append("syntax = \"proto3\";")
    # Proto package: dot-joined from the .art package. Underscores in
    # the leaf are preserved so nanopb-generated C struct names match
    # what we expect (e.g. app.system → "app_system_<Msg>").
    lines.append(f"package {package.replace('.', '_')};")
    lines.append("")
    # Cross-package imports — a field whose type is defined in an IMPORTED .art
    # package (e.g. the supervisor's TraceConfig embedding
    # platform.runtime.TraceControlPush). Path is <pkg-as-path>/<leaf>.proto,
    # resolved against the proto-root include dir (-I platform/proto).
    if imports:
        for imp in imports:
            lines.append(f"import \"{imp}\";")
        lines.append("")

    for en in enums:
        lines.append(f"enum {en.name} {{")
        for v in en.values:
            lines.append(f"    {_proto_enum_value(en.name, v.name)} = {v.number};")
        lines.append("}")
        lines.append("")

    for msg in messages:
        lines.append(f"message {msg.name} {{")
        resv = msg.reserved or []
        if not msg.fields and not resv:
            lines.append("    // empty body")
        for f in msg.fields:
            prefix = "repeated " if f.repeated else ""
            lines.append(f"    {prefix}{f.proto_type} {f.name} = {f.number};")
        if resv:
            lines.append(f"    reserved {', '.join(str(t) for t in resv)};")
        lines.append("}")
        lines.append("")

    return "\n".join(lines) + "\n"


def generate_package_proto(art_path: str | Path,
                            out_root: str | Path) -> Path:
    """Parse a .art file, emit ONE .proto per package at the path
    mirroring the **source** .art package name.

    Two distinct names are at play:

    * **File path** uses the source .art package verbatim
      (``system.services.sm`` → ``system/services/sm/sm.proto``)
      so the on-disk layout mirrors what the user wrote in .art.
      Apps include ``"system/services/sm/sm.pb.h"``.
    * **Proto ``package`` declaration** is the source-true name
      flattened to one underscore-joined identifier
      (``system_services_sm``). protoc emits a single C++ namespace
      from it, so there is no libc ``system()`` collision to dodge.
      See :data:`artheia.generators.proto._PROTO_PACKAGE_LEAD_RENAMES`.

    The file ends up at::

        <out_root>/<art-package-as-path>/<leaf>.proto

    where ``leaf`` is the last segment of the source package.

    Returns the written .proto path.
    """
    art_path = Path(art_path)
    out_root = Path(out_root)

    # Package-aware load (#378): merge package.art + component.art so
    # messages/nodes resolve across the spec/wiring split regardless of
    # which file is named.
    from artheia.model import parse_file
    model = parse_file(str(art_path))

    # Source spec name drives both the filesystem path (dotted →
    # dirs) and the proto package decl (flattened → underscores).
    src_package = model.name or ""
    src_parts = src_package.split(".") if src_package else ["artheia"]
    proto_package = _proto_package_name(src_package)

    leaf = src_parts[-1]
    out_dir = out_root.joinpath(*src_parts)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{leaf}.proto"

    rendered = _render_proto(
        model,
        package=proto_package,
        source_file=str(art_path),
    )
    out_file.write_text(rendered)

    # Sibling .options: pin every string/bytes field to a fixed char[] so nanopb
    # emits a plain member (not a pb_callback_t). nanopb_generator auto-loads the
    # same-basename .options. The field prefix is the FLAT proto package
    # (system_app.) — matching the proto's `package` decl, which nanopb keys on.
    # ALWAYS written (header-only when the package has no sized fields): the
    # per-package BUILD.bazel lists `<leaf>.options` in srcs, and emitting it
    # unconditionally makes a no-force regen a byte-clean no-op instead of
    # leaving a stale hand-placeholder behind.
    options_text = _render_options(model, proto_package.replace(".", "_"))
    (out_dir / f"{leaf}.options").write_text(options_text)

    return out_file
