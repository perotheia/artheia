"""ArtheiaContext — parse one .art, expose every node as a named RemoteRef.

After parsing, the context injects a namespace where each node is reachable by
name as a RemoteRef handle (ctx.ref("CounterNode") / ctx.nodes.CounterNode),
carrying its TIPC address + the resolved proto types / service_ids for each
message on its ports. Several NodeProbes share one context → one parse, one
codec cache.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from artheia.model import parse_file
from artheia.generators.fc_app import _proto_type_of

from .codec import Codec

# Port kind -> (gen_server role of the OWNING node).
#   sender   : node SENDS this data (cast out)
#   receiver : node RECEIVES this data (handle_cast)
#   server   : node ANSWERS calls (handle_call)
#   client   : node MAKES calls
_PORT_ROLE = {
    "SenderPort": "sender",
    "ReceiverPort": "receiver",
    "ServerPort": "server",
    "ClientPort": "client",
}


@dataclass
class MsgRef:
    """One message reachable on a port: local name + flat proto type + svc id."""
    name: str            # local message name, e.g. "Inc"
    proto_type: str      # flat nanopb name, e.g. "system_demo_Inc"
    art_package: str     # defining package, e.g. "system.demo"

    @property
    def service_id(self) -> int:
        from .wire import service_id
        return service_id(self.proto_type)


@dataclass
class OpRef:
    """A clientServer operation: request + (optional) reply message refs."""
    name: str
    request: MsgRef
    reply: MsgRef | None


@dataclass
class PortRef:
    name: str
    kind: str            # SenderPort | ReceiverPort | ServerPort | ClientPort
    role: str            # sender | receiver | server | client
    iface: str
    data: list[MsgRef] = field(default_factory=list)   # senderReceiver
    ops: list[OpRef] = field(default_factory=list)      # clientServer


@dataclass
class RemoteRef:
    """A node, addressable by name — the injected per-node handle.

    Mirrors the C++ RemoteRef: a name + TIPC address. The probe uses it as the
    cast/call TARGET; tests name peers (ctx.ref("CounterNode")) not hex.
    """
    name: str
    tipc_type: int
    tipc_instance: int
    ports: list[PortRef] = field(default_factory=list)

    def port(self, name: str) -> PortRef:
        for p in self.ports:
            if p.name == name:
                return p
        raise KeyError(f"{self.name} has no port {name!r}")

    def find_msg(self, msg_name: str) -> MsgRef:
        """Locate a message (by local name) anywhere on this node's ports."""
        for p in self.ports:
            for d in p.data:
                if d.name == msg_name or d.proto_type.endswith("_" + msg_name):
                    return d
            for op in p.ops:
                for m in (op.request, op.reply):
                    if m and (m.name == msg_name
                              or m.proto_type.endswith("_" + msg_name)):
                        return m
        raise KeyError(f"{self.name} has no message {msg_name!r}")

    def find_op(self, op_name: str) -> OpRef:
        for p in self.ports:
            for op in p.ops:
                if op.name == op_name:
                    return op
        raise KeyError(f"{self.name} has no operation {op_name!r}")


def _hexint(v) -> int:
    if isinstance(v, int):
        return v
    return int(str(v), 0)


class ArtheiaContext:
    """Parsed .art + node RemoteRef namespace + shared codec."""

    def __init__(self, art_file: str | Path, proto_root: str | Path):
        self.art_file = str(art_file)
        self.model = parse_file(self.art_file)
        self.package = self.model.name or ""
        self.codec = Codec(proto_root)
        self._refs: dict[str, RemoteRef] = {}
        self._build_refs()
        # Injected namespace: ctx.nodes.CounterNode -> RemoteRef.
        self.nodes = SimpleNamespace(**self._refs)

    # ---- building the node RemoteRef namespace ----------------------------
    def _build_refs(self) -> None:
        ifaces = {el.name: el for el in self.model.elements
                  if "Interface" in el.__class__.__name__}
        for el in self.model.elements:
            if el.__class__.__name__ != "NodeDecl":
                continue
            tipc = getattr(el, "tipc", None)
            if tipc is None:
                # extern / forward-decl: no LOCAL address. A client .art (e.g.
                # tdb) declares `extern node atomic SupervisorCtl { }` to
                # address a node owned by an imported package. Follow the
                # import lines to the REAL definition (which carries the tipc
                # address + ports) so the probe can target it. This is what
                # lets a client .art drive peers it doesn't define — the
                # transport-swap-safe alternative to a hand-rolled TIPC client.
                real = self._resolve_extern_node(el.name)
                if real is None:
                    continue          # truly unresolved — skip (no address)
                el = real
                tipc = getattr(el, "tipc", None)
                if tipc is None:
                    continue
            ref = RemoteRef(
                name=el.name,
                tipc_type=_hexint(tipc.type),
                tipc_instance=_hexint(tipc.instance),
            )
            # Ports come from the REAL node's model; resolve their ifaces in
            # that model's scope (ifaces dict is local-only, so pass {} and let
            # _build_port read the already-resolved iface object off the port).
            for p in getattr(el, "ports", []):
                ref.ports.append(self._build_port(p, ifaces))
            self._refs[el.name] = ref

    def _resolve_extern_node(self, name: str):
        """Find the real NodeDecl for an `extern` forward-decl by following
        this model's `import pkg.*` lines into the imported packages — the
        same directory-climb the scope provider uses. Returns the NodeDecl
        with a tipc address, or None."""
        from artheia.model import parse_file as _parse
        from artheia.model.scope import _import_dir, _PKG_FILE_PRIORITY
        from pathlib import Path

        entry = Path(self.art_file)
        imports = []
        for im in getattr(self.model, "imports", []):
            raw = getattr(im, "package", None) or getattr(im, "name", "") or ""
            # `import system.supervisor.*` → the package is "system.supervisor"
            # (strip the trailing ".*" / "." wildcard the grammar keeps).
            raw = raw.rstrip("*").rstrip(".")
            if raw:
                imports.append(raw)
        for imp_pkg in imports:
            if not imp_pkg:
                continue
            d = _import_dir(entry, self.package, imp_pkg)
            if d is None or not d.exists():
                continue
            for fname in _PKG_FILE_PRIORITY:
                f = d / fname
                if not f.exists():
                    continue
                try:
                    im_model = _parse(str(f))
                except Exception:
                    continue
                for el in getattr(im_model, "elements", []):
                    if (el.__class__.__name__ == "NodeDecl"
                            and el.name == name
                            and not getattr(el, "extern", False)
                            and getattr(el, "tipc", None) is not None):
                        return el
        return None

    def _build_port(self, p, ifaces) -> PortRef:
        iface = getattr(p, "iface", None)
        iface_name = getattr(iface, "name", "")
        port = PortRef(
            name=p.name,
            kind=p.__class__.__name__,
            role=_PORT_ROLE.get(p.__class__.__name__, "?"),
            iface=iface_name,
        )
        if iface is None:
            return port
        # senderReceiver: data elements.
        for d in getattr(iface, "data", []):
            port.data.append(self._msgref(d.type))
        # clientServer: operations (request + optional reply).
        for op in getattr(iface, "operations", []):
            req = op.params[0].type if getattr(op, "params", None) else None
            if req is not None:
                request = self._msgref(req)
            else:
                # Paramless op: request type is named after the op (matches
                # fc_app _op_request_proto). Synthesize a MsgRef for it.
                flat = (self._flat_pkg() + "_" + op.name)
                request = MsgRef(name=op.name, proto_type=flat,
                                 art_package=self.package)
            reply = self._msgref(op.returns) if getattr(op, "returns", None) \
                else None
            port.ops.append(OpRef(name=op.name, request=request, reply=reply))
        return port

    def _flat_pkg(self) -> str:
        from artheia.generators.proto import _proto_package_name
        return _proto_package_name(self.package).replace(".", "_")

    def _msgref(self, msg_decl) -> MsgRef:
        flat, _sub, _leaf = _proto_type_of(msg_decl)
        # defining package of the message (may differ from this .art's package)
        from textx import get_model
        defining_pkg = get_model(msg_decl).name or self.package
        return MsgRef(name=msg_decl.name, proto_type=flat,
                      art_package=defining_pkg)

    # ---- public API -------------------------------------------------------
    def ref(self, node_name: str) -> RemoteRef:
        try:
            return self._refs[node_name]
        except KeyError:
            raise KeyError(
                f"no node {node_name!r} in {self.package} "
                f"(have: {', '.join(sorted(self._refs))})"
            )

    def probe(self, node_name: str):
        """Create a NodeProbe impersonating `node_name` (binds its address)."""
        from .node import NodeProbe
        return NodeProbe(self, self.ref(node_name))
