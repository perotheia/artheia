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
    assert names == ["SpeedSignal.proto", "StatusReport.proto", "TorqueRequest.proto"]


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


# ---- stub generation ------------------------------------------------------

def test_cpp_stubs_per_node(tmp_path):
    from artheia.generators import generate_cpp_stubs
    model = parse_file(REPO / "examples" / "demo.art")
    paths = generate_cpp_stubs(model, tmp_path, source_file="examples/demo.art")
    names = sorted(p.name for p in paths)
    assert names == ["Actuator_gen.h", "SpeedPublisher_gen.h", "TorqueController_gen.h"]
    torque = (tmp_path / "TorqueController_gen.h").read_text()
    # receiver -> on_*, sender -> send_*, client -> call_*, param -> get_/on_
    assert "void on_speed_in_speed(const SpeedSignal* msg);" in torque
    assert "int send_torque_out_torque(const TorqueRequest* msg);" in torque
    assert "int call_status_query_GetStatus(StatusReport* response);" in torque
    assert "void on_param_gain(float new_value);" in torque
    assert "float get_param_gain(void);" in torque
    assert "#include \"SpeedSignal.pb.h\"" in torque


