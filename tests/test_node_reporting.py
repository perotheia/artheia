"""NodeDecl `reporting=true|false` — AUTOSAR Reporting/Non-Reporting flag.

Grammar declares an optional ``reporting=BOOL`` field on NodeDecl.
The loader's post-process step (``_apply_node_defaults`` in
``artheia.model.inherit``) fills the default ("true") when the
production didn't match, so every NodeDecl exposes a non-empty
``reporting`` attribute regardless of source form.

Inheritance (``extends``) propagates the field through the same
flatten step that handles other NodeDecl fields: derived inherits
the base's value if it didn't write its own.

Tests verify:
  - omitted ``reporting`` defaults to ``"true"``
  - explicit ``reporting=false`` opts out
  - inheritance carries the base's value
  - derived can override the base
"""
from __future__ import annotations

from artheia.model import parse_string


def _node(model, name: str):
    for el in model.elements:
        if el.__class__.__name__ == "NodeDecl" and el.name == name:
            return el
    raise KeyError(f"no node {name!r}")


def test_reporting_default_true():
    m = parse_string(
        """
        package test.report
        node atomic Defaulted {
            tipc type=0x10010001 instance=0
        }
        """
    )
    assert _node(m, "Defaulted").reporting == "true"


def test_explicit_reporting_true():
    m = parse_string(
        """
        package test.report
        node atomic Reporting {
            tipc type=0x10010002 instance=0
            reporting=true
        }
        """
    )
    assert _node(m, "Reporting").reporting == "true"


def test_non_reporting_opt_out():
    m = parse_string(
        """
        package test.report
        node atomic Quiet {
            tipc type=0x10010003 instance=0
            reporting=false
        }
        """
    )
    assert _node(m, "Quiet").reporting == "false"


def test_reporting_inherited_through_extends():
    """A derived node without its own `reporting` inherits the base's."""
    m = parse_string(
        """
        package test.report
        node atomic SilentBase {
            tipc type=0x10010004 instance=0
            reporting=false
        }
        node atomic Derived extends SilentBase {
            tipc type=0x10010005 instance=0
        }
        """
    )
    assert _node(m, "Derived").reporting == "false"


def test_reporting_override_in_derived():
    """Derived's explicit value beats the inherited one."""
    m = parse_string(
        """
        package test.report
        node atomic SilentBase {
            tipc type=0x10010006 instance=0
            reporting=false
        }
        node atomic LoudDerived extends SilentBase {
            tipc type=0x10010007 instance=0
            reporting=true
        }
        """
    )
    assert _node(m, "LoudDerived").reporting == "true"


def test_default_propagates_through_extends_chain():
    """Three-deep chain where no node mentions reporting — all default true."""
    m = parse_string(
        """
        package test.report
        node atomic Root {
            tipc type=0x10010008 instance=0
        }
        node atomic Mid extends Root {
            tipc type=0x10010009 instance=0
        }
        node atomic Leaf extends Mid {
            tipc type=0x1001000a instance=0
        }
        """
    )
    assert _node(m, "Root").reporting == "true"
    assert _node(m, "Mid").reporting == "true"
    assert _node(m, "Leaf").reporting == "true"
