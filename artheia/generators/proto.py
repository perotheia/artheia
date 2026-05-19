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


@dataclass
class _Message:
    name: str
    fields: list[_Field]


def _proto_type_for(field) -> tuple[str, str | None]:
    """Return (proto_type, optional_imported_message_name)."""
    t = field.type
    if getattr(t, "ref", None) is not None:
        return t.ref.name, t.ref.name
    return t.kind, None


def _messages(model) -> Iterable:
    for el in model.elements:
        if el.__class__.__name__ == "MessageDecl":
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
    tpl = env.get_template("message.proto.j2")
    package = model.name or "artheia"

    written: list[Path] = []
    for msg in _messages(model):
        imports: list[str] = []
        fields: list[_Field] = []
        for f in msg.fields:
            proto_type, imp = _proto_type_for(f)
            if imp and imp != msg.name and f"{imp}.proto" not in imports:
                imports.append(f"{imp}.proto")
            fields.append(
                _Field(name=f.name, number=f.number, proto_type=proto_type, repeated=bool(f.repeated))
            )

        rendered = tpl.render(
            source_file=source_file,
            package=package,
            imports=sorted(imports),
            message=_Message(name=msg.name, fields=fields),
        )
        path = out_dir / f"{msg.name}.proto"
        path.write_text(rendered)
        written.append(path)

    return written
