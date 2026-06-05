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
