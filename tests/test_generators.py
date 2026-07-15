"""Generator output tests."""
from __future__ import annotations

import json
from pathlib import Path

from artheia.generators import generate_etcd_schema, generate_netgraph, generate_proto
from artheia.model import parse_file, parse_string


REPO = Path(__file__).resolve().parents[1]


def test_proto_emits_one_file_per_message(tmp_path):
    model = parse_file(REPO / "examples" / "demo.art")
    files = generate_proto(model, tmp_path, source_file="examples/demo.art")
    names = sorted(p.name for p in files)
    # GetStatus.proto = the implicit empty request for the NO-ARG operation
    # `operation GetStatus() returns StatusReport` — synthesized so gen-proto
    # matches gen-app (both emit a `message <Op> {}` for a paramless op).
    assert names == [
        "GetStatus.proto", "SpeedSignal.proto", "StatusReport.proto",
        "TorqueRequest.proto",
    ]


def test_proto_no_arg_operation_emits_empty_request(tmp_path):
    """A no-arg clientServer operation → an empty `message <Op> {}`, so the
    register_call<<Op>, Reply> request type is declared. Regression for the
    supervisor Stop() gap (gen-app referenced system_supervisor_Stop but the
    proto didn't define it)."""
    model = parse_string(
        """
        package p
        message Reply { uint32 ok }
        interface clientServer Ctl {
            operation Ping() returns Reply
        }
        """
    )
    generate_proto(model, tmp_path)
    ping = (tmp_path / "Ping.proto")
    assert ping.exists()
    assert "message Ping {" in ping.read_text()


def test_proto_reserved_holds_tag(tmp_path):
    """A `reserved` marker consumes its positional tag + emits `reserved N;`, so
    deleting a field (→ reserved) doesn't shift later fields' tags."""
    model = parse_string(
        """
        package p
        message Addr { string city }
        message User {
            string name
            reserved old_city
            Addr address
        }
        """
    )
    generate_proto(model, tmp_path)
    user = (tmp_path / "User.proto").read_text()
    assert "string name = 1;" in user
    assert "Addr address = 3;" in user          # tag 3, NOT shifted to 2
    assert "reserved 2;" in user                # the dead tag is reserved


def test_proto_package_reserved(tmp_path):
    """Same, through the per-package generator (gen-app path)."""
    from artheia.generators.proto_package import _render_proto
    from artheia.model import parse_string as _ps
    model = _ps(
        """
        package q
        message M { uint32 a  reserved b  uint32 c }
        """
    )
    txt = _render_proto(model, "q", "")
    assert "uint32 a = 1;" in txt
    assert "uint32 c = 3;" in txt
    assert "reserved 2;" in txt


def test_proto_basic_shape(tmp_path):
    model = parse_string(
        """
        package theia.test
        message Inner { uint32 a }
        message Outer {
            Inner       inner
            repeated string tags
        }
        """
    )
    generate_proto(model, tmp_path)
    outer = (tmp_path / "Outer.proto").read_text()
    assert 'package theia.test;' in outer
    assert 'import "Inner.proto";' in outer
    assert "Inner inner = 1;" in outer
    assert "repeated string tags = 2;" in outer


def test_bundled_proto_cross_package_import(tmp_path):
    """A field whose type is defined in an IMPORTED .art package emits a
    package-qualified type + an import at the imported package's bundled-proto
    path (e.g. the supervisor's TraceConfig embedding platform.runtime's
    TraceControlPush). Real instance: system/supervisor/package.art."""
    from artheia.generators.proto_package import generate_package_proto

    repo = Path(__file__).resolve().parent.parent.parent
    art = repo / "system/supervisor/package.art"
    if not art.exists():
        import pytest
        pytest.skip("supervisor .art not present")
    out = generate_package_proto(str(art), tmp_path)
    text = Path(out).read_text()
    # cross-package import path = <flat-pkg>/<leaf>.proto
    assert 'import "platform_runtime/runtime.proto";' in text
    # field type is package-QUALIFIED so protoc/nanopb resolve via the import
    assert "platform_runtime.TraceControlPush trace_ctrl" in text
    assert "platform_runtime.LogLevelPush log_level" in text
    # the imported enum is NOT re-emitted locally (it lives in the runtime proto)
    assert "enum TraceKind" not in text


def test_netgraph_shape(tmp_path):
    model = parse_file(REPO / "examples" / "demo.art")
    out = tmp_path / "netgraph.json"
    generate_netgraph(model, out)
    data = json.loads(out.read_text())

    assert data["package"] == "theia.demo"
    node_names = {n["name"] for n in data["nodes"]}
    assert node_names == {"SpeedPublisher", "TorqueController", "Actuator"}

    speed_pub = next(n for n in data["nodes"] if n["name"] == "SpeedPublisher")
    assert speed_pub["tipc"] == {"type": "0x80010001", "instance": "0"}

    composition = data["compositions"][0]
    assert composition["name"] == "VehicleSystem"
    assert len(composition["connections"]) == 3
    first = composition["connections"][0]
    assert first["source"] == {"prototype": "speed_pub", "port": "out"}
    assert first["target"] == {"prototype": "torque_ctrl", "port": "speed_in"}
    assert first["interface"] == "SpeedIf"
    assert first["messages"] == ["SpeedSignal"]


def test_params_config_shape(tmp_path):
    """gen-params emits one section per PROTOTYPE (= kNodeName, not node-type),
    with typed defaults; a param-less node-type contributes no section."""
    from artheia.generators.params_config import generate_params_config
    model = parse_file(REPO / "examples" / "demo.art")
    out = tmp_path / "demo.json"
    generate_params_config(model, out)
    data = json.loads(out.read_text())
    assert data["package"] == "theia.demo"
    nodes = data["nodes"]
    # keyed by prototype name (speed_pub), not node-type (SpeedPublisher)
    assert "speed_pub" in nodes and "SpeedPublisher" not in nodes
    assert nodes["speed_pub"]["publish_period_ms"] == 10        # uint -> int
    assert nodes["speed_pub"]["enabled"] is True                # bool
    assert nodes["speed_pub"]["source_name"] == "front-axle"    # string
    assert nodes["torque_ctrl"]["gain"] == 1.25                 # float
    # a prototype whose node-type has NO params block gets no section
    assert "actuator" not in nodes


def test_params_config_const(tmp_path):
    """A `const` param is recorded in a separate top-level `const` map (node ->
    [read-only param names]); values stay flat so the runtime reader is
    unchanged."""
    from artheia.generators.params_config import build_params
    from artheia.model import parse_string
    model = parse_string(
        """
        package p
        node atomic N {
            tipc type=0x1 instance=0
            params {
                const wire_id : uint32 = 7
                rate_hz       : uint32 = 100
            }
        }
        composition C { prototype N n }
        """
    )
    data = build_params(model)
    assert data["nodes"]["n"] == {"wire_id": 7, "rate_hz": 100}   # values flat
    assert data["const"] == {"n": ["wire_id"]}                    # only the const one


_TX_SCHEMA = {
    "package": "p",
    "configs": {
        "Cfg": {
            "digest": "d1", "proto_type": "p_Cfg", "art_package": "p",
            "nodes": ["n"],
            "fields": [
                {"name": "x", "type": "uint32", "repeated": False},
                {"name": "y", "type": "uint32", "repeated": False},
                {"name": "label", "type": "string", "repeated": False},
            ],
        },
    },
}


def test_gen_transform_nanopb_struct_ops(tmp_path):
    """gen-transform emits a nanopb-STRUCT plugin (no JSON): default carry +
    rule ops as to.X = from.Y, the value-map as an if-chain, and the entry."""
    from artheia.generators.transform_codegen import generate_transform_plugin
    transform = {
        "config_type": "Cfg", "from_digest": "d1", "to_digest": "d2",
        "rules": [
            {"op": "set", "field": "x", "value": 7},
            {"op": "copy", "from": "x", "to": "y"},
            {"op": "transform", "path": "x", "map": {"100": 200}, "default": 0},
        ],
    }
    out = tmp_path / "plugin.cc"
    generate_transform_plugin(transform, out, _TX_SCHEMA)
    src = out.read_text()
    # NO JSON at runtime
    assert "nlohmann" not in src and "json::" not in src
    # nanopb decode/encode + the struct type
    assert "pb_decode" in src and "pb_encode" in src
    assert "p_Cfg from = p_Cfg_init_zero;" in src
    # default carry + rule ops as struct members
    assert "to.x = from.x;" in src          # carry
    assert "to.x = 7;" in src               # set
    assert "to.y = from.y;" in src          # carry y (then copy overrides)
    assert "to.y = from.x;" in src          # copy x->y
    assert "if (from.x == 100) to.x = 200;" in src  # value-map if-chain
    # string carry via strncpy
    assert "std::strncpy(to.label, from.label" in src
    assert 'api->add_edge(api->host, "d1", "d2", &transform_Cfg);' in src


def test_gen_transform_custom_sidecar(tmp_path):
    """An {op:custom,fn:name} rule emits an extern decl + a call, and writes a
    WRITE-ONCE custom sidecar stub with the typed struct signature."""
    from artheia.generators.transform_codegen import generate_transform_plugin
    out = tmp_path / "plug.cc"
    generate_transform_plugin({
        "config_type": "Cfg", "from_digest": "d1", "to_digest": "d2",
        "rules": [{"op": "custom", "fn": "my_reshape"}],
    }, out, _TX_SCHEMA)
    src = out.read_text()
    assert 'extern "C" void my_reshape(const p_Cfg* in, p_Cfg* out);' in src
    assert "my_reshape(&from, &to);" in src
    side = out.with_name("plug_custom.cc")
    assert side.exists()
    stub = side.read_text()
    assert 'extern "C" void my_reshape(const p_Cfg* in, p_Cfg* out) {' in stub
    assert "TODO" in stub
    # write-once: a second gen with a different body does NOT clobber the sidecar
    side.write_text("// user code\n")
    generate_transform_plugin({
        "config_type": "Cfg", "from_digest": "d1", "to_digest": "d2",
        "rules": [{"op": "custom", "fn": "my_reshape"}],
    }, out, _TX_SCHEMA)
    assert side.read_text() == "// user code\n"


def test_config_schema_shape(tmp_path):
    """gen-schema: a node's `config <Msg>` binding → one schema entry per
    config_type with a stable shape digest, proto type, bound nodes, fields."""
    from artheia.generators.config_schema import build_config_schema, _digest
    from artheia.model import parse_string
    model = parse_string(
        """
        package p
        message Cfg { uint32 step  bool wrap }
        interface clientServer Foo { operation Get() }
        node atomic N {
            tipc type=0x1 instance=0
            config Cfg
            ports { server s provides Foo }
        }
        composition C { prototype N n }
        """
    )
    data = build_config_schema(model)
    assert "Cfg" in data["configs"]
    e = data["configs"]["Cfg"]
    assert e["proto_type"] == "p_Cfg"
    assert e["nodes"] == ["n"]                              # prototype name
    assert [f["name"] for f in e["fields"]] == ["step", "wrap"]
    assert e["fields"][0]["type"] == "uint32"
    # digest is stable + order-sensitive
    assert e["digest"] == _digest("Cfg", e["fields"])
    assert e["digest"].startswith("cfg_")


def test_etcd_schema_shape(tmp_path):
    model = parse_file(REPO / "examples" / "demo.art")
    out = tmp_path / "etcd.json"
    generate_etcd_schema(model, out)
    data = json.loads(out.read_text())
    assert data["package"] == "theia.demo"
    keys = data["keys"]
    assert "/nodes/SpeedPublisher/params/enabled" in keys
    assert keys["/nodes/SpeedPublisher/params/enabled"]["default"] is True
    assert keys["/nodes/SpeedPublisher/params/publish_period_ms"]["default"] == 10
    assert keys["/nodes/TorqueController/params/gain"]["default"] == 1.25
    assert keys["/nodes/SpeedPublisher/params/source_name"]["default"] == "front-axle"


def test_etcd_schema_empty_when_no_params(tmp_path):
    from artheia.model import parse_string
    model = parse_string(
        """
        package p
        node atomic N { tipc type=0x1 instance=0 }
        """
    )
    out = tmp_path / "etcd.json"
    generate_etcd_schema(model, out)
    data = json.loads(out.read_text())
    assert data["keys"] == {}


def test_netgraph_flattens_nested_composition(tmp_path):
    """`composition Outer { composition Inner inner; ... }` should
    surface the inner's prototypes + connects in Outer's flattened
    netgraph JSON — inner names appear verbatim at Outer's scope, no
    prefixing. Mirrors the design choice in model/flatten.py."""
    from artheia.model import parse_string
    model = parse_string(
        """
        package p
        interface clientServer Foo { operation Get() }
        node atomic NodeA {
            tipc type=0x10 instance=0
            ports { server p provides Foo }
        }
        node atomic NodeB {
            tipc type=0x11 instance=0
            ports { client q requires Foo }
        }
        composition Inner {
            prototype NodeA a
            prototype NodeB b
            connect b.q to a.p
        }
        composition Outer {
            composition Inner inner
            prototype NodeA top_a
        }
        """
    )
    out = tmp_path / "netgraph.json"
    generate_netgraph(model, out)
    data = json.loads(out.read_text())

    comps_by_name = {c["name"]: c for c in data["compositions"]}
    outer = comps_by_name["Outer"]
    proto_names = {p["name"] for p in outer["prototypes"]}
    # Inner's "a" and "b" appear at Outer's scope verbatim, alongside
    # Outer's own "top_a".
    assert proto_names == {"a", "b", "top_a"}
    # Inner's `connect b.q -> a.p` shows up in Outer's connections too.
    assert len(outer["connections"]) == 1
    conn = outer["connections"][0]
    assert conn["source"]["prototype"] == "b"
    assert conn["target"]["prototype"] == "a"


def test_netgraph_includes_gateway_routes_can(tmp_path):
    model = parse_file(REPO / "examples" / "demo.art")
    out = tmp_path / "netgraph.json"
    generate_netgraph(model, out)
    data = json.loads(out.read_text())

    by_name = {n["name"]: n for n in data["nodes"]}
    routes = by_name["SpeedPublisher"]["gateway_routes"]
    assert len(routes) == 1
    r = routes[0]
    assert r["form"] == "can"
    assert r["direction"] == "in"
    assert r["can"]["can_id"] == 0x42
    assert r["can"]["bus"] == "kcan"
    assert r["can"]["dlc"] == 8


def test_netgraph_signal_ref_resolves_via_catalog(tmp_path):
    from artheia.model import parse_string
    model = parse_string(
        """
        package p
        message ACC_07 { uint32 speed }
        node atomic N { tipc type=0x1 instance=0 }
        gateway_route N {
            signal=ACC_07
            direction=in
        }
        """
    )
    catalog = {
        "messages": {
            "ACC_07": {
                "bus_kind": "can",
                "bus": "kcan",
                "can_id": 0x108,
                "dlc": 8,
            }
        }
    }
    out = tmp_path / "netgraph.json"
    generate_netgraph(model, out, catalog=catalog)
    data = json.loads(out.read_text())
    route = data["nodes"][0]["gateway_routes"][0]
    assert route["form"] == "signal"
    assert route["signal"] == "ACC_07"
    assert route["can"]["can_id"] == 0x108
    assert route["can"]["bus"] == "kcan"


def test_netgraph_signal_ref_unresolved_without_catalog(tmp_path):
    from artheia.model import parse_string
    model = parse_string(
        """
        package p
        message ACC_07 { uint32 speed }
        node atomic N { tipc type=0x1 instance=0 }
        gateway_route N { signal=ACC_07 direction=in }
        """
    )
    out = tmp_path / "netgraph.json"
    generate_netgraph(model, out)
    route = json.loads(out.read_text())["nodes"][0]["gateway_routes"][0]
    assert route["unresolved"] is True
    assert route["signal"] == "ACC_07"


def test_manifest_empty_application_emits_set_not_dict(tmp_path):
    """An empty composition (no processes) must render
    ``ApplicationLayer(processes=set())`` — a bare ``{}`` is an empty DICT, which
    carries through simplify() into ApplicationTarget.processes (a frozenset) and
    crashes the frozen target's hash with 'unhashable type: dict'. Regression for
    the hands-off bootstrap path."""
    from artheia.generators.manifest_gen import _render_manifest

    text = _render_manifest(
        source="empty.art", processes=[], services=[],
        app_name="apps", proc_names=[], process_nodes={},
    )
    # BOTH axes must use set() when empty: the application's processes AND the
    # top-level ExecutionLayer(processes=...). The execution axis used to render
    # `processes={\n    }` (an empty DICT across two lines, so the old substring
    # check missed it), which broke combine()/mappend_set with `set | dict` the
    # moment a fresh `theia init` workspace combined its empty apps with services.
    assert "ExecutionLayer(processes=set())" in text
    assert "ExecutionLayer(processes={" not in text
    assert "processes=set()" in text          # the application axis too

    # And it must actually COMBINE with a non-empty layer (the real failure mode):
    # exec the rendered module and combine it the way the bootstrap rig does.
    import sys as _sys
    ns: dict = {}
    mod = tmp_path / "empty_apps.py"
    mod.write_text(text)
    exec(compile(text, str(mod), "exec"), ns)
    empty = ns["DEPLOYMENT"]
    from artheia.manifest.algebra import Explicit
    from artheia.manifest.deployment import (
        DeploymentLayer, ExecutionLayer, ProcessLayer)
    nonempty = DeploymentLayer(execution=ExecutionLayer(processes={
        ProcessLayer(name="com", executable=Explicit("//x:com"),
                     start_cmd=Explicit("bin/com"),
                     function_group=Explicit("services")),
    }))
    merged = nonempty.combine(empty)   # must NOT raise `set | dict`
    assert any(p.name == "com" for p in merged.execution.processes)

    # And it must actually simplify (the real failure mode): exec the rendered
    # module, bind host_machine the way a rig does (Append over the apps AA), then
    # simplify. Before the fix this raised "unhashable type: 'dict'" the moment
    # the empty-process ApplicationTarget hit the applications frozenset.
    from artheia.manifest.algebra import Explicit, Append
    from artheia.manifest.deployment import (
        DeploymentLayer, ApplicationSetLayer, ApplicationLayer, MachineSetLayer,
        MachineLayer,
    )

    ns: dict = {}
    mod = tmp_path / "m.py"
    mod.write_text(text)
    exec(compile(text, str(mod), "exec"), ns)
    deployment = ns["DEPLOYMENT"]
    bound = deployment.combine(DeploymentLayer(
        machines=MachineSetLayer(machines={
            Append(MachineLayer(name="central")),
        }),
        applications=ApplicationSetLayer(applications={
            Append(ApplicationLayer(name="apps", host_machine=Explicit("central"))),
        }),
    ))
    target = bound.simplify()  # must not raise unhashable-type: dict
    # the (single) application's processes is an empty frozenset now
    app = next(iter(target.applications.applications))
    assert app.processes == frozenset()


def test_serialize_manifest_slices_application_per_machine(tmp_path, monkeypatch):
    """serialize-manifest must slice an application's process list to EACH
    machine — not copy the whole set onto the host board and leave the other
    board empty. Regression for the L4-B split (central+compute): the `services`
    AA spans both boards, but application.json gave central all 16 processes
    (incl. compute's ucm/shwa) and compute an EMPTY applications list, because
    _app_dict copied a.processes verbatim and m_apps filtered only on
    host_machine. Each board's application.json must list exactly the processes
    bound there."""
    import sys

    from artheia.cli import serialize_manifest_cmd
    # serialize_manifest_cmd is a click Command; .callback is the raw function.
    _serialize = serialize_manifest_cmd.callback

    # A tiny two-machine rig: one `services` app whose processes split across
    # central (a, b) + compute (c) — the shape of split_rig (host_machine on
    # central, but c runs on compute).
    mod = tmp_path / "_split_fixture.py"
    mod.write_text(
        "from artheia.manifest.algebra import Explicit\n"
        "from artheia.manifest.deployment import (\n"
        "    DeploymentLayer, ExecutionLayer, ProcessLayer,\n"
        "    MachineSetLayer, MachineLayer,\n"
        "    ApplicationSetLayer, ApplicationLayer)\n"
        "RIG = DeploymentLayer(\n"
        "    execution=ExecutionLayer(processes={\n"
        "        ProcessLayer(name='a', executable=Explicit('//x:a'),\n"
        "                     start_cmd=Explicit('bin/a'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('central')),\n"
        "        ProcessLayer(name='b', executable=Explicit('//x:b'),\n"
        "                     start_cmd=Explicit('bin/b'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('central')),\n"
        "        ProcessLayer(name='c', executable=Explicit('//x:c'),\n"
        "                     start_cmd=Explicit('bin/c'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('compute')),\n"
        "    }),\n"
        "    machines=MachineSetLayer(machines={\n"
        "        MachineLayer(name='central', arch=Explicit('x86_64')),\n"
        "        MachineLayer(name='compute', arch=Explicit('x86_64')),\n"
        "    }),\n"
        "    applications=ApplicationSetLayer(applications={\n"
        "        ApplicationLayer(name='services', host_machine=Explicit('central'),\n"
        "                         processes={'a', 'b', 'c'}),\n"
        "    }),\n"
        ")\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("_split_fixture", None)
    out = tmp_path / "out"
    _serialize("_split_fixture", "RIG", str(out), None, None, None)

    central = json.loads((out / "central" / "application.json").read_text())
    compute = json.loads((out / "compute" / "application.json").read_text())

    # central: the `services` app sliced to ITS processes — a, b (NOT c).
    assert [x["name"] for x in central["applications"]] == ["services"]
    assert central["applications"][0]["processes"] == ["a", "b"]
    # compute: the app is PRESENT (not empty) with only its process — c.
    assert [x["name"] for x in compute["applications"]] == ["services"]
    assert compute["applications"][0]["processes"] == ["c"]


def test_serialize_manifest_emits_run_on_start_false(tmp_path, monkeypatch):
    """A process whose PROCESS_NODES meta carries run_on_start=False must emit
    `run_on_start: false` on its executor.json worker leaf (so the supervisor
    defines but does not boot it — a HW-dependent FC like nm opting out for a
    given deploy). A process without it must NOT emit the key (default true)."""
    import sys

    from artheia.cli import serialize_manifest_cmd
    _serialize = serialize_manifest_cmd.callback

    mod = tmp_path / "_ros_fixture.py"
    mod.write_text(
        "from artheia.manifest.algebra import Explicit\n"
        "from artheia.manifest.deployment import (\n"
        "    DeploymentLayer, ExecutionLayer, ProcessLayer,\n"
        "    MachineSetLayer, MachineLayer,\n"
        "    ApplicationSetLayer, ApplicationLayer)\n"
        "RIG = DeploymentLayer(\n"
        "    execution=ExecutionLayer(processes={\n"
        "        ProcessLayer(name='nm', executable=Explicit('//x:nm'),\n"
        "                     start_cmd=Explicit('bin/nm'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('central')),\n"
        "        ProcessLayer(name='com', executable=Explicit('//x:com'),\n"
        "                     start_cmd=Explicit('bin/com'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('central')),\n"
        "    }),\n"
        "    machines=MachineSetLayer(machines={\n"
        "        MachineLayer(name='central', arch=Explicit('x86_64')),\n"
        "    }),\n"
        "    applications=ApplicationSetLayer(applications={\n"
        "        ApplicationLayer(name='services', host_machine=Explicit('central'),\n"
        "                         processes={'nm', 'com'}),\n"
        "    }),\n"
        ")\n"
        # A supervisor tree listing both workers, so executor.json has leaves.
        "from artheia.manifest.supervisor import SupervisorNode, RestartStrategy\n"
        "SUPERVISORS = [SupervisorNode(name='root',\n"
        "    strategy=RestartStrategy.ONE_FOR_ALL, children=['nm', 'com'])]\n"
        # nm opts out of boot; com is a normal (default-true) worker.
        "PROCESS_NODES = {'nm': {'run_on_start': False}, 'com': {}}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("_ros_fixture", None)
    out = tmp_path / "out"
    _serialize("_ros_fixture", "RIG", str(out), None, None, None)

    execu = json.loads((out / "central" / "executor.json").read_text())

    def leaves(n):
        return ([n] if not n.get("children")
                else [x for c in n["children"] for x in leaves(c)])
    by_name = {w["name"]: w for w in leaves(execu) if w.get("type") == "worker"}
    # nm carries run_on_start:false; com omits the key (default true).
    assert by_name["nm"]["run_on_start"] is False
    assert "run_on_start" not in by_name["com"]


def test_serialize_manifest_per_machine_arch_os(tmp_path, monkeypatch):
    """`serialize-manifest --arch a,b --os x,y` sets arch+os PER machine (sorted
    name order), so ONE split rig serializes for a mixed fleet (rpi4=bookworm +
    jetson=focal) without a duplicate per-arch rig file. A single token still
    applies to every machine."""
    import sys
    from artheia.cli import serialize_manifest_cmd
    _serialize = serialize_manifest_cmd.callback

    mod = tmp_path / "_mixed_fixture.py"
    mod.write_text(
        "from artheia.manifest.algebra import Explicit\n"
        "from artheia.manifest.deployment import (\n"
        "    DeploymentLayer, ExecutionLayer, ProcessLayer,\n"
        "    MachineSetLayer, MachineLayer,\n"
        "    ApplicationSetLayer, ApplicationLayer)\n"
        "RIG = DeploymentLayer(\n"
        "    execution=ExecutionLayer(processes={\n"
        "        ProcessLayer(name='a', executable=Explicit('//x:a'),\n"
        "                     start_cmd=Explicit('bin/a'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('central')),\n"
        "        ProcessLayer(name='b', executable=Explicit('//x:b'),\n"
        "                     start_cmd=Explicit('bin/b'),\n"
        "                     function_group=Explicit('services'),\n"
        "                     machine=Explicit('compute')),\n"
        "    }),\n"
        "    machines=MachineSetLayer(machines={\n"
        "        MachineLayer(name='central'), MachineLayer(name='compute'),\n"
        "    }),\n"
        "    applications=ApplicationSetLayer(applications={\n"
        "        ApplicationLayer(name='svc', host_machine=Explicit('central'),\n"
        "                         processes={'a','b'}),\n"
        "    }),\n"
        ")\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("_mixed_fixture", None)
    out = tmp_path / "out"
    # central=aarch64/bookworm, compute=aarch64/focal (sorted name order: central, compute)
    _serialize("_mixed_fixture", "RIG", str(out), "aarch64,aarch64", "bookworm,focal", None)

    cen = json.loads((out / "central" / "machine.json").read_text())
    com = json.loads((out / "compute" / "machine.json").read_text())
    assert (cen["arch"], cen["os"]) == ("aarch64", "bookworm")
    assert (com["arch"], com["os"]) == ("aarch64", "focal")


def test_gen_lib_emits_state_header_for_plain_node(tmp_path):
    """`gen-app --kind lib` must emit impl/<Node>_state.hh for a plain atomic
    node — the shared Daemon.hh.j2 lib header `#include`s it, so a missing
    _state.hh makes the standalone CMake lib fail to compile. Regression: lib
    mode emitted _handlers.cc but not _state.hh (fc mode always did both)."""
    import shutil

    import pytest

    # generate_lib shells out to `nanopb_generator` (a build tool, shipped by
    # the `nanopb` pip pkg / vendored toolchain — NOT a test dep). The base CI
    # job installs only artheia[dev], so skip cleanly when it's absent.
    if shutil.which("nanopb_generator") is None:
        pytest.skip("nanopb_generator not on PATH")

    from artheia.generators.lib_app import generate_lib

    art = tmp_path / "component.art"
    art.write_text(
        """
        package app.mon
        message Ping { uint32 seq }
        interface senderReceiver PingStream { data Ping ping }
        node atomic MonNode {
            tipc type=0x80060001 instance=0
            tag = "MON"
            ports { sender out provides PingStream }
        }
        composition Mon { prototype MonNode mon }
        """
    )
    out = tmp_path / "client"
    generate_lib(str(art), str(out))

    state = out / "impl" / "MonNode_state.hh"
    assert state.is_file(), "lib mode must emit impl/<Node>_state.hh"
    # the lib header includes exactly this path
    hdr = (out / "lib" / "MonNode.hh").read_text()
    assert 'impl/MonNode_state.hh' in hdr


def test_needs_mux_gates_on_receiver_not_reporting():
    """A reporting=false node that RECEIVES (pg group or receiver port)
    must still get the mux binding — the demux (pg_attach + register_cast)
    is orthogonal to the config-service reporting edge. Regression for the
    carla_sidecar CarlaAct bug: DriveCmd silently dropped at dispatch."""
    from artheia.generators.fc_app import _NodeView, _Port, _DataEl

    recv = _Port(name="cmd_in", kind="receiver", iface="DriveCmdFeed",
                 data=[_DataEl(name="cmd", msg="pkg_DriveCmd")])
    consumer = _NodeView(name="Act", snake="act", upper="ACT",
                         tipc_type="0x1", tipc_instance="0",
                         reporting=False, runnable=False, ports=[recv])
    assert consumer.needs_mux, "reporting=false receiver must bind the mux"

    rds_only = _NodeView(name="Cam", snake="cam", upper="CAM",
                         tipc_type="0x2", tipc_instance="0",
                         reporting=False, runnable=False, ports=[])
    assert not rds_only.needs_mux, "reporting=false port-less needs no mux"

    reporter = _NodeView(name="Rep", snake="rep", upper="REP",
                         tipc_type="0x3", tipc_instance="0",
                         reporting=True, runnable=False, ports=[])
    assert reporter.needs_mux, "reporting node still binds the mux"


# ---- [conflate] receiver port → register_cast(..., conflate=true) ----------
# A `receiver … conflate` port marks its message type keep-latest; gen-app must
# emit register_cast<Msg>(..., /*conflate=*/true) so a stale queued cast is
# overwritten in place (docs/tasks genserver-conflating-mailbox).
import textwrap as _textwrap

_CONFLATE_ART = _textwrap.dedent("""
    package test.conflateport

    message Corridor { uint32 seq = 1 }
    interface senderReceiver CorridorStream {
        data Corridor corridor
    }

    node atomic Planner {
        tipc type=0x8001cc01 instance=0
        reporting = false
        ports {
            receiver corridor_in requires CorridorStream conflate
        }
    }
""")

_NOCONFLATE_ART = _CONFLATE_ART.replace(" conflate\n", "\n")


def _gen_main(art_text):
    import tempfile
    import os
    from artheia.generators.fc_app import generate_fc
    d = tempfile.mkdtemp()
    src = os.path.join(d, "m.art")
    with open(src, "w") as f:
        f.write(art_text)
    out = os.path.join(d, "gen")
    generate_fc(src, out)
    with open(os.path.join(out, "main", "main.cc")) as f:
        return f.read()


def test_conflate_port_emits_conflate_true():
    main_cc = _gen_main(_CONFLATE_ART)
    assert "register_cast<Corridor>" in main_cc
    # the conflate flag is passed
    assert "/*conflate=*/true" in main_cc


def test_noconflate_port_omits_conflate_flag():
    main_cc = _gen_main(_NOCONFLATE_ART)
    assert "register_cast<Corridor>" in main_cc
    assert "/*conflate=*/true" not in main_cc
