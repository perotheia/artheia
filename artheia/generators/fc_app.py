"""Generate an FC-shaped C++ scaffold from a single .art file.

This is the `--kind fc` arm of ``artheia gen-app``. It produces a
**GenServer / GenStateM-derived** daemon — the shape used by Adaptive
Functional Clusters in services/system/<fc>/. The existing
``--kind psp`` arm (in :mod:`artheia.generators.cpp_app`) targets a
different shape (LifecycleInterface + GwMessageHeader-based signal
routing) and is left untouched.

User's flow::

    $ artheia gen-app --kind fc services/system/<fc>/package.art \\
                      --out services/system/<fc>/                  \\
                      --proto-out platform/proto/

    services/system/<fc>/
      lib/                            ← every regen
        <Fc>Daemon.hh                  message wiring + class decl
        BUILD.bazel                    cc_library (the lib slice)
      main/                            ← every regen
        main.cc                        TimerService boot + signal wait
        BUILD.bazel                    cc_binary (the main slice)
      impl/                            ← first-time-only (override --force)
        <Fc>Daemon_handlers.cc         handler stubs with noop bodies
        BUILD.bazel                    (not regenerated)

    manifest/services/<fc>/
      executor.py                     ← HAND-EDITED (not generated)
      __init__.py                     ← stays as-is

    Note: there is no per-FC manifest.py. The aggregate FC manifest
    lives at ``services/manifest/service.py:FcLayer`` and synthesizes
    SwComponent + Executable from the explicit CLUSTERS list. The
    only deployment knob is ``executor.py`` (Process + start_cmd +
    supervision policy), hand-edited and imported into FcLayer via
    importlib. Gen-app does not touch ``manifest/services/<fc>/``.

    platform/proto/<art-pkg-as-path>/
      <leaf>.proto                    ← regen via gen-proto-package
      <leaf>.pb.{c,h}                 ← user runs nanopb_generator
      BUILD.bazel                     ← regen

The "every regen" slices are safe to overwrite — they're pure
projection of the .art. The "gen-once" + "first-time-only" slices
are the user's territory after first emit; ``--force`` overrides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..model import parse_file
from .statem import StateMSpec, statem_from_ast
from .proto import _proto_package_name


_TEMPLATES = Path(__file__).parent / "templates" / "fc_app"


# ---- view-model dataclasses ------------------------------------------------

@dataclass
class _IfaceOp:
    name: str
    req_msg: str           # local message name (e.g. "SystemBoot")
    rep_msg: Optional[str] # None for one-way (rare in CS), else local name
    # Defining-package proto types (flat, libc-safe). Equal to the FC's own
    # proto_package + name for a same-package message; differ when the
    # message is imported from another package — see _proto_type_of().
    req_proto: str = ""
    rep_proto: Optional[str] = None


@dataclass
class _DataEl:
    name: str
    msg: str               # local message name (the C++ alias / handler arg)
    # Fully-qualified proto type in the message's DEFINING package, flat
    # libc-safe form (e.g. "system_autosar_vehicle_gen2_flexray_EML_01").
    # The RemoteCodec service_id hashes THIS, so sender and receiver must
    # agree — keying off the resolved message's home package guarantees it.
    proto_type: str = ""
    # Path-form of the defining package, for the `#include "<sub>/<leaf>.pb.h"`.
    proto_subpath: str = ""
    # Leaf .proto module name (the FC short name of the defining package).
    proto_leaf: str = ""
    # True when the defining package is platform.runtime — its codec is already
    # THEIA_DECLARE_REMOTE_CODEC'd in the runtime headers, so Codecs.hh must skip
    # re-declaring it (would be an ODR clash). The .pb.h include is still needed.
    runtime_owned: bool = False


@dataclass
class _Port:
    name: str
    kind: str              # "sender" | "receiver" | "server" | "client"
    iface: str             # interface name (e.g. "SmStateStream")
    # For SR ports: list of data elements. For CS ports: list of ops.
    data: list[_DataEl] = field(default_factory=list)
    ops: list[_IfaceOp]  = field(default_factory=list)


@dataclass
class _SignalDest:
    node: str             # target node TYPE name (e.g. "MyNode")
    tipc_type: str        # "0x..." string from .art tipc decl
    tipc_instance: str    # likewise
    # The peer's runtime kNodeName (snake form, e.g. "counter_node") —
    # used by the netgraph RemoteRef peer-tag for trace tagging.
    node_snake: str = ""


@dataclass
class _SignalEntry:
    msg: str              # message type (last segment of FQN)
    direction: str        # "out" | "in"
    destinations: list[_SignalDest] = field(default_factory=list)


@dataclass
class _NodeView:
    name: str
    snake: str             # lowercase + underscore (sm_daemon)
    upper: str             # uppercase + underscore (SM_DAEMON)
    tipc_type: str
    tipc_instance: str
    # AUTOSAR Reporting/Non-Reporting flag. True (default) means
    # gen-app emits per-(node, msg_type) trace_enable / trace_enabled
    # / trace_clear_all methods on the daemon (#363). The Tracer-hh
    # emit path consults the filter map (#355). reporting=false
    # nodes have no trace API.
    reporting: bool = True
    # (The `fallthrough` field was removed along with the wire-info path.
    # An unrouted inbound frame is dropped with a CRITICAL log in TipcMux;
    # there is no handle_info(const InfoMsg&) clause to emit.)
    # Needs a TimerService (.art `requires_timers`). When True the main
    # publishes the process TimerService (set_process_timers) so the
    # node's handlers / init() can send_after() / cancel_timer() via
    # process_timers(). (kick_off retired — startup work is now the OTP
    # init() callback the runtime drives.)
    requires_timers: bool = False
    # RDS opt-in (.art `requires_rds`). When True the node moves bulk data over
    # the ara::rds zero-copy plane (iceoryx/RouDi): main links librds + calls
    # Runtime::Init(node), and each rds_stream below becomes a typed
    # StreamWriter/StreamReader the node's handlers use. Independent of the TIPC
    # control plane (which still carries the "frame ready" notification).
    requires_rds: bool = False
    # The node's `rds { stream <name> { role chunk_size history } }` declarations,
    # lowered to dicts {name, role, chunk_size, history, instance}. Empty unless
    # requires_rds. gen-app emits one StreamWriter/Reader per entry.
    rds_streams: list = field(default_factory=list)
    # AUTOSAR Log&Trace 3-letter Context ID — stamped onto every log
    # record. Falls back to the node name itself when the .art has
    # no `tag = "..."` field, so every node always has a non-empty
    # contextual tag (per #383 fallback rule).
    log_tag: str = ""
    # Node runtime base (from the .art NodeKind). False = atomic (a
    # GenServer, or GenStateM when `statem` is set below). True = a
    # `node runnable` — a GenRunnable free worker (do_start/do_loop/do_stop),
    # e.g. a gRPC proxy thread. Templates branch on this to pick the base.
    runnable: bool = False
    # `node prebuilt` — a third-party binary (iox-roudi, etcd). gen-app emits a
    # FULLY-GENERATED GenRunnable for it: do_start forks + execvp's `path` (with
    # `prebuilt_args` as argv), do_loop drains the child's stdout/stderr through
    # callbacks INTO this node's logger (behave-like-a-pipe), do_stop SIGTERMs +
    # reaps. Normally reporting=false (no HeartbeatPublisher → the supervisor
    # forks/reaps it but never watchdogs it). `path` is the binary; argv comes
    # from the node's `args` string param (whitespace-split into prebuilt_args).
    prebuilt: bool = False
    path: str = ""
    # argv tail for a prebuilt child — from the `args : string = "..."` node
    # param, whitespace-split. Empty when no `args` param. Each element is a
    # single argv token (no shell parsing — the runnable execvp's directly).
    prebuilt_args: list[str] = field(default_factory=list)
    ports: list[_Port] = field(default_factory=list)
    # When the .art's NodeDecl has a `statem { ... }` block, this is
    # the validated StateMSpec lowered from the AST. None for plain
    # GenServer-shaped FCs. Templates branch on `node.statem` being
    # truthy / None to pick GenStateM-vs-GenServer skeleton.
    statem: Optional[StateMSpec] = None
    # The node's `config <Msg>` binding (the structured, etcd-backed config the
    # node observes via services/per). Holds the config message's TYPE NAME (a
    # str) when the .art node has a `config` line, else None. Templates branch on
    # it to declare the on_config_update hook (a node with config can apply it
    # live; the GenServer base delivers ConfigUpdated → this hook).
    config: Optional[str] = None
    # The FSM's `data <Msg>` resolved to a _DataEl, when the statem block
    # declared one. The GenStateM base encodes this message into the STATEM
    # trace record's payload on every transition (OTP `{State, Data}` — the
    # Data half), so it needs a RemoteCodec<Data>, declared in Codecs.hh.
    # None when the node is plain GenServer or the FSM carries no data.
    statem_data: Optional[_DataEl] = None
    # Static signal-routing slice projected from
    # artheia.generators.netgraph.build_netgraph() —
    # per-(msg) `direction + destinations[]` for the runtime LUT.
    # Empty when the .art has no compositions/clusters with connects
    # referencing this node.
    signals: list[_SignalEntry] = field(default_factory=list)
    # ROS-package assembly: True when this node is prototyped from an IMPORTED
    # package (its lib+impl are built THERE and linked via a bazel dep). The
    # composition's main.cc still CONSTRUCTS + starts it, but gen-app must NOT
    # regenerate its per-node lib/impl files here (that would duplicate the class
    # decl + emit a conflicting empty handler stub). `pkg_lib_label` is the
    # imported package's lib bazel label so main.cc includes its header.
    imported: bool = False
    pkg_lib_label: str = ""       # e.g. //packages/v2v/lib:v2v_lib
    pkg_lib_include: str = ""     # e.g. packages/v2v/lib/OsiV2v.hh

    def unique_handler_ops(self) -> list[_IfaceOp]:
        """Server-port operations, deduplicated by (req_msg, rep_msg).

        When two server ports share the same interface (e.g.
        ``ctl_tdb`` + ``ctl_com`` both ``provides TraceControl``)
        their ops have identical signatures. handle_call dispatches by
        message type, so emitting one handler per duplicate signature
        would not compile. Templates iterate this method instead of
        the raw ports×ops nested loop.

        Order is stable: first appearance wins.
        """
        seen: set[tuple[str, Optional[str]]] = set()
        out: list[_IfaceOp] = []
        for p in self.ports:
            if p.kind != "server":
                continue
            for op in p.ops:
                key = (op.req_msg, op.rep_msg)
                if key in seen:
                    continue
                seen.add(key)
                out.append(op)
        return out

    def unique_receiver_data(self) -> list[_DataEl]:
        """Receiver-port data elements, deduplicated by message type.

        Same rationale as :meth:`unique_handler_ops`: if two receiver
        ports require the same senderReceiver interface, both produce
        a ``handle_cast(const X&, ...)`` declaration. Duplicates fail
        to compile.
        """
        seen: set[str] = set()
        out: list[_DataEl] = []
        for p in self.ports:
            if p.kind != "receiver":
                continue
            for d in p.data:
                if d.msg in seen:
                    continue
                seen.add(d.msg)
                out.append(d)
        return out

    def unique_outbound_data(self) -> list[_DataEl]:
        """OUTBOUND message types this node sends — sender-port data
        (cast) + client-port request types (call), deduplicated.

        A sender/caller must also have a RemoteCodec<Msg> to ENCODE the
        message it puts on the wire (`cast(*this, msg, addr)` /
        `call(ref, req, ...)`), not just inbound types. Without this a
        send-only node (e.g. an incrementer that only casts Inc) has an
        empty codec header and `RemoteCodec<Inc>` is incomplete at the
        cast site.
        """
        seen: set[str] = set()
        out: list[_DataEl] = []
        for p in self.ports:
            if p.kind == "sender":
                for d in p.data:
                    if d.msg not in seen:
                        seen.add(d.msg)
                        out.append(d)
            elif p.kind == "client":
                for op in p.ops:
                    # A client encodes the request AND decodes the reply,
                    # so it needs a RemoteCodec for both.
                    if op.req_msg and op.req_msg not in seen:
                        seen.add(op.req_msg)
                        out.append(_DataEl(name=op.name, msg=op.req_msg,
                                           proto_type=op.req_proto))
                    if op.rep_msg and op.rep_msg not in seen:
                        seen.add(op.rep_msg)
                        out.append(_DataEl(name=op.name, msg=op.rep_msg,
                                           proto_type=op.rep_proto or ""))
        return out

    def unique_sender_data(self) -> list[_DataEl]:
        """SENDER-port data types, deduplicated by message type. The node WATCHEs
        each (its pg broadcast groups) at init so the supervisor pushes it the
        membership; broadcast_<port>_<field> then fans out to those members."""
        seen: set[str] = set()
        out: list[_DataEl] = []
        for p in self.ports:
            if p.kind != "sender":
                continue
            for d in p.data:
                if d.msg not in seen:
                    seen.add(d.msg)
                    out.append(d)
        return out


@dataclass
class _ModelView:
    """Everything the templates need from a parsed .art file."""

    # Filesystem / namespace bookkeeping.
    art_package: str              # source spec, e.g. system.services.sm
    proto_package: str            # flattened underscore form (system_services_sm)
    package_subpath: str          # path-form: system/services/sm
    fc_short: str                 # leaf segment: sm
    cxx_namespace: str            # one-segment underscore form (matches .pb.h)
                                  # e.g. system_services_sm
    daemon_class: str             # SmDaemon
    state_enum: str               # SmDaemonState (when statem present)
    has_statem: bool

    # Bazel label prefix for cross-slice deps (//<prefix>/lib:<short>_lib).
    # Derived from --out at generate_fc() entry; default keeps the legacy
    # `services/<fc_short>` shape for callers that didn't set it.
    bazel_pkg_prefix: str = ""

    # Per-node detail.
    nodes: list[_NodeView] = field(default_factory=list)

    # Every message type referenced by any port (deduped); the
    # daemon needs an #include "..pb.h" per entry.
    messages_used: list[str] = field(default_factory=list)

    # Enums declared in the .art, as (name, [value_names]). The lib aliases the
    # enum TYPE and each VALUE so impl code writes `ActivationState`/`ACT_NONE`
    # instead of the double-prefixed nanopb name
    # (system_services_ucm_ActivationState_ActivationState_ACT_NONE). Hands-off
    # generation: no manual constants in the impl. Pairs (enum_name, values).
    enums: list[tuple] = field(default_factory=list)


# ---- model harvesting ------------------------------------------------------

def _local_msg(msg_ref) -> str:
    """Last segment of a MessageDecl FQN — what nanopb's typedef alias
    points to inside our wrapper namespace.

    The grammar's `[MessageDecl|FQN]` returns the resolved AST node;
    `.name` is the leaf identifier.
    """
    return msg_ref.name


def _defining_package(msg_ref) -> str:
    """The .art package the resolved message actually lives in.

    For a same-file message this equals the FC's own package; for a
    message imported from another package (e.g. a PSP bus PDU pulled in
    via `import system.autosar.vehicle_gen2.flexray.*`) it's the bus
    package. The scope provider resolves the cross-ref to the real AST
    node, so its containing model carries the true origin.
    """
    try:
        from textx import get_model
        return getattr(get_model(msg_ref), "name", "") or ""
    except Exception:
        return ""


def _ws_root_of(p) -> "Path | None":
    from pathlib import Path as _P
    p = _P(p).resolve()
    d = p.parent
    while d != d.parent:
        if (d / "MODULE.bazel").exists() or (d / "WORKSPACE").exists() \
                or (d / "WORKSPACE.bazel").exists():
            return d
        d = d.parent
    return None


def _imported_node_lib(node, self_real: "str | None") -> tuple[str, str]:
    """For a node whose body lives in a DIFFERENT .art than the composition,
    return (lib_bazel_label, lib_include_path) for its package — e.g.
    ("//packages/v2v/lib:v2v_lib", "packages/v2v/lib/OsiV2v.hh"). ("","") when the
    node is local (same .art) or unresolvable. The composition uses these to
    include the node's header + skip regenerating its lib/impl."""
    from textx import get_model
    from pathlib import Path as _P
    gm = get_model(node)
    nfile = getattr(gm, "_tx_filename", None)
    if not nfile:
        return "", ""
    nreal = str(_P(nfile).resolve())
    if self_real is not None and nreal == self_real:
        return "", ""
    root = _ws_root_of(nreal)
    if root is None:
        return "", ""
    pkg_dir = _P(nreal).parent.relative_to(root).as_posix()   # packages/<name>
    pkg_name = getattr(gm, "name", "") or ""
    pkg_short = pkg_name.split(".")[-1] if pkg_name else _P(nreal).parent.name
    node_name = getattr(node, "name", "")
    return ("//%s/lib:%s_lib" % (pkg_dir, pkg_short),
            "%s/lib/%s.hh" % (pkg_dir, node_name))


def _imported_package_impl_deps(model) -> list[str]:
    """Bazel deps for the impl libs of PACKAGES this composition imports nodes
    from (the ROS-package assembly: a composition links a package's prebuilt impl
    rather than regenerating its handlers). For every prototype in every
    composition whose resolved node lives in a DIFFERENT .art file than the
    composition, map that file to its workspace-relative bazel package dir and
    emit ``//<dir>/impl:<pkg_short>_impl``. Distinct, sorted, stable.

    The node's source .art file IS its bazel package (packages/<name>/package.art
    → //packages/<name>). Derived from the resolved cross-ref, so it works
    regardless of import spelling; returns [] when no cross-package node is used
    (the ordinary single-package FC)."""
    from textx import get_model
    from pathlib import Path as _P

    def _ws_root(p: _P):
        p = p.resolve()
        d = p.parent
        while d != d.parent:
            if (d / "MODULE.bazel").exists() or (d / "WORKSPACE").exists() \
                    or (d / "WORKSPACE.bazel").exists():
                return d
            d = d.parent
        return None

    self_file = getattr(model, "_tx_filename", None)
    self_real = _P(self_file).resolve() if self_file else None
    deps: dict[str, None] = {}
    for el in model.elements:
        if el.__class__.__name__ != "CompositionDecl":
            continue
        for proto in getattr(el, "elements", []):
            node = getattr(proto, "type", None)
            if node is None:
                continue
            gm = get_model(node)
            nfile = getattr(gm, "_tx_filename", None)
            if not nfile:
                continue
            nreal = _P(nfile).resolve()
            if self_real is not None and nreal == self_real:
                continue          # same .art → the composition's own node
            root = _ws_root(nreal)
            if root is None:
                continue
            pkg_dir = nreal.parent.relative_to(root).as_posix()   # packages/<name>
            pkg_name = getattr(gm, "name", "") or ""
            pkg_short = pkg_name.split(".")[-1] if pkg_name else nreal.parent.name
            deps["//%s/impl:%s_impl" % (pkg_dir, pkg_short)] = None
    return sorted(deps)


def _proto_type_of(msg_ref) -> tuple[str, str, str]:
    """Return (flat_proto_type, proto_subpath, proto_leaf) for a resolved
    message, keyed off its DEFINING package — so the RemoteCodec
    service_id (a hash of this name) matches on both sender and receiver
    regardless of which FC imported it."""
    pkg = _defining_package(msg_ref)
    proto_pkg = _proto_package_name(pkg)              # source-true package name
    flat_pkg = proto_pkg.replace(".", "_")
    flat = flat_pkg + "_" + msg_ref.name
    leaf = pkg.split(".")[-1] if pkg else msg_ref.name
    # The #include path for the defining package's bundled .pb.h. platform.runtime
    # ships its proto under the FLAT-package dir (platform_runtime/runtime.pb.h —
    # the nanopb convention its own headers + include root use), unlike the normal
    # FC dotted layout (system/services/sm/sm.pb.h). Use the flat-package dir for
    # platform.runtime so a cross-package import (the supervisor embedding its
    # TraceControlPush) #includes the right header.
    if pkg == "platform.runtime":
        subpath = flat_pkg                            # → platform_runtime/<leaf>.pb.h
    else:
        subpath = "/".join(pkg.split(".")) if pkg else ""
    return flat, subpath, leaf


def _data_el(name: str, msg_ref) -> _DataEl:
    flat, subpath, leaf = _proto_type_of(msg_ref)
    # platform.runtime types already have their THEIA_DECLARE_REMOTE_CODEC in
    # the runtime headers (GenServer.hh declares TraceControlPush/LogLevelPush).
    # An FC that sends/receives them (the supervisor's ChildControlIf) must NOT
    # re-declare the codec — that's an ODR redefinition. Flag it so the Codecs.hh
    # template skips the declaration (the type + its .pb.h still come from the
    # runtime, linked via platform/runtime).
    runtime_owned = (_defining_package(msg_ref) == "platform.runtime")
    return _DataEl(name=name, msg=_local_msg(msg_ref),
                   proto_type=flat, proto_subpath=subpath, proto_leaf=leaf,
                   runtime_owned=runtime_owned)


def _sr_data(iface) -> list[_DataEl]:
    return [_data_el(d.name, d.type) for d in iface.data]


def _cs_ops(iface) -> list[_IfaceOp]:
    out: list[_IfaceOp] = []
    for op in iface.operations:
        # CS ops carry an optional `in` param + an optional `returns`.
        # codegen treats the first `in` as the request message.
        req = ""
        req_proto = ""
        for p in op.params:
            if getattr(p, "direction", "") == "in":
                req = _local_msg(p.type)
                req_proto, _, _ = _proto_type_of(p.type)
                break
        # Paramless operation (e.g. `operation Get() returns GetReply`):
        # the request IS a message named after the operation (`message
        # Get { }`). Resolve it so register_call<Req,Rep> has a real Req
        # type — otherwise the main emits `register_call<, Rep>`. The
        # request lives in the iface's own package.
        if not req:
            req = op.name
            req_proto = _op_request_proto(iface, op.name)
        rep = _local_msg(op.returns) if op.returns else None
        rep_proto = _proto_type_of(op.returns)[0] if op.returns else None
        out.append(_IfaceOp(name=op.name, req_msg=req, rep_msg=rep,
                            req_proto=req_proto, rep_proto=rep_proto))
    return out


def _op_request_proto(iface, op_name: str) -> str:
    """Proto type for a paramless operation's implicit request message
    (named after the operation), qualified by the iface's defining
    package — so it matches the message the proto generator emits."""
    pkg = _defining_package(iface)
    proto_pkg = _proto_package_name(pkg) if pkg else ""
    return (proto_pkg.replace(".", "_") + "_" + op_name) if proto_pkg else op_name


def _port_view(p) -> _Port:
    kind = p.__class__.__name__
    if kind == "SenderPort":
        return _Port(name=p.name, kind="sender", iface=p.iface.name,
                     data=_sr_data(p.iface))
    if kind == "ReceiverPort":
        return _Port(name=p.name, kind="receiver", iface=p.iface.name,
                     data=_sr_data(p.iface))
    if kind == "ServerPort":
        return _Port(name=p.name, kind="server", iface=p.iface.name,
                     ops=_cs_ops(p.iface))
    if kind == "ClientPort":
        return _Port(name=p.name, kind="client", iface=p.iface.name,
                     ops=_cs_ops(p.iface))
    return _Port(name="<unknown>", kind="?", iface="?")


def _to_snake(name: str) -> str:
    """SmDaemon -> sm_daemon. CamelCase boundary becomes underscore."""
    import re
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


def _rds_streams_from_ast(node, fc_short: str) -> list:
    """Lower a node's `rds { stream <name> { role chunk_size history } }` block
    to a list of dicts. Each stream's InstanceSpecifier is "/<FC>/<name>" (the
    deployment resolver maps it to an iceoryx service/instance/event triple).
    Empty when the node has no rds block."""
    out = []
    for s in getattr(node, "rds_streams", None) or []:
        out.append({
            "name": s.name,
            "role": getattr(s, "role", "reader"),
            "chunk_size": int(getattr(s, "chunk_size", 0) or 0),
            "history": int(getattr(s, "history", 0) or 0),
            # /<FC>/<stream> — resolver splits the last two path segments.
            "instance": f"/{fc_short.capitalize()}/{s.name}",
        })
    return out


def _prebuilt_args_from_ast(node) -> list[str]:
    """argv tail for a `node prebuilt`, from its `args : string = "..."` param.

    A prebuilt node forks a third-party binary (`path`); the flat command-line
    tail (iox-roudi/etcd take a plain argv) is carried in one `args` string
    param, whitespace-split into argv tokens. No shell parsing — the generated
    runnable execvp's the tokens directly. Empty list when there's no `args`
    param (the binary is launched with no arguments).
    """
    for p in getattr(node, "params", None) or []:
        if getattr(p, "name", "") != "args":
            continue
        lit = getattr(p, "default", None)
        val = getattr(lit, "value", lit)
        return str(val).split() if val is not None else []
    return []


def _node_view(node, proto_name: Optional[str] = None) -> _NodeView:
    # model/inherit's _apply_node_defaults guarantees reporting is
    # always populated as the string "true" or "false" by the time
    # we see it. Default-true: missing/empty is treated as reporting.
    reporting_raw = (getattr(node, "reporting", "") or "true").lower()
    # Runtime node IDENTITY (kNodeName / Tracer key / trace nodeName /
    # supervisor push target / netgraph peer tag) is the PROTOTYPE name
    # (e.g. "counter") when this node was reached via a composition — the
    # .art/manifest address nodes by prototype name, so the runtime matches.
    # Falls back to the snake'd node TYPE for standalone gen (no composition).
    # The C++ class identity (name/upper) stays the TYPE — that's the class.
    identity = proto_name or _to_snake(node.name)
    # AUTOSAR Log&Trace tag (#383). The .art's `tag = "..."` is the
    # canonical source; when absent we fall back to the node IDENTITY so
    # the generated logger context matches the trace nodeName.
    log_tag = (getattr(node, "tag", "") or "").strip() or identity
    nv = _NodeView(
        name=node.name,
        snake=identity,
        upper=node.name.upper(),
        tipc_type=node.tipc.type,
        tipc_instance=node.tipc.instance,
        reporting=(reporting_raw == "true"),
        # textX stores the optional requires_timers flag truthy when the
        # keyword was present, else False/"". Coerce to a real bool.
        requires_timers=bool(getattr(node, "requires_timers", False)),
        # RDS: the `requires_rds` flag + the lowered rds_stream declarations.
        requires_rds=bool(getattr(node, "requires_rds", False)),
        rds_streams=_rds_streams_from_ast(node, identity),
        # NodeKind from the .art: `node runnable Foo` → GenRunnable; the
        # default `node atomic` → GenServer (or GenStateM with a statem block).
        runnable=(getattr(node, "kind", "atomic") == "runnable"),
        # `node prebuilt Foo path="..."` → a fully-generated runnable that
        # forks the third-party binary. argv tail is the `args` string param.
        prebuilt=(getattr(node, "kind", "atomic") == "prebuilt"),
        path=(getattr(node, "path", "") or ""),
        prebuilt_args=_prebuilt_args_from_ast(node),
        log_tag=log_tag,
        statem=statem_from_ast(node),  # None when node has no statem block
        # `config <Msg>` binding → the config message's type name (str), else
        # None. The cross-ref may be the MessageDecl object or a bare name.
        config=(getattr(getattr(node, "config", None), "name", None)
                or (getattr(node, "config", None) if isinstance(
                        getattr(node, "config", None), str) else None)),
    )
    # Resolve the FSM `data <Msg>` to a _DataEl so Codecs.hh declares a
    # RemoteCodec<Data> — GenStateM encodes it into the STATEM trace payload
    # (the OTP `{State, Data}` snapshot). The statem block stores the data
    # MessageDecl cross-ref as `data_type`; reuse the same resolution as a
    # port data element so the service_id keys on the defining package.
    if nv.statem is not None:
        _sm_body = getattr(node, "statem", None)
        _data_ref = getattr(_sm_body, "data_type", None) if _sm_body else None
        if _data_ref is not None:
            nv.statem_data = _data_el("data", _data_ref)
    for p in (node.ports or []):
        nv.ports.append(_port_view(p))
    return nv


def _messages_used(nodes: list[_NodeView]) -> list[str]:
    """All message types any port touches — for #include enumeration."""
    seen: dict[str, None] = {}
    for n in nodes:
        for p in n.ports:
            for d in p.data:
                seen[d.msg] = None
            for op in p.ops:
                if op.req_msg:
                    seen[op.req_msg] = None
                if op.rep_msg:
                    seen[op.rep_msg] = None
    return sorted(seen)


def _nodes_prototyped_by_composition(model, composition_name: str) -> set[str]:
    """Return the node-type names a SINGLE named composition prototypes.

    Sibling to :func:`lib_app._nodes_instantiated_by_compositions`, which
    unions ALL compositions. This one scopes to exactly one composition —
    the per-process partitioning `gen-app --kind fc --composition <Name>`
    needs (one composition = one process / one app dir). Nested
    `composition Foo bar` refs are flattened via
    :func:`flatten_composition`, so a composition built out of
    sub-compositions still resolves its full prototyped node set.

    Raises ``ValueError`` if no composition with that name exists, or if
    the composition prototypes no nodes (an empty partition is almost
    certainly a typo and would emit a node-less, un-bootable app).
    """
    from ..model.flatten import flatten_composition

    comp = None
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl" and el.name == composition_name:
            comp = el
            break
    if comp is None:
        available = sorted(
            el.name for el in model.elements
            if el.__class__.__name__ == "CompositionDecl"
        )
        raise ValueError(
            f"--composition {composition_name!r}: no such composition. "
            f"Available compositions: {', '.join(available) or '(none)'}."
        )

    prototypes, _connects = flatten_composition(comp)
    names: set[str] = set()
    for proto in prototypes:
        # proto.type is the resolved NodeDecl reference; .name is the
        # node-type name (e.g. "MyNode") — the same key the netgraph
        # uses for its per-node signal slice, so the two line up.
        t = getattr(proto, "type", None)
        n = getattr(t, "name", None)
        if n:
            names.add(n)
    if not names:
        raise ValueError(
            f"--composition {composition_name!r} prototypes no nodes; "
            f"nothing to emit. Add `prototype <Node> <name>` lines to it."
        )
    return names


def _prototype_name_by_type(model, composition_name: str) -> dict[str, str]:
    """Map node-TYPE name → PROTOTYPE (instance) name for one composition.

    `prototype MyNode my_node` → {"MyNode": "my_node"}. This is the
    canonical node identity: the .art/manifest address nodes by the prototype
    name, so the runtime kNodeName (Tracer key, trace nodeName, supervisor
    push target, netgraph peer tag) uses it too. Returns {} if the composition
    isn't found (caller falls back to the type name).

    NOTE: assumes a node type is prototyped at most once per composition (true
    across the current model — verified). A duplicated type would collide here
    and need per-instance tracers; we'd detect that as len(prototypes) >
    len(map) at the call site if it ever happens.
    """
    from ..model.flatten import flatten_composition

    comp = None
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl" and el.name == composition_name:
            comp = el
            break
    if comp is None:
        return {}
    prototypes, _connects = flatten_composition(comp)
    out: dict[str, str] = {}
    for proto in prototypes:
        t = getattr(proto, "type", None)
        tn = getattr(t, "name", None)
        pn = getattr(proto, "name", None)
        if tn and pn:
            out[tn] = pn
    return out


def _resolved_node_by_type(model, composition_name: str) -> dict:
    """Map node-TYPE name → the RESOLVED real NodeDecl from the composition's
    prototypes.

    A composition may prototype an `extern node` whose real body (tipc, ports)
    lives in an IMPORTED package — e.g. the gateway prototypes the PSP
    `Kcan_Bus` / `Flexray_Bus` mega-nodes, declared locally only as
    `extern node atomic Kcan_Bus { }`. The scope provider resolves the
    prototype's cross-ref (`proto.type`) to the real node (with tipc), even
    though the LOCAL top-level NodeDecl is still the bare extern stub
    (tipc=None). The node loop iterates local NodeDecls, so without this it
    would hit the stub and crash on the missing tipc. This map lets it
    substitute the resolved real node. Returns {} if the composition isn't
    found. (For the PSP buses the resolved node is a node-only PROJECTION —
    ports stripped — via scope._resolve_in_component_only, so the per-PDU
    O(N²) parse is still avoided; the gateway only needs the node identity +
    tipc, the PDU routing lives in the netgraph.)
    """
    from ..model.flatten import flatten_composition

    comp = None
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl" and el.name == composition_name:
            comp = el
            break
    if comp is None:
        return {}
    prototypes, _connects = flatten_composition(comp)
    out: dict = {}
    for proto in prototypes:
        t = getattr(proto, "type", None)
        tn = getattr(t, "name", None)
        if tn and t is not None:
            out.setdefault(tn, t)
    return out


def _is_test_sender_node(el) -> bool:
    """True for a probe-tester node: a `node atomic` whose ONLY ports are
    `sender` (so it can cast events but serves/receives nothing) — the shape
    of SmTester / MyTester, declared so artheia.probe can bind a tester
    identity. Such a node is never deployed (it's in no composition) and must
    NOT be generated into an FC's lib/main.

    Deliberately narrow: it requires AT LEAST one port and EVERY port to be a
    sender, so it never matches a real co-resident worker (e.g. com's
    TraceForwarder — a port-less runnable) or any node with a server/receiver/
    client surface. An extern stub (no body) isn't matched either (no ports).
    """
    if getattr(el, "extern", False):
        return False
    if getattr(el, "kind", "atomic") != "atomic":
        return False   # runnables / others are real workers
    ports = getattr(el, "ports", None) or []
    if not ports:
        return False
    return all(p.__class__.__name__ == "SenderPort" for p in ports)


def _build_model_view(art_path: Path,
                       cxx_namespace_override: Optional[str] = None,
                       composition: Optional[str] = None) -> _ModelView:
    model = parse_file(str(art_path))
    art_package = model.name or "artheia"
    parts = art_package.split(".")
    fc_short = parts[-1]
    # nanopb emits C struct names prefixed with the underscore-flat
    # form of the proto `package` line — which mirrors the .art package
    # verbatim. So for `system.services.sm` we go via
    # _proto_package_name → "system.services.sm" → flatten to
    # "system_services_sm". That's the prefix glued to every typedef.
    proto_pkg = _proto_package_name(art_package).replace(".", "_")
    # User-facing C++ namespace. Resolution order (most→least specific):
    #   1. an explicit --ns override (CLI), then
    #   2. CONVENTION: a `system.services.<fc>` package is an AUTOSAR Adaptive
    #      functional cluster, so it lands in `ara::<fc>` (sm → ara::sm, ucm →
    #      ara::ucm). This is DETERMINISTIC from the package name — no flag, no
    #      .art keyword, no post-regen hand-rename. The committed FC libs (per/
    #      crypto/sm/phm/ucm) already use exactly this; the convention makes regen
    #      reproduce it hands-off instead of needing --ns every time.
    #   3. otherwise the package flattened to one underscore identifier
    #      (`vendor.app` ⇒ `vendor_app`) — the legacy default for non-ARA apps.
    # The override accepts nested colon-colon segments (`vendor::tornado`).
    if cxx_namespace_override:
        cxx_ns = cxx_namespace_override
    elif art_package.startswith("system.services.") and len(parts) == 3:
        cxx_ns = f"ara::{fc_short}"          # the AUTOSAR Adaptive FC convention
    else:
        cxx_ns = art_package.replace(".", "_")
    daemon_class = ""

    # Per-composition partitioning (one composition = one process / one
    # app dir). When `composition` is given, restrict the emitted node
    # set to exactly that composition's prototyped node-types; every
    # other NodeDecl in the .art (sibling compositions' nodes, plus any
    # cross-package forward-decl stubs) is skipped. When None, keep the
    # legacy behaviour: emit every NodeDecl. The netgraph below is still
    # built over the WHOLE model, so a selected node's cross-process
    # peers (cluster connects to nodes in OTHER compositions) survive in
    # its signal slice — those peers are reached by TipcAddr, never
    # constructed locally.
    wanted: Optional[set[str]] = None
    if composition is not None:
        wanted = _nodes_prototyped_by_composition(model, composition)

    # Names prototyped by SOME composition — used (flat mode only) to spare a
    # composition member from the test-node exclusion below.
    _composed: set[str] = set()
    for _el in model.elements:
        if _el.__class__.__name__ == "CompositionDecl":
            _composed |= _nodes_prototyped_by_composition(model, _el.name)

    # type→prototype name map (e.g. {"MyNode": "my_node"}) — the canonical
    # runtime identity. ALWAYS unioned across EVERY composition in the .art
    # (prototype names are globally unique), NOT just the selected one: a node's
    # cross-process PEERS live in OTHER compositions (p1's my_node is a peer of
    # p3's other_node), and their kNodeName peer tags must resolve too. A type
    # prototyped by >1 composition keeps the first seen.
    proto_by_type: dict[str, str] = {}
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl":
            for t, p in _prototype_name_by_type(model, el.name).items():
                proto_by_type.setdefault(t, p)

    # type → RESOLVED real NodeDecl. Lets the node loop substitute a local
    # `extern node` stub (tipc=None) with the imported real node the
    # composition prototype's cross-ref resolves to (e.g. the PSP Kcan_Bus /
    # Flexray_Bus). Unioned across EVERY composition (like proto_by_type), so
    # the substitution works whether or not a specific --composition is
    # selected — an extern is resolved wherever it's prototyped.
    resolved_by_type: dict = {}
    for el in model.elements:
        if el.__class__.__name__ == "CompositionDecl":
            for t, node in _resolved_node_by_type(model, el.name).items():
                resolved_by_type.setdefault(t, node)

    nodes: list[_NodeView] = []
    has_statem = False
    state_enum = ""
    for el in model.elements:
        if el.__class__.__name__ == "NodeDecl":
            if wanted is not None and el.name not in wanted:
                continue
            # Skip a TEST-ONLY sender node (e.g. SmTester / MyTester): a
            # node declared in the package purely so artheia.probe can bind a
            # tester identity and cast events — it has ONLY sender port(s), is
            # in NO composition, and isn't deployed. Generating it pulls a
            # phantom node into the FC's lib/main + references a _state.hh the
            # FC never builds. This is a NARROW shape check (sender-only +
            # uncomposed) so it never drops a real co-resident worker like com's
            # TraceForwarder (a runnable with no ports, not in the composition).
            if _is_test_sender_node(el) and el.name not in _composed:
                continue
            # An `extern node` stub carries no body (tipc/ports live in an
            # imported package). Substitute the real node the composition
            # prototype resolves to, so its tipc + identity are available.
            if getattr(el, "extern", False) and el.name in resolved_by_type:
                el = resolved_by_type[el.name]
            nv = _node_view(el, proto_name=proto_by_type.get(el.name))
            nodes.append(nv)
            if not daemon_class:
                daemon_class = nv.name
                state_enum = f"{nv.name}State"
            if getattr(el, "statem", None) is not None:
                has_statem = True

    # ROS-package assembly: a composition may prototype a node whose body lives
    # ENTIRELY in an imported package, with NO local NodeDecl (not even an extern
    # stub) — the substitution above only covers a local extern stub. Add those
    # resolved nodes so the composition's main.cc CONSTRUCTS + STARTS them; their
    # handler bodies are NOT regenerated here (they link via the package impl dep
    # emitted into BUILD.main). Scoped by `wanted` (the --composition node set).
    _have = {nv.name for nv in nodes}
    self_file = getattr(model, "_tx_filename", None)
    self_real = str(Path(self_file).resolve()) if self_file else None
    for _type, _rnode in sorted(resolved_by_type.items()):
        rname = getattr(_rnode, "name", _type)
        if rname in _have:
            continue
        if wanted is not None and _type not in wanted and rname not in wanted:
            continue
        _nv = _node_view(_rnode, proto_name=proto_by_type.get(_type))
        # Is this node's body in ANOTHER .art (an imported package)? If so, mark it
        # imported so its lib/impl are NOT regenerated here — main.cc links the
        # package's prebuilt impl (dep emitted in BUILD.main) and includes its lib.
        _lbl, _inc = _imported_node_lib(_rnode, self_real)
        if _lbl:
            _nv.imported = True
            _nv.pkg_lib_label = _lbl
            _nv.pkg_lib_include = _inc
        nodes.append(_nv)
        _have.add(rname)
        if not daemon_class:
            daemon_class = rname
            state_enum = f"{rname}State"
        if getattr(_rnode, "statem", None) is not None:
            has_statem = True

    # Join the netgraph projection — per-node signal routing tables
    # come from compositions / clusters in the same .art. The
    # netgraph generator already walks ConnectDecls and produces a
    # `nodes[].signals` dict keyed by node name; we attach each
    # node's slice to its _NodeView so the template can emit a
    # constexpr destinations table.
    from .netgraph import build_netgraph
    ng = build_netgraph(model)
    ng_by_name = {n["name"]: n for n in ng.get("nodes", [])}
    for nv in nodes:
        ng_entry = ng_by_name.get(nv.name, {})
        for msg, info in (ng_entry.get("signals") or {}).items():
            dests = [
                _SignalDest(
                    node=d["node"],
                    tipc_type=d["tipc_type"],
                    tipc_instance=d["tipc_instance"],
                    # Peer kNodeName = the peer's PROTOTYPE name when known
                    # (matches the `static constexpr kNodeName` the peer emits),
                    # else the snake'd node TYPE for standalone peers.
                    node_snake=proto_by_type.get(d["node"], _to_snake(d["node"])),
                )
                for d in info.get("destinations", [])
            ]
            nv.signals.append(_SignalEntry(
                msg=msg, direction=info.get("direction", "out"),
                destinations=dests,
            ))

    # Harvest enums (name + value names) so the lib can alias the TYPE and each
    # VALUE — impl code writes `ActivationState`/`ACT_NONE`, not the nanopb
    # double-prefix. Identified structurally (an element with .values whose items
    # carry .name) to avoid importing the textX class.
    enums: list[tuple] = []
    for el in model.elements:
        vals = getattr(el, "values", None)
        if vals and getattr(el, "name", None) and all(hasattr(v, "name") for v in vals):
            enums.append((el.name, [v.name for v in vals]))

    return _ModelView(
        art_package=art_package,
        proto_package=proto_pkg,
        package_subpath="/".join(parts),
        fc_short=fc_short,
        cxx_namespace=cxx_ns,
        daemon_class=daemon_class,
        state_enum=state_enum,
        has_statem=has_statem,
        # Default to the legacy services/<fc_short> shape; generate_fc
        # overrides this with the actual --out path so non-services
        # FCs (e.g. platform/gateway) get correct cross-slice labels.
        bazel_pkg_prefix=f"services/{fc_short}",
        nodes=nodes,
        messages_used=_messages_used(nodes),
        enums=enums,
    )


# ---- IO helpers ------------------------------------------------------------

def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _write(path: Path, content: str, *, overwrite: bool) -> str:
    """Returns one of {'wrote', 'skipped-exists', 'overwrote'}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return "skipped-exists"
    was = "overwrote" if path.exists() else "wrote"
    path.write_text(content)
    return was


# ---- auto-per-composition helpers ------------------------------------------

def _is_app_layout(out_dir: Path) -> bool:
    """True if *out_dir* uses the APP per-composition layout (not a services FC).

    Mirrors _art_clusters.app_bazel_target's split: a member whose source dir is
    ``services`` is laid out FLAT (//services/<fc>/main, one dir per FC); every
    other base_dir is an app, laid out per-composition (//<base>/<comp>/main). We
    key on the FIRST path segment of --out so `--out apps`, `--out myapp`, etc.
    auto-iterate compositions while `--out services/<fc>` stays flat."""
    parts = [p for p in Path(out_dir).parts if p not in (".", "")]
    return bool(parts) and parts[0] != "services"


def _art_compositions(art_path: Path) -> "list[str]":
    """The composition NAMES declared as members of the .art's cluster(s), in
    source order. Empty when the .art has no cluster members (a bare node/.art,
    or an empty `cluster Applications { }` scaffold) — the caller then falls back
    to the flat whole-.art emit. Reuses _cluster_members so the enumeration
    matches exactly what gen-manifest binds."""
    from ._art_clusters import _cluster_members
    try:
        clusters = _cluster_members(str(art_path))
    except ValueError:
        return []   # no cluster declared → not an app cluster; flat emit.
    seen: set[str] = set()
    comps: list[str] = []
    for _cluster, _base, _pkg, members in clusters:
        for _ident, composition, _nodes in members:
            if composition and composition not in seen:
                seen.add(composition)
                comps.append(composition)
    return comps


# ---- public entry ----------------------------------------------------------

def generate_fc(
    art_path: str | Path,
    out_dir: str | Path,
    *,
    proto_out: Optional[str | Path] = None,
    cxx_namespace: Optional[str] = None,
    composition: Optional[str] = None,
    force: bool = False,
    package_mode: bool = False,
) -> dict[str, list[str]]:
    """Generate the FC scaffold for a single .art file.

    :param art_path:     ``.art`` file. The package name drives the
                         filesystem layout and the C++ namespace.
    :param out_dir:      Where the lib/main/impl slices land (typically
                         ``services/<fc>/``).

                         NOTE: gen-app does NOT emit any manifest. The
                         manifest is generated PER-CLUSTER, not per-FC:
                         ``artheia gen-manifest <system.art>
                         services/manifest/service.py`` builds the FC
                         list from ``cluster Services`` and sidecars the
                         hand-written supervisor tree in ``executor.py``.
    :param proto_out:    Where .proto lands (typically
                         ``platform/proto/``). ``None`` skips proto
                         emission. The proto goes under
                         ``<proto_out>/<art-pkg-as-path>/<leaf>.proto``.
    :param composition:  When set, emit ONE app for a SINGLE composition —
                         only that composition's prototyped node-types get
                         lib/impl/main/proto. Used for per-process layout
                         (one composition = one process / one app dir).
                         Cross-process peers in OTHER compositions are
                         reached by TipcAddr (the netgraph still carries
                         them), never constructed in this app's main.cc.
                         ``None`` keeps the legacy whole-.art behaviour.
                         Raises ``ValueError`` for an unknown / empty
                         composition.
    :param force:        Overwrite the impl slice (write-once after
                         first emit).

    Returns ``{status: [path,...]}`` for "wrote", "overwrote",
    "skipped-exists".
    """
    art_path = Path(art_path)
    out_dir = Path(out_dir)

    # HANDS-OFF default: an APP cluster (anything but services/) is laid out
    # PER-COMPOSITION — gen-manifest derives each member's bazel_target as
    # //<base_dir>/<composition>/main:<cluster> (see _art_clusters.app_bazel_
    # target), so gen-app MUST emit each composition into <out>/<composition>/
    # for the manifest's target to resolve. When the caller doesn't pin a single
    # --composition, auto-iterate every composition the .art declares so the
    # plain recipe `gen-app --kind fc <component.art> --out apps --proto-out proto`
    # produces the layout `theia install` expects — no --composition needed, no
    # per-composition shell loop, no generated-output patching. (services FCs are
    # FLAT — one dir, many nodes — so they're excluded from the auto-iterate.)
    if composition is None and _is_app_layout(out_dir):
        comps = _art_compositions(art_path)
        if comps:
            agg: dict[str, list[str]] = {
                "wrote": [], "overwrote": [], "skipped-exists": [],
            }
            for comp in comps:
                sub = generate_fc(
                    art_path, out_dir,
                    proto_out=proto_out, cxx_namespace=cxx_namespace,
                    composition=comp, force=force,
                )
                for k in agg:
                    agg[k].extend(sub.get(k, []))
            return agg

    # Ergonomics: with --composition, --out is the PARENT and the app dir
    # is the composition name appended verbatim, so the user names the
    # where (--out) and the what (--composition) once and the tool
    # composes the path. e.g. --out up/tmp --composition MyAppP3 ->
    # up/tmp/MyAppP3. Without --composition, --out is the app dir
    # directly (legacy / services).
    if composition is not None:
        out_dir = out_dir / composition

    mv = _build_model_view(art_path, cxx_namespace_override=cxx_namespace,
                           composition=composition)
    # Derive the Bazel package prefix from --out, so generated cross-slice
    # labels (main → lib, impl → lib) point at the actual output tree, not
    # the hardcoded `services/<fc>/` it used to assume. Strip any leading
    # './' and trailing '/' so 'platform/gateway' and 'services/sm' both
    # form a clean '//<prefix>/lib:<short>_lib' label.
    mv.bazel_pkg_prefix = str(out_dir).strip("./").rstrip("/")
    env = _env()

    results: dict[str, list[str]] = {
        "wrote": [], "overwrote": [], "skipped-exists": [],
    }

    # The bazel label of the nanopb proto aggregator the lib links. It FOLLOWS
    # --proto-out so the generated BUILD points at the proto tree this run
    # actually wrote: --proto-out proto → //proto:platform_protos (a consuming
    # workspace, whose protos live under proto/ — distinct from the framework's
    # platform/proto/). Default (no --proto-out, the legacy <out>/generated
    # co-location) keeps the framework's //platform/proto:platform_protos.
    # Platform-repo prefix for the framework labels gen-app bakes into every FC
    # (//platform/runtime, //platform/supervisor/tombstone, //services/rds). In
    # the framework's own services FCs (--out services) these are REAL in-repo
    # targets, so the prefix is empty (bare //platform/...). In a CONSUMING
    # workspace (any non-services --out) the framework is the sibling `pero_theia`
    # bazel module, so qualify them as @pero_theia//platform/... .
    platform_repo = "@pero_theia" if _is_app_layout(out_dir) else ""

    if proto_out is not None:
        # --proto-out points at THIS workspace's own proto tree — a local label.
        _proto_top = Path(proto_out).as_posix().strip("./").rstrip("/")
        proto_label = f"//{_proto_top}:platform_protos"
    else:
        # The FRAMEWORK's proto tree — qualify it with the platform repo in a
        # consuming workspace (else //platform/proto isn't a local package and the
        # build fails "no such package 'platform/proto'").
        proto_label = f"{platform_repo}//platform/proto:platform_protos"

    # Impl-lib deps for PACKAGES this composition imports nodes from (ROS-package
    # assembly): the composition executable links each imported package's prebuilt
    # impl lib, keyed OFF the .art import. Empty for an ordinary single-package FC.
    imported_pkg_deps = _imported_package_impl_deps(parse_file(str(art_path)))

    ctx = {
        "model": mv,
        "source_file": str(art_path),
        "proto_label": proto_label,
        "platform_repo": platform_repo,
        "imported_pkg_deps": imported_pkg_deps,
    }

    # Template-pair selection. `.statem.` templates derive from
    # demo::runtime::GenStateM<T,S,D>, emit a per-event handle_event
    # table + on_enter user-hook. Plain templates derive from
    # demo::runtime::GenServer<T,S> with handle_cast/handle_call only.
    # The .art's `statem { ... }` block on ANY node trips the
    # statem-variant for the whole FC (single-node FCs are the common
    # case; multi-node FCs with mixed statem/non-statem aren't a
    # shape we ship yet).
    statem_suffix = ".statem" if mv.has_statem else ""

    # --- lib slice (regen) -------------------------------------------------
    #
    # PER-NODE files: an FC may declare more than one node (e.g. com =
    # ComDaemon + the test ProbeDaemon). Each node gets its own daemon
    # header + netgraph header + handler stub, so multi-node FCs
    # decompose cleanly and a single node's regen never disturbs a
    # sibling's hand-written impl. The per-node templates render ONE
    # node from ctx["node"]; FC-wide pieces (Log.hh, the codec header,
    # BUILD files, main.cc) render once over the whole node set.
    lib_dir = out_dir / "lib"
    impl_dir = out_dir / "impl"
    for nv in mv.nodes:
        # ROS-package assembly: an IMPORTED node's lib + impl were generated in
        # its own package and are linked via a bazel dep — do NOT regenerate them
        # here (would duplicate the class decl + emit a conflicting empty handler
        # stub). main.cc still constructs + starts it, including the package's lib.
        if getattr(nv, "imported", False):
            continue
        node_ctx = {**ctx, "node": nv}
        # Per-node template-pair selection by node kind: a runnable node
        # derives from GenRunnable (do_start/do_loop/do_stop), a statem node
        # from gen_statem, a plain node from gen_server. Mixed FCs are fine —
        # each node picks its own skeleton. runnable wins over statem (a
        # `node runnable` carries no statem block anyway).
        #
        # A `node prebuilt` is a fully-GENERATED runnable: it derives from
        # GenRunnable but its do_start/do_loop/do_stop are emitted whole — fork
        # + execvp the declared `path` (with params as argv), drain the child's
        # stdout/stderr through callbacks INTO this node's logger, reap + restart
        # on death. No hand-written handlers, no user state struct: the binary is
        # third-party (iox-roudi, etcd). reporting=false so the supervisor
        # supervises it (fork/reap) without expecting a heartbeat.
        if nv.prebuilt:
            node_suffix = ".prebuilt"
        elif nv.runnable:
            node_suffix = ".runnable"
        elif nv.statem is not None:
            node_suffix = ".statem"
        else:
            node_suffix = ""

        p = lib_dir / f"{nv.name}.hh"
        results[_write(p, env.get_template(f"Daemon{node_suffix}.hh.j2").render(**node_ctx),
                        overwrite=True)].append(str(p))
        # Per-node signal routing table — constexpr TipcAddr for each
        # outbound peer the .art's composition / cluster connects name.
        # Empty (just the namespace shell) when the node has no
        # out-connects, so users have a stable include point regardless.
        p = lib_dir / f"{nv.name}_netgraph.hh"
        results[_write(p, env.get_template("Netgraph.hh.j2").render(**node_ctx),
                        overwrite=True)].append(str(p))
        # State struct (write-once). APP-OWNED — the node's persistent
        # fields are behaviour, not derivable from the .art, so the user
        # owns this file and regen never clobbers it. lib/<Node>.hh
        # #includes it. (statem nodes carry their data in the FSM holder,
        # so no per-node state header for them. prebuilt nodes have no user
        # state — the child binary is third-party; everything is generated.)
        if not nv.statem and not nv.prebuilt:
            p = impl_dir / f"{nv.name}_state.hh"
            results[_write(p, env.get_template("state.hh.j2").render(**node_ctx),
                            overwrite=force)].append(str(p))
        # Handler stubs (write-once). Per node, so adding node B never
        # clobbers node A's hand-written bodies. A prebuilt node's handlers are
        # fully generated (path/args + the fork/drain plumbing live in the
        # Daemon header) — overwrite on regen, nothing to hand-edit.
        p = impl_dir / f"{nv.name}_handlers.cc"
        results[_write(p, env.get_template(f"handlers{node_suffix}.cc.j2").render(**node_ctx),
                        overwrite=(force or nv.prebuilt))].append(str(p))

    # FC-wide inbound RemoteCodec specializations (#387), deduplicated
    # across ALL nodes, in ONE header included once by each node header.
    # Kept out of the per-node headers to avoid an ODR clash when two
    # nodes share an inbound type (e.g. com's ComDaemon + ProbeDaemon
    # both take ComEmpty) and main.cc includes both.
    p = lib_dir / f"{mv.fc_short}_codecs.hh"
    results[_write(p, env.get_template("Codecs.hh.j2").render(**ctx),
                    overwrite=True)].append(str(p))
    # Per-FC logging context (#383). Wraps platform::runtime::Logger
    # so every log record gets the AUTOSAR context tag prepended.
    # Tag comes from the daemon-node's .art `tag = "..."` (falling
    # back to the node name when omitted — see _node_view).
    p = lib_dir / "Log.hh"
    results[_write(p, env.get_template("Log.hh.j2").render(**ctx),
                    overwrite=True)].append(str(p))
    # RDS stream specs (FC-wide) — only when a node declared `requires_rds`.
    # One StreamSpec accessor per rds_stream + a writer/reader handle typedef the
    # impl constructs. ara::rds owns the transport; this is the deployment glue.
    if any(n.requires_rds for n in mv.nodes):
        p = lib_dir / "RdsStreams.hh"
        results[_write(p, env.get_template("RdsStreams.hh.j2").render(**ctx),
                        overwrite=True)].append(str(p))
    p = lib_dir / "BUILD.bazel"
    results[_write(p, env.get_template("BUILD.lib.bazel.j2").render(**ctx),
                    overwrite=True)].append(str(p))

    # --- main slice (regen) ------------------------------------------------
    # One main.cc starts a thread per node. The whole-FC statem_suffix
    # selects the main template; the statem main starts statem nodes via
    # start_statem(timers) and plain nodes via start().
    #
    # PACKAGE MODE (--kind package): a package is a ROS-style unit — nodes +
    # protocol + impl, built ONCE as a linkable lib, with NO executable of its
    # own. The COMPOSITION that imports the package owns the main.cc (it assembles
    # nodes from N packages into one process) and links the package's impl lib.
    # So skip the main slice entirely for a package.
    if not package_mode:
        main_dir = out_dir / "main"
        p = main_dir / "main.cc"
        results[_write(p, env.get_template(f"main{statem_suffix}.cc.j2").render(**ctx),
                        overwrite=True)].append(str(p))
        p = main_dir / "BUILD.bazel"
        results[_write(p, env.get_template("BUILD.main.bazel.j2").render(**ctx),
                        overwrite=True)].append(str(p))

    # --- impl slice (write-once unless --force) ---------------------------
    # Per-node handler stubs were written in the node loop above; only
    # the FC-wide BUILD file remains here.
    p = impl_dir / "BUILD.bazel"
    results[_write(p, env.get_template("BUILD.impl.bazel.j2").render(**ctx),
                    overwrite=force)].append(str(p))

    # --- manifest slice ----------------------------------------------------
    # Intentionally empty. The per-FC manifest is dead code: the actual
    # aggregate lives at services/manifest/service.py:FcLayer, which
    # synthesizes SwComponent + Executable from the explicit CLUSTERS
    # list (.art-name + ownership), not from per-FC manifest.py files.
    # The only deployment knob is the supervision policy, hand-edited
    # at manifest/services/<fc>/executor.py and imported into FcLayer
    # via importlib. gen-app emits NO manifest (see gen-manifest).

    # --- proto slice -------------------------------------------------------
    if proto_out is not None:
        from .proto_package import generate_package_proto
        proto_path = generate_package_proto(art_path, proto_out)
        # Treat as "wrote" — gen-proto-package handles overwrite itself.
        results["wrote"].append(str(proto_path))

    return results
