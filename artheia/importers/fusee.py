"""Fusée — clean-room reverse-engineer of the Tornado vehicle-OS into Artheia.

Two outputs are produced from `up/tornado/`:

1. **Signals** (the catalogue). Reads
   `up/tornado/signals/<category>/{*.proto, BUILD}` and emits a single
   `vendor/tornado/system/signals/<category>/package.art` per category.
   Every proto `message` becomes an Artheia `message`; every
   `vehicle_os_signal(...)` in the BUILD becomes an `interface
   senderReceiver` capturing the DDS topic's data shape.

2. **Components** (the system's nodes + composition). Reads
   `up/tornado/app/onboard/<comp>/BUILD`, finds the
   `vehicle_os_signal_junction(input_signals=..., output_signals=...)` block,
   and emits `vendor/tornado/system/components/<comp>.art` with one `node
   atomic` per component (ports for each input/output topic, reliability
   modifier pulled from the BUILD QoS string). A
   `vendor/tornado/system/system.art` composition wires every (publisher,
   subscriber) pair on each topic.

This is a clean-room redesign, not a copy: the original `.proto` files are
not vendored alongside the `.art`. The `.art` is the only artifact.

Lossy proto2 → Artheia transcription:
  - `required`/`optional` are dropped (Artheia has only proto3-equivalent
    fields); `repeated` is preserved.
  - `enum` types are flattened to `uint32`; the enum's value list lives in
    a leading `//` comment.
  - `oneof` is flattened: each branch becomes an ordinary field with a
    `// oneof <name>` note.
  - Nested `message` decls are hoisted to top level as `<Outer><Inner>`.
  - `default`, `nanopb` options, `java_package`, `deprecated`, `reserved`
    are dropped, carrying `//` annotations where useful.
  - Cross-category type refs that textX can't resolve get a local empty
    stub (`message X { }`) with an `// origin: ...` comment pointing at the
    real declaration's category.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---- proto2 model -----------------------------------------------------------


PROTO2_TO_ART = {
    "int32": "int32",
    "int64": "int64",
    "uint32": "uint32",
    "uint64": "uint64",
    "sint32": "sint32",
    "sint64": "sint64",
    "fixed32": "fixed32",
    "fixed64": "fixed64",
    "sfixed32": "sfixed32",
    "sfixed64": "sfixed64",
    "float": "float",
    "double": "double",
    "bool": "bool",
    "string": "string",
    "bytes": "bytes",
}


@dataclass
class EnumDecl:
    name: str
    values: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class FieldDecl:
    label: str  # "required" | "optional" | "repeated" | ""
    type_name: str  # primitive or proto identifier (may be enum)
    name: str
    number: int
    # Optional source annotations the importer wants to surface in the .art
    # comment line attached to the field.
    notes: list[str] = field(default_factory=list)


@dataclass
class OneofDecl:
    name: str
    fields: list[FieldDecl] = field(default_factory=list)


@dataclass
class MessageDecl:
    name: str
    fields: list[FieldDecl] = field(default_factory=list)
    oneofs: list[OneofDecl] = field(default_factory=list)
    nested_messages: list["MessageDecl"] = field(default_factory=list)
    nested_enums: list[EnumDecl] = field(default_factory=list)
    leading_comment: str | None = None


@dataclass
class ProtoFile:
    path: Path
    package: str | None = None
    imports: list[str] = field(default_factory=list)
    options: list[tuple[str, str]] = field(default_factory=list)
    messages: list[MessageDecl] = field(default_factory=list)
    enums: list[EnumDecl] = field(default_factory=list)


# ---- proto2 parser ---------------------------------------------------------
#
# Tornado's proto2 dialect is small (no services, no extends, no maps). A
# tokenless line/brace scanner is enough.


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_FIELD_OPTS_RE = re.compile(r"\[.*?\]", re.DOTALL)


def _strip_comments(src: str) -> str:
    return _COMMENT_RE.sub("", src)


def _tokenize(src: str) -> list[str]:
    """Split on whitespace and on `{`, `}`, `;`, `=`, `,` punctuation."""
    src = _FIELD_OPTS_RE.sub("", src)
    src = re.sub(r"([{}();=,])", r" \1 ", src)
    return src.split()


def parse_proto_file(path: Path) -> ProtoFile:
    raw = path.read_text()
    src = _strip_comments(raw)
    tokens = _tokenize(src)
    pf = ProtoFile(path=path)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "syntax":
            # syntax = "proto2" ;
            while i < len(tokens) and tokens[i] != ";":
                i += 1
            i += 1
        elif t == "package":
            i += 1
            name = ""
            while i < len(tokens) and tokens[i] != ";":
                name += tokens[i]
                i += 1
            pf.package = name
            i += 1
        elif t == "import":
            i += 1
            # "path";
            target = tokens[i].strip('"')
            pf.imports.append(target)
            while i < len(tokens) and tokens[i] != ";":
                i += 1
            i += 1
        elif t == "option":
            # option k = "v";
            i += 1
            key = tokens[i]
            i += 1  # =
            i += 1
            val = tokens[i].strip('"')
            pf.options.append((key, val))
            while i < len(tokens) and tokens[i] != ";":
                i += 1
            i += 1
        elif t == "message":
            msg, i = _parse_message(tokens, i + 1)
            pf.messages.append(msg)
        elif t == "enum":
            en, i = _parse_enum(tokens, i + 1)
            pf.enums.append(en)
        else:
            i += 1
    return pf


def _parse_message(tokens: list[str], i: int) -> tuple[MessageDecl, int]:
    name = tokens[i]
    i += 1
    assert tokens[i] == "{", f"expected '{{' after message {name}"
    i += 1
    msg = MessageDecl(name=name)
    while tokens[i] != "}":
        t = tokens[i]
        if t == "message":
            sub, i = _parse_message(tokens, i + 1)
            msg.nested_messages.append(sub)
        elif t == "enum":
            en, i = _parse_enum(tokens, i + 1)
            msg.nested_enums.append(en)
        elif t == "oneof":
            of, i = _parse_oneof(tokens, i + 1)
            msg.oneofs.append(of)
        elif t == "reserved":
            while tokens[i] != ";":
                i += 1
            i += 1
        elif t in ("required", "optional", "repeated"):
            fld, i = _parse_field(tokens, i)
            msg.fields.append(fld)
        elif t == ";":
            i += 1
        else:
            # proto3-style field (no label) — accept too.
            fld, i = _parse_field(tokens, i, default_label="")
            msg.fields.append(fld)
    return msg, i + 1  # consume '}'


def _parse_oneof(tokens: list[str], i: int) -> tuple[OneofDecl, int]:
    name = tokens[i]
    i += 1
    assert tokens[i] == "{"
    i += 1
    of = OneofDecl(name=name)
    while tokens[i] != "}":
        if tokens[i] == ";":
            i += 1
            continue
        fld, i = _parse_field(tokens, i, default_label="oneof")
        of.fields.append(fld)
    return of, i + 1


def _parse_field(
    tokens: list[str], i: int, default_label: str = ""
) -> tuple[FieldDecl, int]:
    label = ""
    if tokens[i] in ("required", "optional", "repeated"):
        label = tokens[i]
        i += 1
    elif default_label:
        label = default_label
    type_name = tokens[i]
    i += 1
    fname = tokens[i]
    i += 1
    assert tokens[i] == "=", (
        f"expected '=' after field {fname}, got {tokens[i:i+3]}"
    )
    i += 1
    number = int(tokens[i])
    i += 1
    # eat optional ';'
    if i < len(tokens) and tokens[i] == ";":
        i += 1
    return FieldDecl(label=label, type_name=type_name, name=fname, number=number), i


def _parse_enum(tokens: list[str], i: int) -> tuple[EnumDecl, int]:
    name = tokens[i]
    i += 1
    assert tokens[i] == "{"
    i += 1
    en = EnumDecl(name=name)
    while tokens[i] != "}":
        if tokens[i] == ";":
            i += 1
            continue
        vname = tokens[i]
        i += 1
        assert tokens[i] == "="
        i += 1
        vnum = int(tokens[i])
        i += 1
        if i < len(tokens) and tokens[i] == ";":
            i += 1
        en.values.append((vname, vnum))
    return en, i + 1


# ---- BUILD parser ----------------------------------------------------------


_BUILD_SIGNAL_RE = re.compile(
    r"vehicle_os_signal\s*\(([^)]*)\)", re.DOTALL
)
_KEYVAL_RE = re.compile(r'(\w+)\s*=\s*"([^"]+)"')


@dataclass
class SignalDecl:
    name: str  # raw bazel target name e.g. "vehicle_state_signal"
    proto_target: str  # e.g. "vehicle_state_proto" or "//.../foo_proto"
    proto_message_name: str  # e.g. "moz_msg_VehicleState"


def parse_build(path: Path) -> list[SignalDecl]:
    if not path.exists():
        return []
    raw = path.read_text()
    out: list[SignalDecl] = []
    for m in _BUILD_SIGNAL_RE.finditer(raw):
        body = m.group(1)
        kv = dict(_KEYVAL_RE.findall(body))
        if "name" not in kv or "proto" not in kv or "proto_message_name" not in kv:
            continue
        out.append(
            SignalDecl(
                name=kv["name"],
                proto_target=kv["proto"],
                proto_message_name=kv["proto_message_name"],
            )
        )
    return out


# ---- emitter ---------------------------------------------------------------
#
# The emitter takes the parsed proto files + the BUILD signals for one
# category and produces a `package.art` body.


def _proto_msg_to_art(name: str) -> str:
    """`moz_msg_VehicleState` -> `VehicleState`."""
    return name.removeprefix("moz_msg_")


def _signal_iface_name(signal_name: str) -> str:
    """`vehicle_state_signal` -> `vehicle_state` (keep snake_case for ports)."""
    return signal_name.removesuffix("_signal")


def _is_primitive(t: str) -> bool:
    return t in PROTO2_TO_ART


def _normalize_type(t: str, enum_names: set[str], nested_rename: dict[str, str]) -> tuple[str, list[str]]:
    """Map a proto field type to an Artheia primitive or message name.

    Returns (artheia_type, extra_field_notes).
    """
    notes: list[str] = []
    if _is_primitive(t):
        return PROTO2_TO_ART[t], notes
    bare = t.lstrip(".")
    # Apply hoist renames first — a nested enum like `ChargingCommand.Action`
    # (or just `Action`) is renamed to `ChargingCommandAction`, and only
    # after renaming can the enum-set lookup succeed.
    resolved = nested_rename.get(bare, bare)
    if resolved in enum_names:
        notes.append(f"originally enum {resolved}")
        return "uint32", notes
    return resolved, notes


def _enum_comment(en: EnumDecl) -> str:
    vals = ", ".join(f"{n}={v}" for n, v in en.values)
    return f"// enum {en.name}: {vals}"


def _hoist_nested(
    msg: MessageDecl,
    prefix: str = "",
) -> tuple[list[MessageDecl], list[EnumDecl], dict[str, str]]:
    """Walk nested messages/enums, hoisting them to flat lists.

    Returns (flat_messages, flat_enums, rename_map). The rename_map maps the
    original short name (or `Outer.Inner` dotted) to the new flat name.
    """
    flat_msgs: list[MessageDecl] = []
    flat_enums: list[EnumDecl] = []
    rename: dict[str, str] = {}
    new_prefix = f"{prefix}{msg.name}"
    for sub in msg.nested_messages:
        hoisted_name = f"{new_prefix}{sub.name}"
        sub_copy = MessageDecl(
            name=hoisted_name,
            fields=sub.fields,
            oneofs=sub.oneofs,
            leading_comment=f"hoisted from {new_prefix}.{sub.name}",
        )
        rename[sub.name] = hoisted_name
        rename[f"{msg.name}.{sub.name}"] = hoisted_name
        nm, ne, nr = _hoist_nested(sub_copy, prefix=new_prefix)
        # The hoisted message keeps only its own fields/oneofs; deeper nested
        # were already split out by recursion. Replace any nested_messages
        # left on it with empties.
        sub_copy.nested_messages = []
        sub_copy.nested_enums = []
        flat_msgs.append(sub_copy)
        flat_msgs.extend(nm)
        flat_enums.extend(ne)
        rename.update(nr)
    for en in msg.nested_enums:
        new_en = EnumDecl(name=f"{new_prefix}{en.name}", values=en.values)
        rename[en.name] = new_en.name
        rename[f"{msg.name}.{en.name}"] = new_en.name
        flat_enums.append(new_en)
    return flat_msgs, flat_enums, rename


def _collect_referenced_types(messages: list[MessageDecl]) -> set[str]:
    refs: set[str] = set()
    for m in messages:
        for f in m.fields:
            refs.add(f.type_name.lstrip("."))
        for of in m.oneofs:
            for f in of.fields:
                refs.add(f.type_name.lstrip("."))
    return refs


def emit_package_art(
    category: str,
    proto_files: list[ProtoFile],
    signals: list[SignalDecl],
    *,
    external_message_origins: dict[str, str] | None = None,
) -> str:
    """Return the textual contents of a `package.art` file.

    `external_message_origins`: optional map of message-name → category where
    that message is declared. Used to emit forward-declaration stubs for
    messages referenced across categories — textX's name resolution is
    single-file, so a referenced type must exist locally.
    """
    lines: list[str] = []
    lines.append(
        f"// Generated from up/tornado/signals/{category}/ — DO NOT EDIT BY HAND.\n"
        f"// Lossy proto2 → Artheia transcription. See artheia/importers/fusee.py.\n"
        f"// Each `vehicle_os_signal(...)` in the original BUILD is a DDS topic\n"
        f"// (many-to-many pub/sub). Captured here as a senderReceiver interface;\n"
        f"// the system.art composition wires every (publisher, subscriber) pair."
    )
    lines.append("")
    lines.append(f"package vendor.tornado.system.signals.{category}")
    lines.append("")

    # Imports: any proto file that imports another category's proto becomes
    # an `import vendor.tornado.system.signals.<cat>.*` line. Local imports inside
    # this category are no-ops.
    imports: set[str] = set()
    for pf in proto_files:
        for imp in pf.imports:
            if not imp.startswith("onboard/signals/"):
                continue
            parts = imp.split("/")
            # onboard/signals/<category>/<file>.proto
            if len(parts) >= 4:
                other_cat = parts[2]
                if other_cat != category:
                    imports.add(f"vendor.tornado.system.signals.{other_cat}")
    for imp in sorted(imports):
        lines.append(f"import {imp}.*")
    if imports:
        lines.append("")

    # ---- collect: hoist nested, build enum set, build rename map -----------
    # We process all proto files in one pass so cross-message refs resolve.
    flat_messages: list[MessageDecl] = []
    flat_enums: list[EnumDecl] = []
    rename_map: dict[str, str] = {}

    for pf in proto_files:
        for top in pf.messages:
            nm, ne, nr = _hoist_nested(top, prefix="")
            # Take the top message itself but with its nested cleared.
            top_clean = MessageDecl(
                name=top.name,
                fields=top.fields,
                oneofs=top.oneofs,
                leading_comment=top.leading_comment,
            )
            flat_messages.append(top_clean)
            flat_messages.extend(nm)
            flat_enums.extend(ne)
            rename_map.update(nr)
        flat_enums.extend(pf.enums)

    enum_names = {e.name for e in flat_enums}
    # Emit enums first as // comments — Artheia has no enum decl.
    if flat_enums:
        lines.append("// ---- enums (flattened to uint32 at use sites) ----")
        for en in flat_enums:
            lines.append(_enum_comment(en))
        lines.append("")

    # ---- emit forward-declaration stubs for cross-category / missing refs --
    # textX cross-references are single-file, so any message referenced by a
    # field (or by a senderReceiver interface) but declared in another
    # category — or not declared anywhere — needs a local empty stub.
    local_names = {m.name for m in flat_messages} | enum_names
    refs = _collect_referenced_types(flat_messages)
    # interface refs (signals declared in BUILD)
    for sig in signals:
        refs.add(_proto_msg_to_art(sig.proto_message_name))
    externals: list[tuple[str, str]] = []
    for ref in sorted(refs):
        if _is_primitive(ref):
            continue
        if ref in local_names:
            continue
        if ref in rename_map:
            continue
        origin = (external_message_origins or {}).get(ref, "unknown")
        externals.append((ref, origin))
    if externals:
        lines.append("// ---- forward-decl stubs for cross-category / missing refs ----")
        lines.append("// (textX cross-references are single-file; real defs live elsewhere)")
        for name, origin in externals:
            if origin == "unknown":
                lines.append(f"// origin: unresolved — referenced by BUILD or proto but not declared")
            else:
                lines.append(f"// origin: vendor.tornado.system.signals.{origin}")
            lines.append(f"message {name} {{ }}")
        lines.append("")

    # ---- emit messages -----------------------------------------------------
    for msg in flat_messages:
        if msg.leading_comment:
            lines.append(f"// {msg.leading_comment}")
        lines.append(f"message {msg.name} {{")
        for f in msg.fields:
            ftype, notes = _normalize_type(f.type_name, enum_names, rename_map)
            repeated = "repeated " if f.label == "repeated" else ""
            prefix_notes: list[str] = []
            if f.label and f.label not in ("repeated", ""):
                prefix_notes.append(f.label)
            prefix_notes.extend(notes)
            tail = f"  // {', '.join(prefix_notes)}" if prefix_notes else ""
            lines.append(
                f"    {repeated}{ftype} {f.name} = {f.number}{tail}"
            )
        for of in msg.oneofs:
            lines.append(f"    // oneof {of.name} (proto2 oneof flattened — at most one set)")
            for f in of.fields:
                ftype, notes = _normalize_type(f.type_name, enum_names, rename_map)
                prefix_notes = [f"oneof {of.name}"]
                prefix_notes.extend(notes)
                tail = f"  // {', '.join(prefix_notes)}"
                lines.append(
                    f"    {ftype} {f.name} = {f.number}{tail}"
                )
        lines.append("}")
        lines.append("")

    # ---- emit senderReceiver interfaces ------------------------------------
    if signals:
        lines.append("// ---- DDS topics (vehicle_os_signal in original BUILD) ----")
        message_names = {m.name for m in flat_messages}
        for sig in signals:
            iface = _signal_iface_name(sig.name)
            msg_name = _proto_msg_to_art(sig.proto_message_name)
            # If the message lives in another category (or is unresolved),
            # the stub block above has already forward-declared it as an
            # empty `message X { }` so the cross-reference resolves locally.
            ref = msg_name
            lines.append(f"interface senderReceiver {iface} {{")
            lines.append(f"    data {ref} payload")
            lines.append("}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---- top-level driver ------------------------------------------------------


def _category_proto_files(src_category_dir: Path) -> list[ProtoFile]:
    return [parse_proto_file(p) for p in sorted(src_category_dir.glob("*.proto"))]


def _index_message_origins(
    categories: dict[str, list[ProtoFile]],
) -> dict[str, str]:
    """Map every declared message (including hoisted nested) to its category."""
    origin: dict[str, str] = {}
    for cat, pfs in categories.items():
        for pf in pfs:
            for top in pf.messages:
                origin[top.name] = cat
                # Walk nested to register their hoisted names too.
                stack = [(top, "")]
                while stack:
                    msg, prefix = stack.pop()
                    new_prefix = f"{prefix}{msg.name}"
                    for sub in msg.nested_messages:
                        origin[f"{new_prefix}{sub.name}"] = cat
                        stack.append((sub, new_prefix))
    return origin


def import_platform(
    signals_root: Path,
    platform_root: Path,
) -> list[Path]:
    """Generate `<platform_root>/<cat>/package.art` for every signal category.

    Two passes: first parse every category to build a name → category index,
    then emit each `package.art` with that index so cross-category refs can
    be stubbed locally as forward-declarations.

    Does NOT copy the source .proto files — this is a clean-room redesign.
    """
    cat_dirs = [d for d in sorted(signals_root.iterdir()) if d.is_dir()]
    parsed: dict[str, list[ProtoFile]] = {
        d.name: _category_proto_files(d) for d in cat_dirs
    }
    origins = _index_message_origins(parsed)

    out: list[Path] = []
    for d in cat_dirs:
        category = d.name
        signals = parse_build(d / "BUILD")
        dst = platform_root / category
        dst.mkdir(parents=True, exist_ok=True)
        art_text = emit_package_art(
            category,
            parsed[category],
            signals,
            external_message_origins=origins,
        )
        out_path = dst / "package.art"
        out_path.write_text(art_text)
        out.append(out_path)
    return out


# ---- components round ------------------------------------------------------
#
# A `vehicle_os_signal_junction` in a component's BUILD declares the topics
# the node consumes and produces. Each entry looks like:
#
#     "@vehicle_os//onboard/signals/<category>:<name>_signal": "RELIABLE",
#
# We translate every component into one `node atomic <CamelName>` with a
# port per signal — `receiver` for inputs, `sender` for outputs. The QoS
# string maps to the grammar's reliability modifier.


_JUNCTION_RE = re.compile(
    r"vehicle_os_signal_junction\s*\((?P<body>.*?)\)\s*(?=\Z|\w+\s*\()",
    re.DOTALL,
)
_DICT_BLOCK_RE = re.compile(
    r'(input_signals|output_signals)\s*=\s*\{(?P<body>[^}]*)\}',
    re.DOTALL,
)
_SIGNAL_ENTRY_RE = re.compile(
    r'"(?P<target>[^"]+)"\s*:\s*"(?P<qos>[A-Z_]+)"'
)
_NAME_RE = re.compile(r'name\s*=\s*"([^"]+)"')

_QOS_TO_RELIABILITY = {
    "RELIABLE": "reliable",
    "BEST_EFFORT": "best_effort",
}


@dataclass
class JunctionPort:
    """One side of a vehicle_os_signal_junction entry."""
    direction: str  # "in" | "out"
    target: str     # raw bazel target, e.g. "@vehicle_os//onboard/signals/body:foo_signal"
    qos: str        # "RELIABLE" | "BEST_EFFORT"

    @property
    def category(self) -> str:
        # target like "<prefix>//onboard/signals/<cat>:<name>_signal"
        path, _, _name = self.target.partition(":")
        return path.rsplit("/", 1)[-1]

    @property
    def signal_name(self) -> str:
        _path, _, name = self.target.partition(":")
        return name  # e.g. "vehicle_state_signal"

    @property
    def interface(self) -> str:
        """Artheia interface name — the signal name without the `_signal` suffix."""
        return self.signal_name.removesuffix("_signal")


@dataclass
class ComponentDecl:
    name: str  # snake_case directory name
    ports: list[JunctionPort] = field(default_factory=list)

    @property
    def camel_name(self) -> str:
        return "".join(p.capitalize() for p in self.name.split("_"))


def _strip_python_comments(src: str) -> str:
    """Strip `#` line comments only — do NOT touch `//` since bazel target
    strings contain `//` (e.g. `@vehicle_os//onboard/signals/...`)."""
    return re.sub(r"#[^\n]*", "", src)


def parse_component_build(build_path: Path) -> ComponentDecl | None:
    """Read a component BUILD; return None if it has no signal_junction."""
    if not build_path.exists():
        return None
    raw = build_path.read_text()
    raw_nc = _strip_python_comments(raw)
    m = re.search(r"vehicle_os_signal_junction\s*\(", raw_nc)
    if not m:
        return None
    # find matching closing paren (the regex above is fragile for nested
    # dicts; do a paren scan starting after the opening `(`).
    start = m.end()
    depth = 1
    i = start
    while i < len(raw_nc) and depth:
        ch = raw_nc[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    body = raw_nc[start : i - 1]

    comp_name = build_path.parent.name
    ports: list[JunctionPort] = []
    for block in _DICT_BLOCK_RE.finditer(body):
        direction = "in" if block.group(1) == "input_signals" else "out"
        for entry in _SIGNAL_ENTRY_RE.finditer(block.group("body")):
            ports.append(
                JunctionPort(
                    direction=direction,
                    target=entry.group("target"),
                    qos=entry.group("qos"),
                )
            )
    return ComponentDecl(name=comp_name, ports=ports)


def _port_local_name(p: JunctionPort) -> str:
    """A unique, valid Artheia identifier for the port within its node.

    The grammar identifier rules accept snake_case ASCII. The interface name
    is already snake_case (e.g. `vehicle_state`, `drive_mode_request`); we
    just prefix `in_` / `out_` so a single component can have ports with the
    same topic name on both sides without colliding.
    """
    return f"{'in' if p.direction == 'in' else 'out'}_{p.interface}"


def _emit_component_art(
    comp: ComponentDecl,
    tipc_type: int,
    *,
    platform_message_index: dict[str, str],
) -> str:
    """Render a single component as a .art file."""
    cats_used = sorted({p.category for p in comp.ports})
    lines: list[str] = []
    lines.append(
        f"// Generated from up/tornado/app/onboard/{comp.name}/BUILD — DO NOT EDIT BY HAND.\n"
        f"// Translates the vehicle_os_signal_junction into an Artheia node.\n"
        f"// Synthetic TIPC address allocated by fusée; the real runtime uses DDS topics."
    )
    lines.append("")
    lines.append(f"package vendor.tornado.system.components.{comp.name}")
    lines.append("")

    for cat in cats_used:
        lines.append(f"import vendor.tornado.system.signals.{cat}.*")
    if cats_used:
        lines.append("")

    # textX cross-file references aren't resolved by the loader, so the
    # senderReceiver interfaces named below must be visible *within this
    # file*. Emit a forward-declaration block of empty interfaces.
    seen_ifaces: set[tuple[str, str]] = set()
    for p in comp.ports:
        seen_ifaces.add((p.category, p.interface))
    if seen_ifaces:
        lines.append("// ---- forward-decl stubs for senderReceiver interfaces ----")
        lines.append("// (real declarations live in vendor.tornado.system.signals.<cat>)")
        for cat, iface in sorted(seen_ifaces):
            lines.append(f"// origin: vendor.tornado.system.signals.{cat}")
            lines.append(f"interface senderReceiver {iface} {{ }}")
        lines.append("")

    lines.append(f"node atomic {comp.camel_name} {{")
    lines.append(f"    tipc type=0x{tipc_type:08x} instance=0")
    if comp.ports:
        lines.append("    ports {")
        # inputs first
        in_ports = [p for p in comp.ports if p.direction == "in"]
        out_ports = [p for p in comp.ports if p.direction == "out"]
        for p in in_ports:
            rel = _QOS_TO_RELIABILITY.get(p.qos, "")
            rel_str = f" {rel}" if rel else ""
            lines.append(
                f"        receiver {_port_local_name(p)} requires {p.interface}{rel_str}"
            )
        for p in out_ports:
            rel = _QOS_TO_RELIABILITY.get(p.qos, "")
            rel_str = f" {rel}" if rel else ""
            lines.append(
                f"        sender   {_port_local_name(p)} provides {p.interface}{rel_str}"
            )
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def import_components(
    app_root: Path,
    components_root: Path,
    *,
    platform_message_index: dict[str, str] | None = None,
    tipc_type_base: int = 0x90000000,
) -> tuple[list[Path], list[ComponentDecl]]:
    """Generate `<components_root>/<comp>.art` for every component under
    `app_root` whose BUILD declares a vehicle_os_signal_junction.

    Returns (emitted_paths, parsed_components). The component list feeds the
    system-composition emitter.
    """
    components_root.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    comps: list[ComponentDecl] = []
    # Only scan one level deep — skip generated nested dirs like Gear_ert_rtw.
    comp_dirs = [d for d in sorted(app_root.iterdir()) if d.is_dir()]
    for offset, d in enumerate(comp_dirs):
        comp = parse_component_build(d / "BUILD")
        if comp is None:
            continue
        comps.append(comp)
        tipc_type = tipc_type_base + offset
        art_text = _emit_component_art(
            comp,
            tipc_type,
            platform_message_index=platform_message_index or {},
        )
        out_path = components_root / f"{comp.name}.art"
        out_path.write_text(art_text)
        out_paths.append(out_path)
    return out_paths, comps


# ---- system composition ----------------------------------------------------


def emit_system_art(
    comps: list[ComponentDecl],
) -> str:
    """Render the top-level `system.art` with one composition wiring all
    components by per-(publisher, subscriber) topic pairs.

    Tornado topics are N-to-M (pub/sub broadcast), but the Artheia grammar
    only has 1:1 `connect`. We over-specify: for every topic that has at
    least one publisher and one subscriber, emit a `connect <pub>.<port> to
    <sub>.<port>` line for every pair.

    Because textX cross-references are single-file, nodes referenced in
    `prototype` declarations must also be declared locally. We emit an empty
    `node atomic <CamelName> {...}` stub per component (with the same TIPC
    layout as the component file would not collide with — we use a distinct
    base address to avoid the TIPC uniqueness validator firing).
    """
    lines: list[str] = []
    lines.append(
        "// Generated by fusée — DO NOT EDIT BY HAND.\n"
        "// Wires every (publisher, subscriber) pair on each Tornado DDS topic.\n"
        "// Forward-decls for nodes and interfaces are emitted locally because\n"
        "// textX cross-references are single-file."
    )
    lines.append("")
    lines.append("package vendor.tornado.system")
    lines.append("")

    # Collect topics: interface_name -> {"in": [(comp_idx, port)], "out": [...]}
    topics: dict[str, dict[str, list[tuple[ComponentDecl, JunctionPort]]]] = {}
    for comp in comps:
        for p in comp.ports:
            t = topics.setdefault(p.interface, {"in": [], "out": []})
            t[p.direction].append((comp, p))

    # Forward-decl interfaces (one per topic).
    if topics:
        lines.append("// ---- forward-decl stubs for senderReceiver interfaces ----")
        for iface in sorted(topics):
            lines.append(f"interface senderReceiver {iface} {{ }}")
        lines.append("")

    # Forward-decl nodes — empty ports of the right shape so the connect
    # validator can resolve `proto.port` references.
    lines.append("// ---- forward-decl stubs for nodes ----")
    # Use a different TIPC base to avoid colliding with the components/
    # files if both were loaded in the same model (not strictly possible
    # given single-file parsing, but keeps things tidy).
    for offset, comp in enumerate(comps):
        ttype = 0x91000000 + offset
        lines.append(f"node atomic {comp.camel_name} {{")
        lines.append(f"    tipc type=0x{ttype:08x} instance=0")
        if comp.ports:
            lines.append("    ports {")
            for p in [pp for pp in comp.ports if pp.direction == "in"]:
                lines.append(
                    f"        receiver {_port_local_name(p)} requires {p.interface}"
                )
            for p in [pp for pp in comp.ports if pp.direction == "out"]:
                lines.append(
                    f"        sender   {_port_local_name(p)} provides {p.interface}"
                )
            lines.append("    }")
        lines.append("}")
        lines.append("")

    lines.append("composition TornadoSystem {")
    for comp in comps:
        lines.append(f"    prototype {comp.camel_name} {comp.name}")
    lines.append("")
    # Connections: for each topic, every (pub, sub) pair.
    written = 0
    skipped: list[str] = []
    for iface in sorted(topics):
        pubs = topics[iface]["out"]
        subs = topics[iface]["in"]
        if not pubs or not subs:
            skipped.append(iface)
            continue
        lines.append(f"    // topic {iface}: {len(pubs)} publisher(s) × {len(subs)} subscriber(s)")
        for pub_comp, pub_port in pubs:
            for sub_comp, sub_port in subs:
                lines.append(
                    f"    connect {pub_comp.name}.{_port_local_name(pub_port)} "
                    f"to {sub_comp.name}.{_port_local_name(sub_port)}"
                )
                written += 1
    lines.append("}")
    lines.append("")
    if skipped:
        lines.append(
            f"// {written} connect lines emitted; {len(skipped)} topic(s) had only"
            f" producers or only consumers among the translated components"
            f" (external publishers/subscribers not modeled this round)."
        )
    return "\n".join(lines) + "\n"


def import_all(
    tornado_root: Path,
    vendor_root: Path,
) -> dict[str, list[Path]]:
    """Run the full fusée pipeline against `tornado_root` (e.g. up/tornado/).

    Emits, under `vendor_root/system/`:
      - signals/<cat>/package.art   (one per signal category)
      - components/<comp>.art       (one per component with a
        vehicle_os_signal_junction)
      - system.art                  (TornadoSystem composition)

    Returns a dict with 'signals', 'components', 'system' keys.
    """
    system_root = vendor_root / "system"
    signal_paths = import_platform(
        tornado_root / "signals", system_root / "signals"
    )
    # Index signal messages (Foo → category) for component emission. Not
    # currently used by the component emitter but plumbed so future work
    # can resolve cross-package message types.
    parsed_signals: dict[str, list[ProtoFile]] = {
        d.name: _category_proto_files(d)
        for d in sorted((tornado_root / "signals").iterdir())
        if d.is_dir()
    }
    msg_index = _index_message_origins(parsed_signals)

    component_paths, comps = import_components(
        tornado_root / "app" / "onboard",
        system_root / "components",
        platform_message_index=msg_index,
    )
    system_path = system_root / "system.art"
    system_path.write_text(emit_system_art(comps))
    return {
        "signals": signal_paths,
        "components": component_paths,
        "system": [system_path],
    }
