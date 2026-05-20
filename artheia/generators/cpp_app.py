"""Generate a C++14 application scaffold from an Artheia vendor system fragment.

Three-slice layout matches `up/mosaic-eng-ref/...climate_arbitrator`:

    applications/<vendor>/
    ├── CMakeLists.txt                                (regen)
    ├── core/
    │   └── <Node>Inputs.hh                           (regen, slice 1)
    └── app/
        ├── <Node>.hh                                 (regen, slice 2)
        ├── <Node>.cc                                 (regen, slice 2)
        ├── <Node>_main.cc                            (regen, slice 2)
        └── impl/
            └── <Node>_handlers.cc                    (FIRST-TIME-ONLY, slice 3)

Slice 3 is the only file the user is expected to hand-edit. The
generator refuses to overwrite it; delete the file if you want a
fresh stub.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..model import parse_file


_TEMPLATES = Path(__file__).parent / "templates" / "cpp_app"


# ---- model helpers ---------------------------------------------------------

def _snake_case(s: str) -> str:
    """`OddPathMonitor` -> `odd_path_monitor`."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _format_tipc_type(tipc) -> str:
    """TipcAddress.type is an int parsed from hex; render back as 0xNN."""
    t = getattr(tipc, "type", None)
    if isinstance(t, int):
        return f"0x{t:08x}"
    return str(t)


def _cxx_type_for(param_type: str) -> str:
    """.art primitive -> C++ scalar."""
    return {
        "bool":   "bool",
        "uint32": "uint32_t",
        "uint64": "uint64_t",
        "int32":  "int32_t",
        "int64":  "int64_t",
        "float":  "float",
        "double": "double",
        "string": "std::string",
    }.get(param_type, "uint32_t")


def _cxx_default(param_type: str, value) -> str:
    """Render the .art default for the C++ initializer."""
    if param_type == "string":
        return f'"{value}"'
    if param_type == "bool":
        return "true" if str(value).lower() in ("true", "1") else "false"
    if param_type == "float":
        s = str(value)
        return s if "." in s or "e" in s.lower() else f"{s}.0f"
    return str(value)


def _pdu_from_iface(iface_name: str) -> str:
    """`EML_01_Iface` -> `EML_01`. Returns None if the interface doesn't
    follow the convention (e.g. Status, which is a clientServer)."""
    if iface_name.endswith("_Iface"):
        return iface_name[: -len("_Iface")]
    return ""


@dataclass
class _PortInfo:
    name: str               # snake-case name as written in .art
    interface: str          # interface type name (e.g. EML_01_Iface)
    message: str = ""       # PDU type for receiver/sender (e.g. EML_01)
    callback: str = ""      # method name on the app class
    call: str = ""          # for client ports: helper method name
    call_dispatch: str = ""


@dataclass
class _ParamInfo:
    name: str
    type: str
    cxx_type: str
    default: str
    macro: str              # SCREAMING_SNAKE


@dataclass
class _NodeInfo:
    name: str               # PascalCase
    snake: str              # snake_case
    tipc_type: str
    tipc_instance: int
    recv_ports: list[_PortInfo] = field(default_factory=list)
    send_ports: list[_PortInfo] = field(default_factory=list)
    client_ports: list[_PortInfo] = field(default_factory=list)
    server_ports: list[_PortInfo] = field(default_factory=list)
    params: list[_ParamInfo] = field(default_factory=list)


def _harvest_node(node) -> _NodeInfo:
    """Convert a textX NodeDecl into the data view the templates expect."""
    info = _NodeInfo(
        name=node.name,
        snake=_snake_case(node.name),
        tipc_type=_format_tipc_type(node.tipc),
        tipc_instance=int(getattr(node.tipc, "instance", 0) or 0),
    )
    for port in getattr(node, "ports", []) or []:
        kind = port.__class__.__name__  # ReceiverPort / SenderPort / ClientPort / ServerPort
        iface = getattr(port, "iface", None) or getattr(port, "interface", None)
        iface_name = iface.name if iface is not None else "?"
        msg = _pdu_from_iface(iface_name)
        pi = _PortInfo(
            name=port.name,
            interface=iface_name,
            message=msg,
            callback=f"on_{port.name}",
            call=f"call_{port.name}",
            call_dispatch=f"dispatch_{port.name}",
        )
        if kind == "ReceiverPort":
            info.recv_ports.append(pi)
        elif kind == "SenderPort":
            info.send_ports.append(pi)
        elif kind == "ClientPort":
            info.client_ports.append(pi)
        elif kind == "ServerPort":
            info.server_ports.append(pi)
    for p in getattr(node, "params", []) or []:
        ptype = getattr(p, "type", "uint32")
        # NodeParam.default is a ParamLiteral wrapper; the actual scalar
        # lives on ParamLiteral.value.
        default_obj = getattr(p, "default", None)
        default = getattr(default_obj, "value", default_obj) if default_obj is not None else 0
        info.params.append(_ParamInfo(
            name=p.name,
            type=ptype,
            cxx_type=_cxx_type_for(ptype),
            default=_cxx_default(ptype, default),
            macro=p.name.upper(),
        ))
    return info


def _harvest_nodes(art_paths: Iterable[Path]) -> list[_NodeInfo]:
    nodes: list[_NodeInfo] = []
    seen: set[str] = set()
    for p in art_paths:
        model = parse_file(str(p))
        for el in model.elements:
            if el.__class__.__name__ != "NodeDecl":
                continue
            if not getattr(el, "ports", None):
                continue  # forward-decl stub with no ports → not the real node
            if el.name in seen:
                continue
            info = _harvest_node(el)
            # Prefer the decl with the most ports if we see the node twice.
            seen.add(el.name)
            nodes.append(info)
    return nodes


# ---- writers ---------------------------------------------------------------

def _render(env: Environment, template_name: str, ctx: dict) -> str:
    return env.get_template(template_name).render(**ctx)


def _write(path: Path, content: str, *, overwrite: bool) -> str:
    """Write content to path. Returns one of {'wrote', 'skipped-exists'}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return "skipped-exists"
    path.write_text(content)
    return "wrote"


# ---- public API ------------------------------------------------------------

def generate(
    vendor_root: str | Path,
    out_dir: str | Path,
    *,
    namespace: str = "",
    project_name: str = "",
) -> dict[str, list[str]]:
    """Walk a vendor system tree, emit a C++14 application scaffold.

    Returns a dict {kind: [paths]} listing every file written or skipped,
    grouped by slice for the caller to summarize.
    """
    vendor_root = Path(vendor_root)
    out_dir = Path(out_dir)

    components_dir = vendor_root / "system" / "components"
    if not components_dir.is_dir():
        raise ValueError(f"no components directory at {components_dir}")

    art_paths = sorted(components_dir.rglob("*.art"))
    if not art_paths:
        raise ValueError(f"no .art files under {components_dir}")

    nodes = _harvest_nodes(art_paths)
    if not nodes:
        raise ValueError("no NodeDecl with ports found in vendor components")

    # Defaults derived from the vendor path.
    vendor_name = vendor_root.name
    if not namespace:
        namespace = vendor_name.replace("-", "_")
    if not project_name:
        project_name = vendor_name

    # Set of PDU includes the templates emit. Convention: `<Pdu>.pb.h`
    # under <PSP>/proto/shared/. We treat every receiver/sender message
    # as a possible PDU; non-PDU interfaces (e.g. Status) produce no
    # include because their `message` field is empty.
    pdu_set: set[str] = set()
    for n in nodes:
        for p in n.recv_ports + n.send_ports:
            if p.message:
                pdu_set.add(p.message)
    pdus_referenced = sorted(pdu_set)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        keep_trailing_newline=True,
        trim_blocks=False,
        undefined=StrictUndefined,
    )

    results: dict[str, list[str]] = {
        "wrote": [],
        "skipped-exists": [],
    }

    for node in nodes:
        ctx_node = {
            "node": node,
            "namespace": namespace,
            "pdus_referenced": pdus_referenced,
            "source_file": str(art_paths[0].relative_to(vendor_root.parent)),
        }

        # Slice 1: core/<Node>Inputs.hh
        target = out_dir / "core" / f"{node.name}Inputs.hh"
        status = _write(target, _render(env, "Inputs.hh.j2", ctx_node), overwrite=True)
        results[status].append(str(target))

        # Slice 2: app/<Node>.hh, app/<Node>.cc, app/<Node>_main.cc
        for tmpl, fname in [
            ("App.hh.j2",  f"{node.name}.hh"),
            ("App.cc.j2",  f"{node.name}.cc"),
            ("main.cc.j2", f"{node.name}_main.cc"),
        ]:
            target = out_dir / "app" / fname
            status = _write(target, _render(env, tmpl, ctx_node), overwrite=True)
            results[status].append(str(target))

        # Slice 3: app/impl/<Node>_handlers.cc — write-once
        target = out_dir / "app" / "impl" / f"{node.name}_handlers.cc"
        status = _write(target, _render(env, "handlers.cc.j2", ctx_node), overwrite=False)
        results[status].append(str(target))

    # CMakeLists.txt (regen) — one per vendor app, lists every node.
    cmake_ctx = {
        "project_name": project_name,
        "namespace": namespace,
        "nodes": nodes,
        # Caller can extend; for now we always include `shared/` and
        # leave per-bus dirs empty. Once gen-platform-protos starts
        # emitting platform-side protos those land here.
        "proto_buses": [],
        "source_file": str(art_paths[0].relative_to(vendor_root.parent)),
    }
    target = out_dir / "CMakeLists.txt"
    status = _write(target, _render(env, "CMakeLists.txt.j2", cmake_ctx), overwrite=True)
    results[status].append(str(target))

    return results
