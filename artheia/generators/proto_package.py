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
        # Import path uses the FLAT-package dir + leaf — matching the C++ include
        # convention the bundled proto already exposes (e.g. the runtime ships
        # proto/platform_runtime/runtime.proto and #include
        # "platform_runtime/runtime.pb.h"). leaf = last .art-package segment.
        leaf = ref_pkg.split(".")[-1]
        return f"{flat}.{ref.name}", f"{flat}/{leaf}.proto"
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


_DEFAULT_STR_MAX = 64  # nanopb char[] size for an unpinned string/bytes field


def _str_max_for(field) -> "int | None":
    """The nanopb max_size for a string/bytes field, or None for non-string.

    A nanopb string/bytes field with NO max_size becomes a pb_callback_t (a
    function pointer the runtime can't strncpy / a migration plugin can't copy).
    So EVERY string/bytes field gets a fixed char[]: the size from an inline
    `.art` `[max_size:N]` (or `[max_length:N]`) option if present, else a
    sensible default. Non-string fields return None (no .options line)."""
    t = getattr(field, "type", None)
    kind = getattr(t, "kind", None)
    if kind not in ("string", "bytes"):
        return None
    opt = (getattr(field, "options", "") or "")
    import re
    m = re.search(r"max_(?:size|length)\s*[:=]\s*(\d+)", opt)
    return int(m.group(1)) if m else _DEFAULT_STR_MAX


def _render_options(model, flat_package: str) -> str:
    """Emit a nanopb .options file pinning every string/bytes field to a fixed
    char[] (so it's a plain member, not a pb_callback_t). nanopb_generator
    auto-loads the same-basename .options next to the .proto. Lines are
    `<flat_pkg>.<Message>.<field>  max_size:N`. Empty (header only) if the
    package has no string/bytes fields."""
    rows: List[str] = []
    for el in model.elements:
        if el.__class__.__name__ != "MessageDecl":
            continue
        for item in getattr(el, "fields", []) or []:
            if item.__class__.__name__ == "MessageReserved":
                continue
            n = _str_max_for(item)
            if n is not None:
                rows.append(f"{flat_package}.{el.name}.{item.name}   max_size:{n}")
    head = [
        "# AUTO-GENERATED by `artheia gen-app` — nanopb field-size constraints.",
        "# nanopb_generator auto-loads this (same basename next to the .proto).",
        "# A string/bytes field without max_size becomes a pb_callback_t; pinning",
        "# it to a fixed char[] makes it a plain member (strncpy-able, trace-",
        f"# decodable). Default {_DEFAULT_STR_MAX}; override per field with an .art",
        "# `[max_size:N]` option.",
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
    # Write it whenever the package has any string/bytes field.
    options_text = _render_options(model, proto_package.replace(".", "_"))
    if any(line and not line.startswith("#")
           for line in options_text.splitlines()):
        (out_dir / f"{leaf}.options").write_text(options_text)

    return out_file
