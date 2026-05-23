"""Callback-style stub generators for C++ and Python.

Per node, emit free-function declarations only. No classes, no inheritance,
no run loop. Runtime glue lives elsewhere.

Mapping per port:
  receiver port "x" requires SR-iface { data Msg v; ...}
    -> void on_x_v(const Msg* msg);                 (user implements)
  sender port "y" provides SR-iface { data Msg v; ...}
    -> int send_y_v(const Msg* msg);                (user calls)
  server port "s" provides CS-iface { operation Op(...) returns R }
    -> int on_s_Op(const A*, const B*, R* response);  (user implements)
  client port "c" requires CS-iface { operation Op(...) returns R }
    -> int call_c_Op(const A*, const B*, R* response); (user calls)

Plus per param:
  param p : T = default
    -> void on_param_p(T new_value);  (runtime invokes on etcd change)
    -> T get_param_p(void);           (initial / cached value)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


_TEMPLATES = Path(__file__).parent / "templates"


# ---- C and Python type tables ---------------------------------------------

_CTYPE = {
    "int32":  "int32_t",
    "int64":  "int64_t",
    "uint32": "uint32_t",
    "uint64": "uint64_t",
    "float":  "float",
    "double": "double",
    "bool":   "bool",
    "string": "const char*",
}

_PYTYPE = {
    "int32":  "int",
    "int64":  "int",
    "uint32": "int",
    "uint64": "int",
    "float":  "float",
    "double": "float",
    "bool":   "bool",
    "string": "str",
}


# ---- view model fed to templates ------------------------------------------

@dataclass
class _DataEl:
    name: str
    message: str


@dataclass
class _OpParam:
    name: str
    message: str


@dataclass
class _OpView:
    name: str
    params: list[_OpParam]
    returns: str | None


@dataclass
class _SRPort:
    name: str
    data_elements: list[_DataEl]


@dataclass
class _CSPort:
    name: str
    operations: list[_OpView]


@dataclass
class _ParamView:
    name: str
    ctype: str
    pytype: str
    default_repr: str


@dataclass
class _NodeView:
    name: str
    upper: str
    tipc_type: str
    tipc_instance: str
    recv_sr: list[_SRPort] = field(default_factory=list)
    send_sr: list[_SRPort] = field(default_factory=list)
    server_ports: list[_CSPort] = field(default_factory=list)
    client_ports: list[_CSPort] = field(default_factory=list)
    params: list[_ParamView] = field(default_factory=list)


# ---- node → view ----------------------------------------------------------

def _sr_data(iface) -> list[_DataEl]:
    return [_DataEl(name=d.name, message=d.type.name) for d in iface.data]


def _cs_ops(iface) -> list[_OpView]:
    out: list[_OpView] = []
    for op in iface.operations:
        out.append(_OpView(
            name=op.name,
            params=[_OpParam(name=p.name, message=p.type.name) for p in op.params],
            returns=op.returns.name if op.returns else None,
        ))
    return out


def _default_repr_py(p) -> str:
    v = p.default.value
    if p.type == "bool":
        return "True" if v == "true" else "False"
    if p.type in ("int32", "int64", "uint32", "uint64"):
        return str(int(v))
    if p.type in ("float", "double"):
        return repr(float(v))
    # string
    return repr(v)


def _node_view(node) -> _NodeView:
    nv = _NodeView(
        name=node.name,
        upper=node.name.upper(),
        tipc_type=node.tipc.type,
        tipc_instance=node.tipc.instance,
    )
    for p in node.ports or []:
        kind = p.__class__.__name__
        if kind == "ReceiverPort":
            nv.recv_sr.append(_SRPort(name=p.name, data_elements=_sr_data(p.iface)))
        elif kind == "SenderPort":
            nv.send_sr.append(_SRPort(name=p.name, data_elements=_sr_data(p.iface)))
        elif kind == "ServerPort":
            nv.server_ports.append(_CSPort(name=p.name, operations=_cs_ops(p.iface)))
        elif kind == "ClientPort":
            nv.client_ports.append(_CSPort(name=p.name, operations=_cs_ops(p.iface)))
    for p in getattr(node, "params", []) or []:
        nv.params.append(_ParamView(
            name=p.name,
            ctype=_CTYPE[p.type],
            pytype=_PYTYPE[p.type],
            default_repr=_default_repr_py(p),
        ))
    return nv


def _messages_used(node_view: _NodeView) -> list[str]:
    seen: dict[str, None] = {}
    for p in node_view.recv_sr + node_view.send_sr:
        for d in p.data_elements:
            seen[d.message] = None
    for p in node_view.server_ports + node_view.client_ports:
        for op in p.operations:
            for param in op.params:
                seen[param.message] = None
            if op.returns:
                seen[op.returns] = None
    return list(seen)


# ---- entry points ---------------------------------------------------------

def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _iter_nodes(model):
    for el in model.elements:
        if el.__class__.__name__ == "NodeDecl":
            yield el


def generate_cpp_stubs(model, out_dir: str | Path, source_file: str = "") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _env()
    tpl = env.get_template("stub.cpp.h.j2")
    statem_tpl = env.get_template("statem_base.hh.j2")
    written: list[Path] = []
    for node in _iter_nodes(model):
        nv = _node_view(node)
        rendered = tpl.render(
            source_file=source_file,
            node=nv,
            messages_used=_messages_used(nv),
        )
        path = out_dir / f"{node.name}_gen.h"
        path.write_text(rendered)
        written.append(path)
        # Emit a <Node>StateMBase.hh alongside the callback stub if the
        # node carries a `statem { ... }` block.
        if getattr(node, "statem", None) is not None:
            sv = _statem_view(node)
            rendered_sm = statem_tpl.render(
                source_file=source_file,
                **sv,
            )
            sm_path = out_dir / f"{node.name}StateMBase.hh"
            sm_path.write_text(rendered_sm)
            written.append(sm_path)
    return written


# ---- statem view → template -----------------------------------------------

def _local_msg_name(msg_ref) -> str:
    """Last segment of a MessageDecl FQN — what's used as a C++ type."""
    return msg_ref.name


def _format_target(target, state_enum: str) -> str:
    """Render a TransitionTarget AST node as a C++ return expression.

    Mirrors :class:`StateMSpec`'s lowering rules (see statem.py) but
    works on the textX AST directly so the template gets pre-formatted
    C++ source strings — keeps the .j2 file readable.
    """
    if getattr(target, "halt", False):
        return f"return demo::runtime::halt<{state_enum}>();"
    state = target.state
    timeout = getattr(target, "timeout", None)
    if timeout:
        ms = _duration_to_ms(timeout)
        return (
            f"return demo::runtime::transition_to<{state_enum}>("
            f"{state_enum}::{state}, {ms});"
        )
    return (
        f"return demo::runtime::transition_to<{state_enum}>("
        f"{state_enum}::{state});"
    )


def _duration_to_ms(text: str) -> int:
    """Mirror of statem.py:_parse_duration_to_ms — kept local to avoid a
    cross-module import cycle (artheia.generators must not depend on
    artheia.manifest)."""
    for suffix, mul in (("ms", 1), ("s", 1000), ("m", 60_000), ("h", 3_600_000)):
        if text.endswith(suffix):
            return int(text[: -len(suffix)]) * mul
    raise ValueError(f"unrecognised duration {text!r}")


def _statem_view(node) -> dict:
    """Build the template context for ``statem_base.hh.j2``.

    The view is intentionally template-shaped (not a dataclass) — the
    template uses ~10 fields and a couple of nested record lists, so a
    plain dict keeps the j2 file readable.
    """
    body = node.statem
    state_enum = f"{node.name}State"
    class_name = node.name
    base_class = f"{node.name}StateMBase"

    states = list(body.states)
    initial = body.initial

    data_struct_is_synthetic = body.data_type is None
    data_struct = (
        f"{node.name}Data" if data_struct_is_synthetic
        else body.data_type.name
    )

    # Walk the on-blocks once, building two parallel lists:
    #   * event_rules — one entry per (state, event_type) pair, with
    #     the pre-formatted return body. The template emits one
    #     handle_event overload per UNIQUE event_type, branching on
    #     state. We dedupe by event_type below.
    #   * timeout_rules — one entry per state that has a `timeout → ...`
    #     rule. The template wraps these in a single switch on state.
    event_rules: list[dict] = []
    timeout_rules: list[dict] = []
    for blk in body.on_blocks:
        state = blk.state
        for rule in blk.rules:
            target_expr = _format_target(rule.target, state_enum)
            target_desc = (
                "halt" if getattr(rule.target, "halt", False)
                else (f"{rule.target.state}" + (
                    f" after {rule.target.timeout}"
                    if getattr(rule.target, "timeout", None) else ""))
            )
            if getattr(rule, "event", None) is not None:
                event_rules.append({
                    "state": state,
                    "event_local": _local_msg_name(rule.event),
                    "body": target_expr,
                    "target_desc": target_desc,
                })
            else:
                # timeout rule
                timeout_rules.append({
                    "state": state,
                    "body": target_expr,
                    "target_desc": target_desc,
                })

    # Collapse event rules: one overload per event_type. Each rule
    # becomes an `if (s == State::X) { return ...; }` branch inside
    # that overload. Multiple states sharing the same event type
    # cascade as `if-if-if` (not `else if` — we want each branch's
    # return to terminate cleanly; the fallthrough to keep_state at
    # the bottom of the overload is in the template).
    events_by_type: dict[str, list[dict]] = {}
    for r in event_rules:
        events_by_type.setdefault(r["event_local"], []).append(r)

    collapsed_event_rules: list[dict] = []
    for event_local, rules in events_by_type.items():
        branches = " ".join(
            f"if (s == {state_enum}::{r['state']}) {{ {r['body']} }}"
            for r in rules
        )
        target_desc = " | ".join(
            f"{r['state']} → {r['target_desc']}" for r in rules
        )
        collapsed_event_rules.append({
            "event_local": event_local,
            "body": branches,
            "target_desc": target_desc,
        })

    # Includes: every event type + the data type (if user-declared).
    # Sort so the output is stable across runs.
    used = set(events_by_type.keys())
    if not data_struct_is_synthetic:
        used.add(data_struct)
    messages_used = sorted(used)

    # Namespace: derive from the parsed model's package, fall back to
    # `artheia_gen` if the model is unpackaged. Use the last segment
    # so the generated header stays succinct.
    pkg = _model_package(node)
    namespace = "artheia_gen" if pkg is None else pkg.replace(".", "_")

    return dict(
        node_name=node.name,
        namespace=namespace,
        class_name=class_name,
        base_class=base_class,
        state_enum=state_enum,
        data_struct=data_struct,
        data_struct_is_synthetic=data_struct_is_synthetic,
        states=states,
        initial=initial,
        event_rules=collapsed_event_rules,
        timeout_rules=timeout_rules,
        messages_used=messages_used,
    )


def _model_package(node) -> str | None:
    """Walk up to the parsed Model to find its package qualifier."""
    parent = getattr(node, "parent", None)
    while parent is not None and type(parent).__name__ != "Model":
        parent = getattr(parent, "parent", None)
    if parent is None:
        return None
    name = getattr(parent, "name", None)
    return name if name else None


