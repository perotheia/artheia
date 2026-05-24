"""Node prototype inheritance: ``node X extends Y { tipc ... }``.

textX parses the extends clause as a cross-ref; the loader's
post-process step (artheia.model.inherit.resolve_inheritance)
flattens the chain so generators see standalone-looking NodeDecls.

Covers:
  - Basic inheritance: derived gets base's ports / params / statem
  - Override: derived's own field declarations win wholesale
  - Chains: A extends B extends C
  - Cycle detection: clear error pointing at the chain
"""
from __future__ import annotations

import pytest

from artheia.model import parse_string


def _node(model, name: str):
    for el in model.elements:
        if el.__class__.__name__ == "NodeDecl" and el.name == name:
            return el
    raise KeyError(f"no node {name!r}")


def test_derived_inherits_ports():
    m = parse_string(
        """
        package test.inh
        interface senderReceiver Stream { }
        node atomic Base {
            tipc type=0x10010001 instance=0
            ports {
                sender out provides Stream
            }
        }
        node atomic Derived extends Base {
            tipc type=0x10010002 instance=0
        }
        """
    )
    base = _node(m, "Base")
    derived = _node(m, "Derived")
    assert len(base.ports) == 1
    assert len(derived.ports) == 1
    # The port object is REFERENCED, not deep-copied — derived sees
    # the same textX Port instance as base. That's fine because the
    # port is immutable from gen-app's perspective.
    assert derived.ports[0].name == "out"
    # TIPC is the derived's own.
    assert derived.tipc.type == "0x10010002"


def test_derived_overrides_ports():
    """When derived declares its own ports, base's are NOT copied."""
    m = parse_string(
        """
        package test.inh
        interface senderReceiver Stream { }
        node atomic Base {
            tipc type=0x10010001 instance=0
            ports {
                sender out provides Stream
            }
        }
        node atomic Derived extends Base {
            tipc type=0x10010002 instance=0
            ports {
                receiver in_only requires Stream
            }
        }
        """
    )
    derived = _node(m, "Derived")
    assert len(derived.ports) == 1
    assert derived.ports[0].name == "in_only"
    # Overrode is WHOLESALE — base's `out` is gone, not merged in.
    assert all(p.name != "out" for p in derived.ports)


def test_derived_inherits_flags():
    # Note: kick_off comes BEFORE requires_timers in the grammar (the
    # body's optional fields are positional).
    m = parse_string(
        """
        package test.inh
        node atomic Base {
            tipc type=0x10010001 instance=0
            kick_off
            requires_timers
        }
        node atomic Derived extends Base {
            tipc type=0x10010002 instance=0
        }
        """
    )
    derived = _node(m, "Derived")
    assert bool(derived.requires_timers) is True
    assert bool(derived.kick_off) is True


def test_derived_inherits_statem():
    m = parse_string(
        """
        package test.inh
        node atomic Base {
            tipc type=0x10010001 instance=0
            statem {
                states [OFF, ON]
                initial OFF
            }
        }
        node atomic Derived extends Base {
            tipc type=0x10010002 instance=0
        }
        """
    )
    derived = _node(m, "Derived")
    assert derived.statem is not None
    assert list(derived.statem.states) == ["OFF", "ON"]
    assert derived.statem.initial == "OFF"


def test_chain_inheritance():
    """A extends B extends C — A absorbs C's fields transitively."""
    m = parse_string(
        """
        package test.inh
        interface senderReceiver Stream { }
        node atomic C {
            tipc type=0x10010001 instance=0
            ports {
                sender out provides Stream
            }
        }
        node atomic B extends C {
            tipc type=0x10010002 instance=0
        }
        node atomic A extends B {
            tipc type=0x10010003 instance=0
        }
        """
    )
    a = _node(m, "A")
    assert len(a.ports) == 1
    assert a.ports[0].name == "out"


def test_cycle_detected():
    """A → B → A surfaces as a clear ValueError, not an infinite loop."""
    # textX-level cross-ref resolves both directions; the cycle is
    # caught when resolve_inheritance walks the chain.
    with pytest.raises(ValueError, match="extends cycle"):
        parse_string(
            """
            package test.inh
            node atomic A extends B {
                tipc type=0x10010001 instance=0
            }
            node atomic B extends A {
                tipc type=0x10010002 instance=0
            }
            """
        )


def test_no_extends_unchanged():
    """A node WITHOUT extends behaves identically to before the flatten
    pass — sanity that the post-process step is a no-op for plain
    NodeDecls."""
    m = parse_string(
        """
        package test.inh
        interface senderReceiver Stream { }
        node atomic Plain {
            tipc type=0x10010001 instance=0
            ports {
                sender out provides Stream
            }
        }
        """
    )
    n = _node(m, "Plain")
    assert getattr(n, "base", None) is None
    assert len(n.ports) == 1
