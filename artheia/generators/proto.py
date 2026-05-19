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


def generate_proto(model, out_dir: str | Path, source_file: str = "") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = _env()
    msg_tpl = env.get_template("message.proto.j2")
    enum_tpl = env.get_template("enum.proto.j2")
    package = model.name or "artheia"

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
