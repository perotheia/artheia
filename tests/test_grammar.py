"""Grammar coverage tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from textx import TextXSemanticError, TextXSyntaxError

from artheia.model import parse_file, parse_string


REPO = Path(__file__).resolve().parents[1]


def test_demo_parses():
    model = parse_file(REPO / "examples" / "demo.art")
    kinds = [(e.__class__.__name__, getattr(e, "name", None)) for e in model.elements]
    assert ("MessageDecl", "SpeedSignal") in kinds
    assert ("NodeDecl", "TorqueController") in kinds
    assert ("CompositionDecl", "VehicleSystem") in kinds
    gateway_routes = [e for e in model.elements if e.__class__.__name__ == "GatewayRouteDecl"]
    assert {r.node.name for r in gateway_routes} == {"SpeedPublisher", "TorqueController"}


def test_empty_package_ok():
    parse_string("package empty\n")


def test_message_only():
    m = parse_string(
        """
        package p
        message M {
            uint32 a = 1
            string b = 2
            repeated bytes c = 3
        }
        """
    )
    msg = m.elements[0]
    assert [f.name for f in msg.fields] == ["a", "b", "c"]
    assert msg.fields[2].repeated is True
    assert msg.fields[0].repeated is False


def test_duplicate_field_number_rejected():
    with pytest.raises(TextXSemanticError, match="field number 1"):
        parse_string(
            """
            package p
            message M {
                uint32 a = 1
                uint32 b = 1
            }
            """
        )


def test_zero_field_number_rejected():
    with pytest.raises(TextXSemanticError, match="non-positive"):
        parse_string(
            """
            package p
            message M {
                uint32 a = 0
            }
            """
        )


def test_tipc_collision_rejected():
    with pytest.raises(TextXSemanticError, match="TIPC address"):
        parse_string(
            """
            package p
            interface senderReceiver If { }
            node atomic A { tipc type=0x1 instance=0 }
            node atomic B { tipc type=0x1 instance=0 }
            """
        )


def test_connect_direction_mismatch_rejected():
    with pytest.raises(TextXSemanticError, match="both ports are"):
        parse_string(
            """
            package p
            message M { uint32 a = 1 }
            interface senderReceiver If { data M m }
            node atomic A {
                tipc type=0x1 instance=0
                ports { sender out provides If }
            }
            node atomic B {
                tipc type=0x2 instance=0
                ports { sender out provides If }
            }
            composition C {
                prototype A a
                prototype B b
                connect a.out to b.out
            }
            """
        )


def test_connect_family_mismatch_rejected():
    with pytest.raises(TextXSemanticError, match="senderReceiver and clientServer"):
        parse_string(
            """
            package p
            message M { uint32 a = 1 }
            interface senderReceiver SR { data M m }
            interface clientServer CS { operation Op() returns M }
            node atomic A {
                tipc type=0x1 instance=0
                ports { sender out provides SR }
            }
            node atomic B {
                tipc type=0x2 instance=0
                ports { client q requires CS }
            }
            composition C {
                prototype A a
                prototype B b
                connect a.out to b.q
            }
            """
        )


def test_connect_interface_mismatch_rejected():
    with pytest.raises(TextXSemanticError, match="interface mismatch"):
        parse_string(
            """
            package p
            message M { uint32 a = 1 }
            interface senderReceiver IfA { data M m }
            interface senderReceiver IfB { data M m }
            node atomic A {
                tipc type=0x1 instance=0
                ports { sender out provides IfA }
            }
            node atomic B {
                tipc type=0x2 instance=0
                ports { receiver in requires IfB }
            }
            composition C {
                prototype A a
                prototype B b
                connect a.out to b.in
            }
            """
        )


def test_syntax_error_surfaces():
    with pytest.raises(TextXSyntaxError):
        parse_string("package p\nmessage { }\n")


# ---- v0.1: params + gateway_route + buses ---------------------------------

_NODE_WITH_PARAMS = """
package p
node atomic N {
    tipc type=0x1 instance=0
    params {
        a : uint32 = 10
        b : bool   = false
        c : string = "hello"
        d : float  = 3.14
    }
}
"""


def test_params_parse_and_typecheck():
    m = parse_string(_NODE_WITH_PARAMS)
    node = m.elements[0]
    assert [p.name for p in node.params] == ["a", "b", "c", "d"]
    assert [p.type for p in node.params] == ["uint32", "bool", "string", "float"]


def test_param_bool_wrong_default_rejected():
    with pytest.raises(TextXSemanticError, match="parameter 'a' is bool"):
        parse_string(
            """
            package p
            node atomic N { tipc type=0x1 instance=0
              params { a : bool = 5 }
            }
            """
        )


def test_param_uint_out_of_range_rejected():
    with pytest.raises(TextXSemanticError, match="out of range"):
        parse_string(
            """
            package p
            node atomic N { tipc type=0x1 instance=0
              params { a : uint32 = -1 }
            }
            """
        )


def test_param_duplicate_rejected():
    with pytest.raises(TextXSemanticError, match="duplicate parameter"):
        parse_string(
            """
            package p
            node atomic N { tipc type=0x1 instance=0
              params { a : uint32 = 1
                       a : uint32 = 2 }
            }
            """
        )


def test_gateway_route_well_known_bus():
    m = parse_string(
        """
        package p
        node atomic N { tipc type=0x1 instance=0 }
        gateway_route N {
            can id=0x42 bus=kcan dlc=8
            direction=in
        }
        """
    )
    route = m.elements[-1]
    assert route.spec.bus == "kcan"
    assert route.spec.can_id == "0x42"
    assert route.direction.value == "in"


def test_gateway_route_declared_bus():
    m = parse_string(
        """
        package p
        bus myCan kind=can
        node atomic N { tipc type=0x1 instance=0 }
        gateway_route N {
            can id=100 bus=myCan
            direction=out
        }
        """
    )
    route = m.elements[-1]
    assert route.spec.bus == "myCan"


def test_gateway_route_unknown_bus_rejected():
    with pytest.raises(TextXSemanticError, match="unknown bus"):
        parse_string(
            """
            package p
            node atomic N { tipc type=0x1 instance=0 }
            gateway_route N {
                can id=0x42 bus=mysteryBus
                direction=in
            }
            """
        )


def test_gateway_route_kind_mismatch_rejected():
    with pytest.raises(TextXSemanticError, match="bus is kind=flexray"):
        parse_string(
            """
            package p
            node atomic N { tipc type=0x1 instance=0 }
            gateway_route N {
                can id=0x42 bus=mlbevo_gen2_a
                direction=in
            }
            """
        )


def test_gateway_reserved_tipc_rejected():
    with pytest.raises(TextXSemanticError, match="reserved for the gateway"):
        parse_string(
            """
            package p
            node atomic Squatter {
                tipc type=0x80010000 instance=0
            }
            """
        )


def test_flexray_route():
    m = parse_string(
        """
        package p
        node atomic N { tipc type=0x1 instance=0 }
        gateway_route N {
            flexray slot=15 bus=mlbevo_gen2_a channel=A cycle=0 pdu_offset=4
            direction=in
        }
        """
    )
    spec = m.elements[-1].spec
    assert spec.slot_id == 15
    assert spec.channel == "A"
    assert spec.pdu_offset == 4
