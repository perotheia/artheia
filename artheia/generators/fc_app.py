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
                      --manifest-out manifest/services/<fc>/        \\
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
from ..manifest.statem import StateMSpec, statem_from_ast
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
    # libc-safe form (e.g. "system_autosar_mlbevo_gen2_flexray_EML_01").
    # The RemoteCodec service_id hashes THIS, so sender and receiver must
    # agree — keying off the resolved message's home package guarantees it.
    proto_type: str = ""
    # Path-form of the defining package, for the `#include "<sub>/<leaf>.pb.h"`.
    proto_subpath: str = ""
    # Leaf .proto module name (the FC short name of the defining package).
    proto_leaf: str = ""


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
    node: str             # target node name (informational)
    tipc_type: str        # "0x..." string from .art tipc decl
    tipc_instance: str    # likewise


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
    # Fall-through node (default False). When True, gen-app emits a
    # handle_info(const InfoMsg&, State&) declaration on the daemon
    # (body in impl) so the node receives inbound TIPC frames that
    # matched no register_cast/register_call. The TEST PROBE sets this;
    # a normal node leaves it False and the GenServer base CRITICAL-errors
    # on any unrouted frame (netgraph/wiring disagreement).
    fallthrough: bool = False
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
    ports: list[_Port] = field(default_factory=list)
    # When the .art's NodeDecl has a `statem { ... }` block, this is
    # the validated StateMSpec lowered from the AST. None for plain
    # GenServer-shaped FCs. Templates branch on `node.statem` being
    # truthy / None to pick GenStateM-vs-GenServer skeleton.
    statem: Optional[StateMSpec] = None
    # Static signal-routing slice projected from
    # artheia.generators.netgraph.build_netgraph() —
    # per-(msg) `direction + destinations[]` for the runtime LUT.
    # Empty when the .art has no compositions/clusters with connects
    # referencing this node.
    signals: list[_SignalEntry] = field(default_factory=list)

    def unique_handler_ops(self) -> list[_IfaceOp]:
        """Server-port operations, deduplicated by (req_msg, rep_msg).

        When two server ports share the same interface (e.g.
        ``ctl_supdbg`` + ``ctl_com`` both ``provides TraceControl``)
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


@dataclass
class _ModelView:
    """Everything the templates need from a parsed .art file."""

    # Filesystem / namespace bookkeeping.
    art_package: str              # source spec, e.g. system.services.sm
    proto_package: str            # libc-safe rewrite (services_services_sm)
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
    via `import system.autosar.mlbevo_gen2.flexray.*`) it's the bus
    package. The scope provider resolves the cross-ref to the real AST
    node, so its containing model carries the true origin.
    """
    try:
        from textx import get_model
        return getattr(get_model(msg_ref), "name", "") or ""
    except Exception:
        return ""


def _proto_type_of(msg_ref) -> tuple[str, str, str]:
    """Return (flat_proto_type, proto_subpath, proto_leaf) for a resolved
    message, keyed off its DEFINING package — so the RemoteCodec
    service_id (a hash of this name) matches on both sender and receiver
    regardless of which FC imported it."""
    pkg = _defining_package(msg_ref)
    proto_pkg = _proto_package_name(pkg)              # libc-safe lead rename
    flat = proto_pkg.replace(".", "_") + "_" + msg_ref.name
    subpath = "/".join(pkg.split(".")) if pkg else ""
    leaf = pkg.split(".")[-1] if pkg else msg_ref.name
    return flat, subpath, leaf


def _data_el(name: str, msg_ref) -> _DataEl:
    flat, subpath, leaf = _proto_type_of(msg_ref)
    return _DataEl(name=name, msg=_local_msg(msg_ref),
                   proto_type=flat, proto_subpath=subpath, proto_leaf=leaf)


def _sr_data(iface) -> list[_DataEl]:
    return [_data_el(d.name, d.type) for d in iface.data]


def _cs_ops(iface) -> list[_IfaceOp]:
    out: list[_IfaceOp] = []
    for op in iface.operations:
        # CS ops have one "in" param + an optional `returns` clause.
        # The grammar allows multiple params but every existing FC
        # uses exactly one; codegen treats the first `in` as the req.
        req = ""
        req_proto = ""
        for p in op.params:
            if getattr(p, "direction", "") == "in":
                req = _local_msg(p.type)
                req_proto, _, _ = _proto_type_of(p.type)
                break
        rep = _local_msg(op.returns) if op.returns else None
        rep_proto = _proto_type_of(op.returns)[0] if op.returns else None
        out.append(_IfaceOp(name=op.name, req_msg=req, rep_msg=rep,
                            req_proto=req_proto, rep_proto=rep_proto))
    return out


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


def _node_view(node) -> _NodeView:
    # model/inherit's _apply_node_defaults guarantees reporting is
    # always populated as the string "true" or "false" by the time
    # we see it. Default-true: missing/empty is treated as reporting.
    reporting_raw = (getattr(node, "reporting", "") or "true").lower()
    # Fall-through (default false). Unlike reporting, there's no model
    # post-process default, so missing/empty is simply False.
    fallthrough_raw = (getattr(node, "fallthrough", "") or "false").lower()
    # AUTOSAR Log&Trace tag (#383). The .art's `tag = "..."` is the
    # canonical source; when absent we fall back to the node name so
    # the generated logger always has a non-empty context.
    log_tag = (getattr(node, "tag", "") or "").strip() or node.name
    nv = _NodeView(
        name=node.name,
        snake=_to_snake(node.name),
        upper=node.name.upper(),
        tipc_type=node.tipc.type,
        tipc_instance=node.tipc.instance,
        reporting=(reporting_raw == "true"),
        fallthrough=(fallthrough_raw == "true"),
        # NodeKind from the .art: `node runnable Foo` → GenRunnable; the
        # default `node atomic` → GenServer (or GenStateM with a statem block).
        runnable=(getattr(node, "kind", "atomic") == "runnable"),
        log_tag=log_tag,
        statem=statem_from_ast(node),  # None when node has no statem block
    )
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


def _build_model_view(art_path: Path,
                       cxx_namespace_override: Optional[str] = None) -> _ModelView:
    model = parse_file(str(art_path))
    art_package = model.name or "artheia"
    parts = art_package.split(".")
    fc_short = parts[-1]
    # nanopb emits C struct names prefixed with the underscore-flat
    # form of the proto `package` line — which itself is the libc-safe
    # rewrite of the .art package. So for `system.services.sm` we go
    # via _proto_package_name → "services.services.sm" → flatten to
    # "services_services_sm". That's the prefix glued to every typedef.
    proto_pkg = _proto_package_name(art_package).replace(".", "_")
    # User-facing C++ namespace. Defaults to the .art-package as one
    # underscore-flat identifier (so `system.services.sm` ⇒ the single
    # symbol `system_services_sm`); user override accepts nested
    # colon-colon segments — e.g. ``--ns ara::sm`` emits
    # ``namespace ara::sm { ... }`` directly. The flag is the
    # single point of conformity for AUTOSAR-style FC names and for
    # vendor-app namespaces (e.g. ``--ns vendor::tornado``).
    cxx_ns = cxx_namespace_override or art_package.replace(".", "_")
    daemon_class = ""

    nodes: list[_NodeView] = []
    has_statem = False
    state_enum = ""
    for el in model.elements:
        if el.__class__.__name__ == "NodeDecl":
            nv = _node_view(el)
            nodes.append(nv)
            if not daemon_class:
                daemon_class = nv.name
                state_enum = f"{nv.name}State"
            if getattr(el, "statem", None) is not None:
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
                )
                for d in info.get("destinations", [])
            ]
            nv.signals.append(_SignalEntry(
                msg=msg, direction=info.get("direction", "out"),
                destinations=dests,
            ))

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


# ---- public entry ----------------------------------------------------------

def generate_fc(
    art_path: str | Path,
    out_dir: str | Path,
    *,
    manifest_out: Optional[str | Path] = None,
    proto_out: Optional[str | Path] = None,
    cxx_namespace: Optional[str] = None,
    force: bool = False,
) -> dict[str, list[str]]:
    """Generate the FC scaffold for a single .art file.

    :param art_path:     ``.art`` file. The package name drives the
                         filesystem layout and the C++ namespace.
    :param out_dir:      Where the lib/main/impl slices land (typically
                         ``services/<fc>/``).
    :param manifest_out: Deprecated. Accepted for back-compat but
                         ignored — gen-app no longer writes anything
                         under ``manifest/services/<fc>/``. The
                         aggregate FC manifest lives at
                         ``services/manifest/service.py:FcLayer``;
                         the per-FC supervision policy at
                         ``manifest/services/<fc>/executor.py`` is
                         hand-edited.
    :param proto_out:    Where .proto lands (typically
                         ``platform/proto/``). ``None`` skips proto
                         emission. The proto goes under
                         ``<proto_out>/<art-pkg-as-path>/<leaf>.proto``.
    :param force:        Overwrite the impl slice (write-once after
                         first emit).

    Returns ``{status: [path,...]}`` for "wrote", "overwrote",
    "skipped-exists".
    """
    art_path = Path(art_path)
    out_dir = Path(out_dir)

    mv = _build_model_view(art_path, cxx_namespace_override=cxx_namespace)
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

    ctx = {
        "model": mv,
        "source_file": str(art_path),
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
        node_ctx = {**ctx, "node": nv}
        # Per-node template-pair selection by node kind: a runnable node
        # derives from GenRunnable (do_start/do_loop/do_stop), a statem node
        # from gen_statem, a plain node from gen_server. Mixed FCs are fine —
        # each node picks its own skeleton. runnable wins over statem (a
        # `node runnable` carries no statem block anyway).
        if nv.runnable:
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
        # Handler stubs (write-once). Per node, so adding node B never
        # clobbers node A's hand-written bodies.
        p = impl_dir / f"{nv.name}_handlers.cc"
        results[_write(p, env.get_template(f"handlers{node_suffix}.cc.j2").render(**node_ctx),
                        overwrite=force)].append(str(p))

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
    p = lib_dir / "BUILD.bazel"
    results[_write(p, env.get_template("BUILD.lib.bazel.j2").render(**ctx),
                    overwrite=True)].append(str(p))

    # --- main slice (regen) ------------------------------------------------
    # One main.cc starts a thread per node. The whole-FC statem_suffix
    # selects the main template; the statem main starts statem nodes via
    # start_statem(timers) and plain nodes via start().
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
    # via importlib. ``manifest_out`` is accepted for API back-compat
    # but ignored.
    _ = manifest_out  # unused — see comment above

    # --- proto slice -------------------------------------------------------
    if proto_out is not None:
        from .proto_package import generate_package_proto
        proto_path = generate_package_proto(art_path, proto_out)
        # Treat as "wrote" — gen-proto-package handles overwrite itself.
        results["wrote"].append(str(proto_path))

    return results
