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


def test_cpp_stubs_emit_statem_base(tmp_path):
    """A node with a `statem { ... }` block gets a sibling
    <Name>StateMBase.hh that derives from GenStateM and carries the
    static transition table."""
    from artheia.generators import generate_cpp_stubs
    from artheia.model import parse_string

    model = parse_string(
        """
        package svc
        message SystemBoot { }
        message StartupComplete { }
        message PowerOff { }
        message SmData { uint32 boot_attempts }
        node atomic SmDaemon {
            tipc type=0x8001000D instance=0
            statem {
                states [OFF, STARTING, RUNNING, DEGRADED, SHUTDOWN]
                initial OFF
                data SmData

                on OFF:
                    event SystemBoot → STARTING after 30s

                on STARTING:
                    event StartupComplete → RUNNING
                    timeout → DEGRADED

                on DEGRADED:
                    event SystemBoot → STARTING after 30s

                on SHUTDOWN:
                    event PowerOff → halt
            }
        }
        """
    )
    paths = generate_cpp_stubs(model, tmp_path, source_file="test.art")
    names = sorted(p.name for p in paths)
    assert "SmDaemon_gen.h" in names
    assert "SmDaemonStateMBase.hh" in names

    body = (tmp_path / "SmDaemonStateMBase.hh").read_text()

    # Header skeleton + base-class derivation.
    assert "#include \"GenStateM.hh\"" in body
    assert "namespace svc" in body
    assert "enum class SmDaemonState : uint8_t" in body
    assert "OFF = 0," in body
    assert "STARTING = 1," in body
    assert "SHUTDOWN = 4" in body
    assert "class SmDaemonStateMBase" in body
    assert ("public demo::runtime::GenStateM<SmDaemon,"
            in body.replace("\n", " ").replace("  ", " "))

    # init() returns the declared initial state.
    assert "return SmDaemonState::OFF;" in body

    # User data type imported as-is (not synthesised) and included.
    assert "#include \"SmData.pb.h\"" in body
    assert "SmData" in body

    # Multi-state-shared event collapses into one overload with
    # cascaded if-branches.
    assert "handle_event(" in body
    assert "const SystemBoot& /*e*/" in body
    assert "if (s == SmDaemonState::OFF)" in body
    assert "if (s == SmDaemonState::DEGRADED)" in body

    # Single-state event uses the single-branch form.
    assert "if (s == SmDaemonState::STARTING)" in body
    assert "const StartupComplete& /*e*/" in body
    assert ("return demo::runtime::transition_to<SmDaemonState>"
            "(SmDaemonState::RUNNING);" in body)

    # halt rule lowers to halt<S>().
    assert "const PowerOff& /*e*/" in body
    assert ("return demo::runtime::halt<SmDaemonState>();"
            in body)

    # State-timeout dispatch wraps in a switch.
    assert "StateTimeoutMsg<SmDaemonState>" in body
    assert "case SmDaemonState::STARTING:" in body
    assert ("return demo::runtime::transition_to<SmDaemonState>"
            "(SmDaemonState::DEGRADED);" in body)

    # The 30s after-clause lowers to 30000ms.
    assert ("transition_to<SmDaemonState>(SmDaemonState::STARTING, 30000)"
            in body)


def test_cpp_stubs_statem_synthesises_empty_data_struct(tmp_path):
    """When `.art` omits `data <Msg>`, the generator synthesises an
    empty POD so the GenStateM template parameter has a name."""
    from artheia.generators import generate_cpp_stubs
    from artheia.model import parse_string

    model = parse_string(
        """
        package p
        message Tick { }
        node atomic Clicker {
            tipc type=0x1 instance=0
            statem {
                states [IDLE, BUSY]
                initial IDLE
                on IDLE:
                    event Tick → BUSY
            }
        }
        """
    )
    paths = generate_cpp_stubs(model, tmp_path, source_file="test.art")
    body = (tmp_path / "ClickerStateMBase.hh").read_text()
    assert "struct ClickerData {};" in body


def test_cpp_stubs_skips_statem_for_plain_node(tmp_path):
    """Nodes without a statem block get only their _gen.h — no
    StateMBase.hh sibling."""
    from artheia.generators import generate_cpp_stubs
    from artheia.model import parse_string

    model = parse_string(
        """
        package p
        node atomic Plain {
            tipc type=0x1 instance=0
        }
        """
    )
    paths = generate_cpp_stubs(model, tmp_path, source_file="test.art")
    names = [p.name for p in paths]
    assert names == ["Plain_gen.h"]
    assert not (tmp_path / "PlainStateMBase.hh").exists()


