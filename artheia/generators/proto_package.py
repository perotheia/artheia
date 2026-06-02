"""Per-package proto3 generator: one combined .proto per .art package.

Unlike `proto.py` (which emits one .proto per message), this generator
emits a single .proto file per source package, laid out under the
output root in directories that mirror the package path:

    package demo.system     → <out>/demo/system/system.proto
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
            for idx, f in enumerate(el.fields):
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
                    number=idx + 1,
                    proto_type=pt,
                    repeated=bool(f.repeated),
                ))
            messages.append(_Message(name=el.name, fields=fields))
        elif kind == "EnumDecl":
            enums.append(_Enum(name=el.name, values=el.values))
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


def _render_proto(model, package: str, source_file: str) -> str:
    messages, enums, imports = _harvest(model)
    lines: List[str] = []
    lines.append("// AUTO-GENERATED by `artheia gen-proto-package` — DO NOT EDIT")
    lines.append(f"// source: {source_file}")
    lines.append("")
    lines.append("syntax = \"proto3\";")
    # Proto package: dot-joined from the .art package. Underscores in
    # the leaf are preserved so nanopb-generated C struct names match
    # what we expect (e.g. demo.system → "demo_system_<Msg>").
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
        if not msg.fields:
            lines.append("    // empty body")
        for f in msg.fields:
            prefix = "repeated " if f.repeated else ""
            lines.append(f"    {prefix}{f.proto_type} {f.name} = {f.number};")
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
    return out_file
