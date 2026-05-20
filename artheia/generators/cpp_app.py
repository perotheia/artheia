"""Generate a C++14 application scaffold from an Artheia vendor system fragment.

Routing info (per port -> bus address) is joined from one or more
netgraph.json files passed via `netgraph_paths`. The naming convention
`<Pdu>_Iface` (e.g. `EML_01_Iface`) maps the interface name to the PDU
name, which keys the netgraph.

Generated apps:
  1. connect to the gateway via GwClient
  2. loop on recv_signal()
  3. dispatch by can_id (CAN) or slot_id+channel_idx (FlexRay) to the
     right on_<port>() callback
  4. nanopb-decode the proto wire bytes into shared_<Pdu> and pass to
     the user handler


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
    # Routing — filled in from netgraph.json. Exactly one of can_id /
    # slot_id is set, or both empty if the PDU has no wire route (e.g.
    # client status_query → Status RPC, not a bus PDU).
    can_id: int = -1        # CAN: 11-bit ID, used to filter recv_signal
    slot_id: int = -1       # FlexRay: cycle slot
    channel_idx: int = -1   # FlexRay: 0=A, 1=B
    bus_kind: str = ""      # "can" | "flexray" | ""
    # Nanopb resolution — filled in from the per-PSP .proto files.
    proto_pkg: str = ""     # `shared`, `mlbevo_gen2`, `can_kcan`, ...
    proto_dir: str = ""     # `shared`, `flexray`, `can/kcan`, ...
    cxx_struct: str = ""    # e.g. `shared_ACC_07`, `mlbevo_gen2_EML_01`


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


# ---- netgraph load + port routing ----------------------------------------


def _load_netgraph(paths: Iterable[str | Path]) -> dict[str, dict]:
    """Merge every netgraph.json into a single {pdu_name: route_dict}.

    Each route_dict carries `bus_kind` plus the bus-specific keys
    (`can_id` for CAN, `slot_id`+`channel_idx`+`cycle` for FlexRay).
    If a PDU appears in multiple netgraphs the first occurrence wins
    (caller's responsibility to order intentionally).
    """
    import json

    merged: dict[str, dict] = {}
    for p in paths:
        ng = json.loads(Path(p).read_text())
        kind = ng.get("bus_kind", "")
        for pdu, route in ng.get("routes", {}).items():
            if pdu in merged:
                continue
            entry: dict = {"bus_kind": kind}
            if kind == "can":
                entry["can_id"] = route.get("can_id", -1)
                entry["extended_id"] = route.get("extended_id", False)
                entry["dlc"] = route.get("dlc", 0)
            elif kind == "flexray":
                # Pick the first frame trigger as the canonical route.
                triggers = route.get("frame_triggers") or []
                if triggers:
                    t = triggers[0]
                    entry["slot_id"] = t.get("slot_id", -1)
                    entry["channel_idx"] = t.get("channel_idx", -1)
                    entry["cycle"] = t.get("cycle", -1)
            merged[pdu] = entry
    return merged


def _annotate_ports(node: "_NodeInfo", routes: dict[str, dict]) -> None:
    """Decorate every receiver/sender port with its bus address."""
    for port in node.recv_ports + node.send_ports:
        if not port.message:
            continue
        route = routes.get(port.message)
        if not route:
            continue
        port.bus_kind = route["bus_kind"]
        if route["bus_kind"] == "can":
            port.can_id = route.get("can_id", -1)
        elif route["bus_kind"] == "flexray":
            port.slot_id = route.get("slot_id", -1)
            port.channel_idx = route.get("channel_idx", -1)


def _resolve_proto_locations(
    node: "_NodeInfo", psp_proto_root: Path,
) -> None:
    """For each receiver/sender port whose interface names a PDU, find
    that PDU's .proto under `psp_proto_root/<dir>/<Pdu>.proto`, parse
    its `package` line, and stash both into the port. Subsequent
    templates use this to write the right include path and the right
    C struct typename.

    Search order: shared/ first (cross-bus PDUs), then flexray/, then
    can/<bus>/ in alphabetical order. The first hit wins.
    """
    import re

    if not psp_proto_root.is_dir():
        return

    # Build PDU → (dir, package) index once.
    pkg_re = re.compile(r"^package\s+([A-Za-z0-9_]+)\s*;", re.MULTILINE)
    locs: dict[str, tuple[str, str]] = {}
    candidate_subdirs = ["shared"]
    flexray_dir = psp_proto_root / "flexray"
    if flexray_dir.is_dir():
        candidate_subdirs.append("flexray")
    can_root = psp_proto_root / "can"
    if can_root.is_dir():
        for bus in sorted(d.name for d in can_root.iterdir() if d.is_dir()):
            candidate_subdirs.append(f"can/{bus}")

    for sub in candidate_subdirs:
        sub_dir = psp_proto_root / sub
        if not sub_dir.is_dir():
            continue
        for f in sub_dir.glob("*.proto"):
            pdu = f.stem
            if pdu in locs:
                continue
            try:
                text = f.read_text()
            except OSError:
                continue
            m = pkg_re.search(text)
            pkg = m.group(1) if m else "shared"
            locs[pdu] = (sub, pkg)

    for port in node.recv_ports + node.send_ports:
        if not port.message:
            continue
        loc = locs.get(port.message)
        if not loc:
            continue
        port.proto_dir, port.proto_pkg = loc
        port.cxx_struct = f"{port.proto_pkg}_{port.message}"


# ---- public API ------------------------------------------------------------

def generate(
    vendor_root: str | Path,
    out_dir: str | Path,
    *,
    namespace: str = "",
    project_name: str = "",
    netgraph_paths: Iterable[str | Path] = (),
    psp_proto_root: str | Path | None = None,
) -> dict[str, list[str]]:
    """Walk a vendor system tree, emit a C++14 application scaffold.

    `netgraph_paths` — list of netgraph.json files (typically from
    `artheia gen-netgraph-partition`, one per bus). The generator joins
    each receiver port with its bus address (can_id or slot_id) so the
    emitted dispatch loop can route incoming GwMessageHeader frames to
    the right `on_<port>()` callback.

    Returns a dict {kind: [paths]} listing every file written or skipped,
    grouped by slice for the caller to summarize.
    """
    vendor_root = Path(vendor_root)
    out_dir = Path(out_dir)

    # Load netgraphs first so every node gets routed ports.
    routes = _load_netgraph(netgraph_paths)

    components_dir = vendor_root / "system" / "components"
    if not components_dir.is_dir():
        raise ValueError(f"no components directory at {components_dir}")

    art_paths = sorted(components_dir.rglob("*.art"))
    if not art_paths:
        raise ValueError(f"no .art files under {components_dir}")

    nodes = _harvest_nodes(art_paths)
    if not nodes:
        raise ValueError("no NodeDecl with ports found in vendor components")

    proto_root_path = Path(psp_proto_root) if psp_proto_root else None
    for node in nodes:
        _annotate_ports(node, routes)
        if proto_root_path is not None:
            _resolve_proto_locations(node, proto_root_path)

    # Defaults derived from the vendor path.
    vendor_name = vendor_root.name
    if not namespace:
        namespace = vendor_name.replace("-", "_")
    if not project_name:
        project_name = vendor_name

    # Set of PDU includes the templates emit. Resolved per port from
    # the .proto files under psp_proto_root: each receiver/sender port
    # gets an include like `<proto_dir>/<Pdu>.pb.h` (e.g.
    # `shared/ACC_07.pb.h` or `flexray/EML_01.pb.h`). Ports without a
    # resolved location are skipped (their handlers would not compile
    # — the template emits TODO comments instead).
    include_set: set[str] = set()
    for n in nodes:
        for p in n.recv_ports + n.send_ports:
            if p.message and p.proto_dir:
                include_set.add(f"{p.proto_dir}/{p.message}.pb.h")
    pdu_includes = sorted(include_set)
    pdus_referenced = pdu_includes  # back-compat alias used by templates

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
