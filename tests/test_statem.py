"""statem block: grammar parsing + AST → dataclass projection.

Covers Phase 3 of the gen_statem MVP (see
``docs/tasks/PROGRESS/gen_statem/design.md``):

* the ``.art`` ``statem { ... }`` block parses cleanly
* :func:`artheia.generators.statem.statem_from_ast` lowers to a
  :class:`StateMSpec`
* validation catches obvious mistakes (unknown initial state, unknown
  target state, halt with timeout)
* duration parsing handles ``ms`` / ``s`` / ``m`` / ``h``
"""
from __future__ import annotations

import pytest
from textx import TextXSemanticError, TextXSyntaxError

from artheia.model import parse_string
from artheia.generators.statem import (
    StateMSpec,
    StateBlock,
    TransitionRule,
    TransitionTarget,
    statem_from_ast,
    _parse_duration_to_ms,
)


# ---- duration helpers --------------------------------------------------------

def test_parse_duration():
    assert _parse_duration_to_ms("0ms") == 0
    assert _parse_duration_to_ms("500ms") == 500
    assert _parse_duration_to_ms("1s") == 1_000
    assert _parse_duration_to_ms("30s") == 30_000
    assert _parse_duration_to_ms("2m") == 120_000
    assert _parse_duration_to_ms("1h") == 3_600_000


def test_parse_duration_rejects_bare_int():
    with pytest.raises(ValueError):
        _parse_duration_to_ms("30")


# ---- grammar smoke tests -----------------------------------------------------

def test_minimal_statem_parses():
    """Smallest valid statem: just states + initial."""
    m = parse_string(
        """
        package p
        node atomic N {
            tipc type=0x1 instance=0
            statem {
                states [OFF, ON]
                initial OFF
            }
        }
        """
    )
    node = m.elements[0]
    assert node.statem is not None
    assert list(node.statem.states) == ["OFF", "ON"]
    # textX resolves `initial=[ID]` to the matching string from `states`.
    assert node.statem.initial == "OFF"


def test_statem_with_event_and_timeout_parses():
    m = parse_string(
        """
        package p
        message Boot { }
        node atomic N {
            tipc type=0x1 instance=0
            statem {
                states [OFF, STARTING, RUNNING]
                initial OFF
                on OFF:
                    event Boot → STARTING after 30s
                on STARTING:
                    timeout → RUNNING
            }
        }
        """
    )
    node = m.elements[1]
    assert node.statem is not None
    assert len(node.statem.on_blocks) == 2

    off_blk = node.statem.on_blocks[0]
    assert off_blk.state == "OFF"
    assert len(off_blk.rules) == 1
    rule = off_blk.rules[0]
    assert rule.event.name == "Boot"
    assert rule.target.state == "STARTING"
    assert rule.target.timeout == "30s"

    starting_blk = node.statem.on_blocks[1]
    assert starting_blk.state == "STARTING"
    assert starting_blk.rules[0].target.state == "RUNNING"


def test_statem_halt_target_parses():
    m = parse_string(
        """
        package p
        message Bye { }
        node atomic N {
            tipc type=0x1 instance=0
            statem {
                states [ALIVE, DEAD]
                initial ALIVE
                on ALIVE:
                    event Bye → halt
            }
        }
        """
    )
    node = m.elements[1]
    target = node.statem.on_blocks[0].rules[0].target
    assert target.halt is True
    # textX leaves the unselected alternative empty: state is "" / None.
    assert not target.state


def test_statem_data_message_ref_parses():
    m = parse_string(
        """
        package p
        message FooData { uint32 retries }
        node atomic N {
            tipc type=0x1 instance=0
            statem {
                states [A, B]
                initial A
                data FooData
            }
        }
        """
    )
    node = m.elements[1]
    assert node.statem.data_type.name == "FooData"


def test_node_without_statem_still_works():
    """The block is optional — nodes that don't need an FSM stay untouched."""
    m = parse_string(
        """
        package p
        node atomic N {
            tipc type=0x1 instance=0
        }
        """
    )
    node = m.elements[0]
    assert node.statem is None


# ---- AST → dataclass projection ---------------------------------------------

def test_statem_from_ast_minimal():
    m = parse_string(
        """
        package p
        node atomic N {
            tipc type=0x1 instance=0
            statem {
                states [OFF, ON]
                initial OFF
            }
        }
        """
    )
    node = m.elements[0]
    spec = statem_from_ast(node)
    assert isinstance(spec, StateMSpec)
    assert spec.states == ("OFF", "ON")
    assert spec.initial == "OFF"
    assert spec.data_fqn is None
    assert spec.blocks == ()


def test_statem_from_ast_full():
    m = parse_string(
        """
        package svc
        message SystemBoot { }
        message StartupComplete { }
        message SmData { uint32 boot_attempts }
        node atomic Sm {
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
                    timeout → halt
            }
        }
        """
    )
    sm_node = m.elements[3]
    spec = statem_from_ast(sm_node)

    assert spec.states == ("OFF", "STARTING", "RUNNING", "DEGRADED", "SHUTDOWN")
    assert spec.initial == "OFF"
    assert spec.data_fqn == "svc.SmData"
    assert len(spec.blocks) == 4

    off = next(b for b in spec.blocks if b.state == "OFF")
    assert off.rules == (
        TransitionRule(
            event_fqn="svc.SystemBoot",
            target=TransitionTarget(state="STARTING", timeout_ms=30_000),
        ),
    )

    starting = next(b for b in spec.blocks if b.state == "STARTING")
    assert len(starting.rules) == 2
    assert starting.rules[0].event_fqn == "svc.StartupComplete"
    assert starting.rules[0].target.state == "RUNNING"
    assert starting.rules[0].target.timeout_ms is None
    assert starting.rules[1].is_timeout is True
    assert starting.rules[1].event_fqn is None
    assert starting.rules[1].target.state == "DEGRADED"

    shutdown = next(b for b in spec.blocks if b.state == "SHUTDOWN")
    assert shutdown.rules[0].is_timeout is True
    assert shutdown.rules[0].target.halt is True
    assert shutdown.rules[0].target.state is None


def test_statem_from_ast_returns_none_when_absent():
    m = parse_string(
        """
        package p
        node atomic N {
            tipc type=0x1 instance=0
        }
        """
    )
    node = m.elements[0]
    assert statem_from_ast(node) is None


# ---- validation rules --------------------------------------------------------

def test_validate_rejects_unknown_initial():
    spec = StateMSpec(
        states=("A", "B"),
        initial="Z",      # not in states
    )
    with pytest.raises(ValueError, match="initial state"):
        spec.validate()


def test_validate_rejects_unknown_target():
    spec = StateMSpec(
        states=("A", "B"),
        initial="A",
        blocks=(StateBlock(
            state="A",
            rules=(TransitionRule(
                event_fqn="p.E",
                target=TransitionTarget(state="X"),  # unknown
            ),),
        ),),
    )
    with pytest.raises(ValueError, match="transition target"):
        spec.validate()


def test_validate_rejects_unknown_on_block():
    spec = StateMSpec(
        states=("A",),
        initial="A",
        blocks=(StateBlock(state="Z", rules=()),),  # unknown
    )
    with pytest.raises(ValueError, match="on-block references unknown"):
        spec.validate()


def test_validate_rejects_duplicate_states():
    spec = StateMSpec(
        states=("A", "B", "A"),
        initial="A",
    )
    with pytest.raises(ValueError, match="duplicate state"):
        spec.validate()


def test_transition_target_rejects_halt_with_state():
    with pytest.raises(ValueError):
        TransitionTarget(halt=True, state="X")


def test_transition_target_rejects_halt_with_timeout():
    with pytest.raises(ValueError):
        TransitionTarget(halt=True, timeout_ms=100)


def test_transition_target_rejects_empty():
    with pytest.raises(ValueError):
        TransitionTarget()  # neither halt nor state


def test_transition_rule_rejects_event_and_timeout_both():
    with pytest.raises(ValueError):
        TransitionRule(
            event_fqn="p.E",
            is_timeout=True,
            target=TransitionTarget(state="X"),
        )


def test_transition_rule_rejects_neither():
    with pytest.raises(ValueError):
        TransitionRule(target=TransitionTarget(state="X"))
