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
      __init__.py                     ← regen header
      manifest.py                     ← regen: SwComponent + Executable
      executor.py                     ← gen-once: Process + start_cmd

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


@dataclass
class _DataEl:
    name: str
    msg: str               # local message name


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


def _sr_data(iface) -> list[_DataEl]:
    return [_DataEl(name=d.name, msg=_local_msg(d.type)) for d in iface.data]


def _cs_ops(iface) -> list[_IfaceOp]:
    out: list[_IfaceOp] = []
    for op in iface.operations:
        # CS ops have one "in" param + an optional `returns` clause.
        # The grammar allows multiple params but every existing FC
        # uses exactly one; codegen treats the first `in` as the req.
        req = ""
        for p in op.params:
            if getattr(p, "direction", "") == "in":
                req = _local_msg(p.type)
                break
        rep = _local_msg(op.returns) if op.returns else None
        out.append(_IfaceOp(name=op.name, req_msg=req, rep_msg=rep))
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
    nv = _NodeView(
        name=node.name,
        snake=_to_snake(node.name),
        upper=node.name.upper(),
        tipc_type=node.tipc.type,
        tipc_instance=node.tipc.instance,
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
                         ``services/system/<fc>/``).
    :param manifest_out: Where manifest.py + executor.py land
                         (typically ``manifest/services/<fc>/``).
                         ``None`` skips manifest emission.
    :param proto_out:    Where .proto lands (typically
                         ``platform/proto/``). ``None`` skips proto
                         emission. The proto goes under
                         ``<proto_out>/<art-pkg-as-path>/<leaf>.proto``.
    :param force:        Overwrite write-once slices (impl + executor.py).

    Returns ``{status: [path,...]}`` for "wrote", "overwrote",
    "skipped-exists".
    """
    art_path = Path(art_path)
    out_dir = Path(out_dir)

    mv = _build_model_view(art_path, cxx_namespace_override=cxx_namespace)
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
    lib_dir = out_dir / "lib"
    p = lib_dir / f"{mv.daemon_class}.hh"
    results[_write(p, env.get_template(f"Daemon{statem_suffix}.hh.j2").render(**ctx),
                    overwrite=True)].append(str(p))
    # Per-node signal routing table — constexpr destinations[] for
    # each outbound signal the .art's composition / cluster connects
    # name. Empty when the node has no out-connects; the file still
    # gets emitted (with just the namespace shell) so users have a
    # single include point regardless.
    p = lib_dir / f"{mv.daemon_class}_netgraph.hh"
    results[_write(p, env.get_template("Netgraph.hh.j2").render(**ctx),
                    overwrite=True)].append(str(p))
    p = lib_dir / "BUILD.bazel"
    results[_write(p, env.get_template("BUILD.lib.bazel.j2").render(**ctx),
                    overwrite=True)].append(str(p))

    # --- main slice (regen) ------------------------------------------------
    main_dir = out_dir / "main"
    p = main_dir / "main.cc"
    results[_write(p, env.get_template(f"main{statem_suffix}.cc.j2").render(**ctx),
                    overwrite=True)].append(str(p))
    p = main_dir / "BUILD.bazel"
    results[_write(p, env.get_template("BUILD.main.bazel.j2").render(**ctx),
                    overwrite=True)].append(str(p))

    # --- impl slice (write-once unless --force) ---------------------------
    impl_dir = out_dir / "impl"
    p = impl_dir / f"{mv.daemon_class}_handlers.cc"
    results[_write(p, env.get_template(f"handlers{statem_suffix}.cc.j2").render(**ctx),
                    overwrite=force)].append(str(p))
    p = impl_dir / "BUILD.bazel"
    results[_write(p, env.get_template("BUILD.impl.bazel.j2").render(**ctx),
                    overwrite=force)].append(str(p))

    # --- manifest slice ----------------------------------------------------
    if manifest_out is not None:
        manifest_dir = Path(manifest_out)
        # manifest.py — regen on every run (it's a pure projection)
        p = manifest_dir / "manifest.py"
        results[_write(p, env.get_template("manifest.py.j2").render(**ctx),
                        overwrite=True)].append(str(p))
        # executor.py — gen-once (carries supervision strategy + start_cmd,
        # both hand-drafted by the rig integrator)
        p = manifest_dir / "executor.py"
        results[_write(p, env.get_template("executor.py.j2").render(**ctx),
                        overwrite=force)].append(str(p))
        # __init__.py so it's a Python package
        p = manifest_dir / "__init__.py"
        results[_write(
            p,
            f"# {mv.fc_short} manifest package — generated by artheia gen-app\n",
            overwrite=True,
        )].append(str(p))

    # --- proto slice -------------------------------------------------------
    if proto_out is not None:
        from .proto_package import generate_package_proto
        proto_path = generate_package_proto(art_path, proto_out)
        # Treat as "wrote" — gen-proto-package handles overwrite itself.
        results["wrote"].append(str(proto_path))

    return results
