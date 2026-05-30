"""Proto3 generator: one .proto per Artheia message.

Output matches the conventions used by the existing nanopb pipeline in
~/repo/theia/gateway/pero_cmp_lnx/tools/templates/proto.j2 — namespaced
package line, one message per file, no nested messages. References between
messages become `import "Other.proto"` lines.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES = Path(__file__).parent / "templates"


@dataclass
class _Field:
    name: str
    number: int
    proto_type: str
    repeated: bool
    options: str = ""  # raw pass-through, e.g. "(nanopb).max_length = 20"


@dataclass
class _Message:
    name: str
    fields: list[_Field]


@dataclass
class _EnumValue:
    name: str
    number: int


@dataclass
class _Enum:
    name: str
    values: list[_EnumValue]


def _proto_type_for(field) -> tuple[str, str | None]:
    """Return (proto_type, optional_imported_decl_name).

    The imported decl can be either a message or an enum — both live in
    their own `.proto` file in the output.
    """
    t = field.type
    if getattr(t, "ref", None) is not None:
        return t.ref.name, t.ref.name
    return t.kind, None


def _messages(model) -> Iterable:
    for el in model.elements:
        if el.__class__.__name__ == "MessageDecl":
            yield el


def _enums(model) -> Iterable:
    for el in model.elements:
        if el.__class__.__name__ == "EnumDecl":
            yield el


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


# Top-level .art package-name segments that would collide with libc /
# POSIX identifiers IF protoc emitted them as a nested C++ namespace.
#
# This used to rewrite a leading `system` → `services` to dodge libc's
# `int system(const char*)`. That concern only applies to a *nested*
# namespace (`::system::supervisor::`). The proto package decls Theia
# emits are FLATTENED to a single underscore-joined identifier
# (`package system_demo;` — see proto_package.py), so protoc produces
# one namespace `system_demo` and one C-struct prefix `system_demo_*`.
# A flattened `system_demo` / `system_demo_Inc` is never the bare token
# `system`, so it cannot bind to libc `system()`. The rewrite was
# therefore unnecessary and made package names read as `services_demo`
# instead of the source-true `system_demo`. The table is now empty;
# add an entry only if a genuinely-colliding flat name ever appears.
_PROTO_PACKAGE_LEAD_RENAMES: dict[str, str] = {}


def _proto_package_name(art_package: str) -> str:
    """Map an .art package name to its proto package name.

    Identity today (no lead segments are rewritten — see
    ``_PROTO_PACKAGE_LEAD_RENAMES`` for the history). Kept as the single
    choke point so a future collision rewrite stays centralized.

    Empty / missing names fall through to "artheia".
    """
    if not art_package:
        return "artheia"
    head, dot, rest = art_package.partition(".")
    if head in _PROTO_PACKAGE_LEAD_RENAMES:
        head = _PROTO_PACKAGE_LEAD_RENAMES[head]
    return head + (dot + rest if dot else "")


def package_subdir(art_package: str) -> Path:
    """Map an .art package to its filesystem subdir under proto-root.

    The .art ``system.services.sm`` lays out at
    ``system/services/sm/`` — mirrors the source-tree layout
    (``services/system/sm/package.art``) verbatim.

    Note: the .proto file's ``package`` declaration is the flattened
    underscore-joined name (``system_services_sm``), not this dir
    name. They're independent — the filesystem mirrors the .art
    spec name, the C++ namespace is what protoc emits from the
    flattened declaration. See :data:`_PROTO_PACKAGE_LEAD_RENAMES`
    for the (now-empty) collision-rewrite history.

    Empty/missing names land at ``artheia/``.
    """
    if not art_package:
        return Path("artheia")
    return Path(*art_package.split("."))


def generate_proto(model, out_dir: str | Path, source_file: str = "") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = _env()
    msg_tpl = env.get_template("message.proto.j2")
    enum_tpl = env.get_template("enum.proto.j2")
    package = _proto_package_name(model.name or "")

    written: list[Path] = []

    # Emit enum protos first so message imports can reference them.
    for en in _enums(model):
        rendered = enum_tpl.render(
            source_file=source_file,
            package=package,
            enum=_Enum(
                name=en.name,
                values=[_EnumValue(name=v.name, number=v.number) for v in en.values],
            ),
        )
        path = out_dir / f"{en.name}.proto"
        path.write_text(rendered)
        written.append(path)

    for msg in _messages(model):
        imports: list[str] = []
        fields: list[_Field] = []
        # Artheia message fields are unnumbered; we assign 1..N in
        # declaration order. Any trailing nanopb options block is passed
        # through verbatim — the generator does not parse it.
        for idx, f in enumerate(msg.fields):
            proto_type, imp = _proto_type_for(f)
            if imp and imp != msg.name and f"{imp}.proto" not in imports:
                imports.append(f"{imp}.proto")
            fields.append(_Field(
                name=f.name,
                number=idx + 1,
                proto_type=proto_type,
                repeated=bool(f.repeated),
                options=(getattr(f, "options", "") or "").strip(),
            ))

        rendered = msg_tpl.render(
            source_file=source_file,
            package=package,
            imports=sorted(imports),
            message=_Message(name=msg.name, fields=fields),
        )
        path = out_dir / f"{msg.name}.proto"
        path.write_text(rendered)
        written.append(path)

    return written
