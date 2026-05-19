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
    return written


