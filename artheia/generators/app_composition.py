"""gen-app-composition: per-process app projects from a composition.

For each `composition C { ... on process P ... }` partition, emits a
self-contained CMake project under `<out>/<C>_<P>/` containing:

    main.cc          — generated boot scaffold for THIS process
    CMakeLists.txt   — links the existing demo_runtime

The boot scaffold uses the per-node <Node>Inputs convention. Each
local node is constructed with an Inputs struct whose members come
from the composition's `connect` lines:

  * `connect local.port → other.port` where `other` is local:
        port → LocalRef<peer_node_type>
  * `connect local.port → other.port` where `other` is remote:
        port → RemoteRef<peer_node_type, tipc_type, tipc_inst>

Inbound dispatch (register_cast / register_call) is derived from the
`connect` edges that TERMINATE at a local node's port:

  * `connect remote.X → local.recv_port` where recv_port requires a
    `senderReceiver Iface { data Msg }` → register_cast<Msg>(local).
  * `connect remote.X → local.server_port` where server_port provides
    a `clientServer Iface { operation Op() returns Reply }`:
        register_call<Req, Reply>(local).
    (Where `Req` is the operation's request message — for the demo
    CounterSrv has empty-param operations so we use the operation
    name as the request type.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from textx import metamodel_from_file  # noqa: F401  (kept: _GRAMMAR path uses it for callers/tools)


_GRAMMAR = (Path(__file__).resolve().parent.parent /
            "grammar" / "artheia.tx")


# ---- model harvest ------------------------------------------------------

@dataclass
class _Port:
    """A port on a NodeDecl, as seen by the generator."""
    name: str
    kind: str          # "sender" | "receiver" | "client" | "server"
    iface_name: str    # e.g. "IncIface", "CounterSrv"


@dataclass
class _Node:
    name: str          # C++ class name (== .art NodeDecl name)
    snake: str
    ports: List[_Port] = field(default_factory=list)
    # Annotations harvested from the NodeDecl.
    kick_off: bool = False
    requires_timers: bool = False


@dataclass
class _Iface:
    name: str
    kind: str          # "senderReceiver" | "clientServer"
    # For senderReceiver: the single `data X name` element's message type.
    data_msg: Optional[str] = None
    # For clientServer: list of (op_name, request_type, reply_type).
    # If the .art doesn't declare an `in <name>:<MsgType>` param, we
    # synthesize the request type from the op name ("Get" → "Get").
    ops: List[Tuple[str, str, str]] = field(default_factory=list)


@dataclass
class _Proto:
    name: str          # prototype name in composition
    node_type: str
    snake: str
    process: str
    tipc_type_hex: str
    tipc_instance: int


@dataclass
class _Composition:
    name: str
    protos: List[_Proto] = field(default_factory=list)
    connects: List[Tuple[str, str, str, str]] = field(default_factory=list)
    # ^ (src_proto_name, src_port, dst_proto_name, dst_port)
    # Processes this composition OWNS (one project is generated per
    # entry). Remote peer protos folded in from cluster connects carry
    # their own `process` tag but are NOT generated here — only their
    # RemoteRef is emitted into our process. Set in _harvest before the
    # cluster merge.
    own_processes: List[str] = field(default_factory=list)
    # The nanopb header to #include, derived from the source .art package
    # path: `system.demo` → "system/demo/demo.pb.h" (matches the layout
    # gen-proto-package writes under platform/proto/).
    proto_include: str = "demo/system/system.pb.h"


def _snake(name: str) -> str:
    out: List[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _harvest(art_path: Path, composition_name: str
              ) -> Tuple[_Composition, Dict[str, _Node], Dict[str, _Iface],
                          str]:
    """Returns (composition, nodes_by_name, ifaces_by_name, proto_pkg).

    proto_pkg is the .art package name with dots → underscores, used to
    derive C struct names (`demo.system` → `demo_system` → message
    `Inc` → C type `demo_system_Inc`).
    """
    # Package-aware load (#378): parse_file merges package.art +
    # component.art, so a composition harvested from component.art sees
    # the NodeDecls / interfaces declared in the sibling package.art.
    # A raw single-file metamodel parse would fail on cross-file refs
    # (e.g. `prototype CounterNode counter` where CounterNode lives in
    # package.art).
    from artheia.model import parse_file
    model = parse_file(str(art_path))

    # Proto C-type prefix must match what gen-proto-package emits: the
    # libc-safe package rewrite (leading `system` → `services` to dodge
    # libc's system()), dots → underscores. e.g. .art package
    # `system.demo` → proto package `services.demo` → C type
    # `services_demo_Inc`. A raw replace(".","_") here would emit
    # `system_demo_Inc` and fail to link against the generated nanopb.
    from artheia.generators.proto import _proto_package_name
    proto_pkg = _proto_package_name(model.name or "artheia").replace(".", "_")

    nodes: Dict[str, _Node] = {}
    ifaces: Dict[str, _Iface] = {}

    for el in model.elements:
        kind = el.__class__.__name__
        if kind == "NodeDecl":
            # textX leaves ?= flags as bool.
            n = _Node(
                name=el.name,
                snake=_snake(el.name),
                kick_off=bool(getattr(el, "kick_off", False)),
                requires_timers=bool(getattr(el, "requires_timers", False)),
            )
            for p in getattr(el, "ports", []) or []:
                pk = p.__class__.__name__
                kind_str = {
                    "SenderPort":   "sender",
                    "ReceiverPort": "receiver",
                    "ClientPort":   "client",
                    "ServerPort":   "server",
                }.get(pk, "?")
                # both 'iface' and 'interface' are used in the grammar
                # (different rules); each port has its own iface ref.
                iface = getattr(p, "iface", None) or getattr(p, "interface", None)
                iface_name = iface.name if iface is not None else "?"
                n.ports.append(_Port(name=p.name, kind=kind_str,
                                       iface_name=iface_name))
            nodes[n.name] = n
        elif kind == "SenderReceiverInterface":
            data_msg = None
            for d in getattr(el, "data", []) or []:
                # First `data` wins. Multi-data interfaces aren't used
                # in the demo but the model permits them.
                t = d.type
                if t is not None:
                    data_msg = getattr(t, "name", None) or getattr(t, "ref",
                                                                    None)
                    if hasattr(data_msg, "name"):
                        data_msg = data_msg.name
                    break
            ifaces[el.name] = _Iface(name=el.name, kind="senderReceiver",
                                       data_msg=data_msg)
        elif kind == "ClientServerInterface":
            ops: List[Tuple[str, str, str]] = []
            for op in getattr(el, "operations", []) or []:
                op_name = op.name
                # Request type: first `in` param, or fall back to the
                # operation name itself when the param list is empty.
                req_type = None
                for param in getattr(op, "params", []) or []:
                    if param.direction == "in":
                        t = param.type
                        req_type = getattr(t, "name", None)
                        if req_type is None and hasattr(t, "ref"):
                            req_type = t.ref.name
                        break
                if req_type is None:
                    req_type = op_name  # synthesize: operation Get → message Get
                reply_type = None
                if getattr(op, "returns", None) is not None:
                    reply_type = op.returns.name
                ops.append((op_name, req_type, reply_type or "void"))
            ifaces[el.name] = _Iface(name=el.name, kind="clientServer",
                                       ops=ops)

    # Composition.
    comp_el = None
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl" and el.name == composition_name:
            comp_el = el
            break
    if comp_el is None:
        raise ValueError(f"no composition {composition_name!r} in {art_path}")

    # Flatten nested-composition refs so an inner `composition Foo bar`
    # contributes its prototypes + connects to this composition's view.
    # Inner names appear verbatim at parent scope (no instance-prefixing).
    from artheia.model import flatten_composition
    proto_decls, connect_decls = flatten_composition(comp_el)

    comp = _Composition(name=composition_name)
    for el in proto_decls:
        node = el.type
        proc = getattr(el, "process", None) or "default"
        tipc_type = node.tipc.type
        if isinstance(tipc_type, int):
            tipc_type_hex = f"0x{tipc_type:x}"
        else:
            tipc_type_hex = str(tipc_type)
        comp.protos.append(_Proto(
            name=el.name,
            node_type=node.name,
            snake=_snake(node.name),
            process=proc,
            tipc_type_hex=tipc_type_hex,
            tipc_instance=int(getattr(node.tipc, "instance", 0) or 0),
        ))
    for el in connect_decls:
        s = el.source
        t = el.target
        comp.connects.append((s.proto.name, s.port,
                              t.proto.name, t.port))

    # The processes this composition owns — captured BEFORE the cluster
    # merge folds in remote peer protos (which carry their own process
    # tag we must not generate a project for).
    comp.own_processes = sorted({p.process for p in comp.protos})

    # nanopb include path from the source package: system.demo →
    # system/demo/demo.pb.h (gen-proto-package writes <pkg-path>/<leaf>.proto
    # under platform/proto/, and nanopb emits the sibling .pb.h).
    _src_parts = (model.name or "artheia").split(".")
    comp.proto_include = "/".join(_src_parts + [f"{_src_parts[-1]}.pb.h"])

    # ---- cross-composition (cluster) connects (#378) -------------------
    # A cluster wires prototypes that live in DIFFERENT member
    # compositions, e.g.:
    #   cluster Applications {
    #       composition Demo3WayP1 p1   # owns counter
    #       composition Demo3WayP2 p2   # owns observer
    #       connect observer.counter_call to counter.srv
    #   }
    # When generating P2, `counter` lives in P1 — it's a REMOTE peer.
    # The composition-scoped harvest above doesn't see it, so pull in:
    #   (a) every cluster connect touching one of THIS composition's
    #       prototypes, and
    #   (b) the peer prototype it references (from the sibling member
    #       composition) as a remote proto, so its node_type + tipc
    #       address are known for the RemoteRef.
    _merge_cluster_connects(model, composition_name, comp)

    return comp, nodes, ifaces, proto_pkg


def _all_composition_protos(model) -> "Dict[str, _Proto]":
    """Index every prototype across all compositions in *model* by name.

    Prototype names are globally unique in the artheia model (a
    prototype identifies one node / TIPC endpoint), so a flat name→proto
    map is sufficient to resolve cluster connects that span compositions.
    """
    from artheia.model import flatten_composition
    out: "Dict[str, _Proto]" = {}
    for el in getattr(model, "elements", []):
        if el.__class__.__name__ != "CompositionDecl":
            continue
        proto_decls, _ = flatten_composition(el)
        for pd in proto_decls:
            node = pd.type
            tt = node.tipc.type
            tt_hex = f"0x{tt:x}" if isinstance(tt, int) else str(tt)
            out[pd.name] = _Proto(
                name=pd.name,
                node_type=node.name,
                snake=_snake(node.name),
                process=getattr(pd, "process", None) or "default",
                tipc_type_hex=tt_hex,
                tipc_instance=int(getattr(node.tipc, "instance", 0) or 0),
            )
    return out


def _merge_cluster_connects(model, composition_name: str,
                            comp: "_Composition") -> None:
    """Fold cluster-level connects that touch *comp*'s prototypes into
    *comp* (connects + the remote peer prototypes they reference)."""
    own_names = {p.name for p in comp.protos}
    if not own_names:
        return
    all_protos = _all_composition_protos(model)
    have = set(own_names)
    have_connects = {(s, sp, d, dp) for s, sp, d, dp in comp.connects}

    for el in getattr(model, "elements", []):
        if el.__class__.__name__ != "ClusterDecl":
            continue
        for c in getattr(el, "elements", []):
            src = getattr(c, "source", None)
            tgt = getattr(c, "target", None)
            if src is None or tgt is None:
                continue
            s_proto = getattr(getattr(src, "proto", None), "name", None)
            t_proto = getattr(getattr(tgt, "proto", None), "name", None)
            if s_proto is None or t_proto is None:
                continue
            # Only connects that touch one of THIS composition's protos.
            if s_proto not in own_names and t_proto not in own_names:
                continue
            edge = (s_proto, src.port, t_proto, tgt.port)
            if edge not in have_connects:
                comp.connects.append(edge)
                have_connects.add(edge)
            # Bring in the peer proto (the one not owned here) so the
            # renderer can emit its RemoteRef (node_type + tipc address).
            for peer in (s_proto, t_proto):
                if peer not in have and peer in all_protos:
                    comp.protos.append(all_protos[peer])
                    have.add(peer)


# ---- per-port helpers --------------------------------------------------

def _resolve_port(node: _Node, port_name: str) -> _Port:
    for p in node.ports:
        if p.name == port_name:
            return p
    raise ValueError(f"node {node.name}: no port {port_name}")


def _cxx_type_for(proto_pkg: str, msg: str) -> str:
    """`demo_system` + `Inc` → `demo_system_Inc`."""
    return f"{proto_pkg}_{msg}"


# ---- main.cc renderer --------------------------------------------------

def _render_main(comp: _Composition,
                  process: str,
                  nodes: Dict[str, _Node],
                  ifaces: Dict[str, _Iface],
                  proto_pkg: str) -> str:
    local = [p for p in comp.protos if p.process == process]
    local_names = {p.name for p in local}

    # Per-prototype peer maps:
    #   outbound_peer[proto.port] = (peer_proto, peer_port)
    # built from the connect list. We use this when constructing each
    # local prototype's Inputs struct.
    outbound_peer: Dict[Tuple[str, str], Tuple[str, str]] = {}
    inbound_peer:  Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for src_name, src_port, dst_name, dst_port in comp.connects:
        outbound_peer[(src_name, src_port)] = (dst_name, dst_port)
        inbound_peer.setdefault((dst_name, dst_port), []).append(
            (src_name, src_port))

    # Which remote prototypes do local prototypes reach out to?
    needed_remote_names = set()
    for (src_name, _sp), (dst_name, _dp) in outbound_peer.items():
        if src_name in local_names and dst_name not in local_names:
            needed_remote_names.add(dst_name)
    remote_protos = [p for p in comp.protos
                      if p.process != process and p.name in needed_remote_names]

    proto_by_name = {p.name: p for p in comp.protos}

    # Per local prototype, build the Inputs struct contents.
    # Each port that's a CLIENT (call/cast outbound) gets an Inputs field
    # whose type is the peer's ref type (Local or Remote). Sender ports
    # do the same (outbound cast). Receiver and server ports are PASSIVE
    # — they don't need a ref; instead, when the local prototype owns
    # them, we register inbound dispatch on the mux.
    def ref_type_for(peer: _Proto, is_local: bool) -> str:
        if is_local:
            return f"demo::runtime::LocalRef<demo::{peer.node_type}>"
        else:
            return (f"demo::runtime::RemoteRef<demo::{peer.node_type}, "
                    f"{peer.tipc_type_hex}u, {peer.tipc_instance}u>")

    def ref_var_for(peer: _Proto, is_local: bool) -> str:
        # Used by main.cc to name the ref variable in scope.
        return f"{peer.snake}_ref" if not is_local else f"{peer.snake}_local_ref"

    lines: List[str] = []
    lines.append(f"// AUTO-GENERATED by `artheia gen-app-composition` — DO NOT EDIT")
    lines.append(f"// composition: {comp.name}")
    lines.append(f"// process:     {process}")
    lines.append("//")
    lines.append("// Boot scaffold: TimerService, TipcMux, RemoteRef connects,")
    lines.append("// local node Inputs construction, inbound dispatch registration,")
    lines.append("// start/kick_off, run until deadline, stop(\"normal\").")
    lines.append("")
    lines.append('#include "GenServer.hh"')
    lines.append('#include "Logger.hh"')
    lines.append('#include "NodeRef.hh"')
    lines.append('#include "TimerService.hh"')
    lines.append('#include "TipcMux.hh"')
    lines.append('#include "demo_codecs.hh"')
    lines.append("")
    seen_hh = set()
    for p in local + remote_protos:
        inc = f"{p.snake}.hh"
        if inc in seen_hh:
            continue
        seen_hh.add(inc)
        lines.append(f'#include "{inc}"')
    lines.append("")
    lines.append(f'#include "{comp.proto_include}"')
    lines.append("")
    lines.append("#include <atomic>")
    lines.append("#include <chrono>")
    lines.append("#include <csignal>")
    lines.append("#include <cstdio>")
    lines.append("#include <cstdlib>")
    lines.append("#include <thread>")
    lines.append("")
    lines.append("namespace {")
    lines.append("std::atomic<bool> g_shutdown{false};")
    lines.append("void on_sig(int) noexcept { g_shutdown.store(true); }")
    lines.append("}")
    lines.append("")
    lines.append("int main() {")
    lines.append("    std::signal(SIGINT,  on_sig);")
    lines.append("    std::signal(SIGTERM, on_sig);")
    lines.append("    auto logger = platform::runtime::MakeConsoleLogger();")
    lines.append(f'    logger->info("=== {process} start (gen) ===");')
    lines.append("")
    lines.append("    demo::runtime::TimerService timers;")
    lines.append("    demo::runtime::TipcMux mux;")
    lines.append("")

    # Remote refs (one per peer this process reaches outward to).
    for p in remote_protos:
        lines.append(f"    // remote: {p.node_type} on process {p.process}")
        lines.append(
            f"    demo::runtime::RemoteRef<demo::{p.node_type}, "
            f"{p.tipc_type_hex}u, {p.tipc_instance}u> {p.snake}_ref;"
        )
        lines.append(f"    if (!{p.snake}_ref.connect(3000)) {{")
        lines.append(f'        logger->error("{process}: failed to '
                      f'connect to {p.node_type} (tipc {p.tipc_type_hex}:'
                      f'{p.tipc_instance})");')
        lines.append("        return 1;")
        lines.append("    }")
        lines.append(f"    mux.watch_remote_ref({p.snake}_ref);")
        lines.append("")

    # Local refs for prototypes whose ports are USED as outbound from
    # another local prototype. (If driver_p1 calls counter_p1.srv,
    # main needs a LocalRef<CounterNode>(counter_p1_var) to pass into
    # driver_p1's Inputs.) We track which local prototypes need a
    # LocalRef wrapper around them.
    needs_local_ref = set()
    for (src_name, _sp), (dst_name, _dp) in outbound_peer.items():
        if src_name in local_names and dst_name in local_names:
            needs_local_ref.add(dst_name)

    # Construct each local node, in order. First the ones that have no
    # outbound ports to other locals (so when we hit a node that
    # depends on a LocalRef, the target already exists).
    # For the demo: counter is constructed first, then driver/ticker
    # which depend on it.
    # Simple topological-ish order: sort so locals with no outbound
    # client/sender ports go first.
    def outbound_count(p: _Proto) -> int:
        n = nodes[p.node_type]
        cnt = 0
        for port in n.ports:
            if port.kind in ("client", "sender"):
                if (p.name, port.name) in outbound_peer:
                    cnt += 1
        return cnt

    local_sorted = sorted(local, key=outbound_count)

    # First pass: construct nodes with no outbound deps, and emit a
    # LocalRef for each one that needs to be referenced from elsewhere.
    for p in local_sorted:
        n = nodes[p.node_type]
        # Build the Inputs struct literal. Members:
        #  - logger (always)
        #  - timers (if any port uses send_after — for the demo all
        #    nodes except CounterNode get it)
        #  - one field per outbound port (client/sender) from this node
        # Annotations drive: needs_timers, kick_off, trace_on.
        # All three come straight from the NodeDecl in .art now.
        needs_timers = n.requires_timers

        # Decide the C++ instantiation. Templated nodes need <ref types>;
        # non-templated nodes (CounterNode) just use the bare class.
        outbound_ports = [port for port in n.ports
                           if port.kind in ("client", "sender")]
        is_templated = bool(outbound_ports)

        if is_templated:
            # Template args in declaration order matching the Inputs
            # template parameter list. For DriverNode we encoded
            # <IncOutRef, CounterCallRef>; for ObserverNode
            # <CounterCallRef>; for IncrementerNode <IncOutRef>.
            # The order is the port declaration order. So we compute
            # ref types for each outbound port and pass them in that order.
            ref_types = []
            ref_args = []
            for port in outbound_ports:
                peer_key = (p.name, port.name)
                if peer_key not in outbound_peer:
                    raise ValueError(
                        f"{p.name}.{port.name} has no connect edge")
                peer_proto_name, _ = outbound_peer[peer_key]
                peer = proto_by_name[peer_proto_name]
                peer_is_local = peer.name in local_names
                ref_types.append(ref_type_for(peer, peer_is_local))
                if peer_is_local:
                    ref_args.append(f"{peer.snake}_local_ref")
                else:
                    ref_args.append(f"{peer.snake}_ref")

            using_alias = f"{p.snake}_T"
            lines.append(f"    // local: {p.node_type}")
            lines.append(
                f"    using {using_alias} = demo::{p.node_type}<" +
                ", ".join(ref_types) + ">;")
            # Build the Inputs initializer-list. Order: logger, timers (if any),
            # then per-port refs in declaration order.
            init_fields = ["logger"]
            if needs_timers:
                init_fields.append("timers")
            init_fields.extend(ref_args)
            lines.append(
                f"    {using_alias} {p.snake}({using_alias}::Inputs{{" +
                ", ".join(init_fields) + "});"
            )
        else:
            # Non-templated node. Inputs is concrete <Node>Inputs.
            lines.append(f"    // local: {p.node_type}")
            init_fields = ["logger"]
            if needs_timers:
                init_fields.append("timers")
            lines.append(
                f"    demo::{p.node_type} {p.snake}("
                f"demo::{p.node_type}Inputs{{" +
                ", ".join(init_fields) + "});"
            )

        if p.name in needs_local_ref:
            lines.append(
                f"    demo::runtime::LocalRef<demo::{p.node_type}> "
                f"{p.snake}_local_ref({p.snake});"
            )
        lines.append("")

    # Wait — the topological order is per-prototype, but a node that
    # uses a LocalRef needs the LocalRef variable in scope BEFORE its
    # own construction. The sort puts low-outbound nodes first which
    # are the ref targets — good. But the Inputs literal references
    # the LocalRef by name, so the LocalRef has to be emitted before
    # the node that uses it. The block above emits LocalRef AFTER the
    # node it wraps; that's fine for the construction of the target
    # node itself, and by the time we reach the next iteration (a node
    # that needs that LocalRef in its Inputs), it's in scope. Subtle
    # but correct given the sort.

    # Inbound dispatch registration. For each local prototype that has
    # at least one inbound connect from a remote prototype, bind it on
    # TIPC and register the right dispatch entries.
    for p in local_sorted:
        n = nodes[p.node_type]
        # Determine which of this node's ports receive from a remote.
        remote_inbound_ports = []
        for port in n.ports:
            if port.kind not in ("receiver", "server"):
                continue
            edges = inbound_peer.get((p.name, port.name), [])
            for src_name, _sp in edges:
                if src_name not in local_names:
                    remote_inbound_ports.append(port)
                    break

        if not remote_inbound_ports:
            continue

        bind_var = f"bind_{p.snake}"
        lines.append(
            f"    auto* {bind_var} = mux.bind_node({p.snake}, "
            f"{p.tipc_type_hex}u, {p.tipc_instance}u);"
        )
        for port in remote_inbound_ports:
            iface = ifaces.get(port.iface_name)
            if iface is None:
                lines.append(f"    // WARN: unknown iface {port.iface_name}")
                continue
            if port.kind == "receiver":
                # Receiver: senderReceiver iface with `data Msg`.
                # → register_cast<demo_system_Msg>(bind, node).
                msg = iface.data_msg or "Unknown"
                cxx_msg = _cxx_type_for(proto_pkg, msg)
                lines.append(
                    f"    mux.register_cast<{cxx_msg}>("
                    f"{bind_var}, {p.snake});"
                )
            elif port.kind == "server":
                # Server: clientServer iface with operations.
                # → register_call<demo_system_Req, demo_system_Reply>
                #   for each operation.
                for op_name, req, reply in iface.ops:
                    cxx_req   = _cxx_type_for(proto_pkg, req)
                    cxx_reply = _cxx_type_for(proto_pkg, reply)
                    lines.append(
                        f"    mux.register_call<{cxx_req}, "
                        f"{cxx_reply}>({bind_var}, {p.snake});"
                    )
        lines.append("")

    lines.append("    mux.start();")
    for p in local_sorted:
        lines.append(f"    {p.snake}.start();")

    # kick_off() per the .art annotation (no more hand-table).
    for p in local_sorted:
        if nodes[p.node_type].kick_off:
            lines.append(f"    {p.snake}.kick_off();")
    lines.append("")

    lines.append("    int run_ms = 5000;")
    lines.append('    if (const char* env = std::getenv("DEMO_RUN_MS")) '
                  "run_ms = std::atoi(env);")
    lines.append("    auto deadline = std::chrono::steady_clock::now() +")
    lines.append("                    std::chrono::milliseconds(run_ms);")
    lines.append("    while (!g_shutdown.load() &&")
    lines.append("           std::chrono::steady_clock::now() < deadline) {")
    lines.append("        std::this_thread::sleep_for("
                  "std::chrono::milliseconds(50));")
    lines.append("    }")
    lines.append("")
    lines.append(f'    logger->info("=== {process} stopping ===");')
    lines.append("    mux.stop();")
    for p in reversed(local_sorted):
        lines.append(f'    {p.snake}.stop("normal");')
    lines.append("")

    # Summary lines per local node — same as before.
    SUMMARY = {
        "CounterNode":     '"counter=" + std::to_string({var}.state().counter)',
        "DriverNode":      '"driver.replies_ok=" + std::to_string({var}.state().replies_ok)',
        "ObserverNode":    ('"polls=" + std::to_string({var}.state().polls_issued) + '
                              '" replies_ok=" + std::to_string({var}.state().replies_ok) + '
                              '" timeouts=" + std::to_string({var}.state().timeouts) + '
                              '" last_value=" + std::to_string({var}.state().last_value)'),
        "IncrementerNode": '"casts_sent=" + std::to_string({var}.state().casts_sent)',
    }
    parts = []
    for p in local_sorted:
        s = SUMMARY.get(p.node_type)
        if s:
            parts.append(s.format(var=p.snake))
    if parts:
        joined = ' + " " + '.join(parts)
        lines.append(f'    logger->info(std::string("=== {process} '
                      f'summary: ") + ')
        lines.append(f"        {joined} + \" ===\");")
    lines.append("    return 0;")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_cmakelists(comp: _Composition,
                        process: str,
                        runtime_dir: str) -> str:
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by `artheia gen-app-composition` — DO NOT EDIT")
    lines.append(f"# composition: {comp.name}")
    lines.append(f"# process:     {process}")
    lines.append("cmake_minimum_required(VERSION 3.16)")
    lines.append(f"project(gen_{comp.name}_{process} C CXX)")
    lines.append("")
    lines.append("set(CMAKE_CXX_STANDARD 17)")
    lines.append("set(CMAKE_CXX_STANDARD_REQUIRED ON)")
    lines.append("set(CMAKE_POSITION_INDEPENDENT_CODE ON)")
    lines.append("")
    lines.append("if(NOT TARGET demo_runtime)")
    lines.append(f'  add_subdirectory({runtime_dir} demo_runtime_build)')
    lines.append("endif()")
    lines.append("")
    lines.append(f"add_executable(${{PROJECT_NAME}} main.cc)")
    lines.append("target_link_libraries(${PROJECT_NAME} PRIVATE demo_runtime)")
    return "\n".join(lines) + "\n"


# ---- public API ---------------------------------------------------------

def _emit_proto(art_path: Path, proto_out: Path,
                run_nanopb: bool) -> List[Path]:
    """Emit the composition's package .proto under ``proto_out`` and,
    if ``run_nanopb`` and ``nanopb_generator`` is on PATH, compile it to
    ``.pb.{c,h}`` in place. Returns the paths written.

    Layout mirrors the source .art package (e.g. ``system.demo`` →
    ``<proto_out>/system/demo/demo.proto``). The proto ``package`` decl
    uses the libc-safe rewrite (``services_demo``) — see
    :func:`artheia.generators.proto._proto_package_name`. This keeps the
    app and its codec in one ``gen-app-composition`` invocation rather
    than a separate ``gen-proto-package`` + manual nanopb step.
    """
    from .proto_package import generate_package_proto
    proto_file = generate_package_proto(art_path, proto_out)
    written: List[Path] = [proto_file]
    if not run_nanopb:
        return written
    import shutil
    import subprocess
    if shutil.which("nanopb_generator") is None:
        # No codec compiler available — leave the .proto; caller's build
        # (Bazel genrule or a later nanopb run) compiles it.
        return written
    proto_dir = proto_file.parent
    subprocess.run(
        ["nanopb_generator", "-I", str(proto_out), "-D", str(proto_out),
         str(proto_file.relative_to(proto_out))],
        cwd=str(proto_out), check=True,
    )
    leaf = proto_file.stem
    for ext in (".pb.c", ".pb.h"):
        pb = proto_dir / f"{leaf}{ext}"
        if pb.exists():
            written.append(pb)
    return written


def generate_composition(art_path: str | Path,
                          composition_name: str,
                          out_root: str | Path,
                          runtime_dir: str = "../../demo",
                          proto_out: str | Path | None = None,
                          run_nanopb: bool = True) -> List[Path]:
    art_path = Path(art_path).resolve()
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    comp, nodes, ifaces, proto_pkg = _harvest(art_path, composition_name)
    # Only the composition's OWN processes get a project; remote peers
    # folded in from cluster connects are referenced, not generated.
    processes = comp.own_processes

    written: List[Path] = []
    # Codec first: the per-process mains #include the package .pb.h, so
    # emitting the proto here makes gen-app-composition self-contained.
    if proto_out is not None:
        written.extend(_emit_proto(art_path, Path(proto_out), run_nanopb))

    for proc in processes:
        proj_dir = out_root / f"{composition_name}_{proc}"
        proj_dir.mkdir(parents=True, exist_ok=True)
        main_cc = proj_dir / "main.cc"
        main_cc.write_text(_render_main(comp, proc, nodes, ifaces, proto_pkg))
        written.append(main_cc)
        cml = proj_dir / "CMakeLists.txt"
        cml.write_text(_render_cmakelists(comp, proc, runtime_dir))
        written.append(cml)

    return written
