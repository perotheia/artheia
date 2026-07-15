"""Tests for config-field defaults: the .art `field = value` declaration and the
two generators that read it (gen-schema carries it; gen-config-defaults emits the
first-boot seed). Single source: declare once, consumed by the migration add-rule
+ the seed."""
from __future__ import annotations

import textwrap

from artheia.model.loader import parse_string
from artheia.generators.config_schema import build_config_schema, _field_shape
from artheia.generators.config_defaults import build_config_defaults
import textx


_ART = textwrap.dedent("""
    package test.cfgdef

    message Knobs {
        uint32 step       = 5
        uint32 max_value  = 100
        bool   wrap       = true
        string label      = "hi"
        uint32 plain
    }

    node atomic KnobNode {
        tipc type=0xd0010099 instance=0
        config Knobs
        ports {
        }
    }

    composition Solo {
        prototype KnobNode knob on process P1
    }
""")


def _model():
    return parse_string(_ART)


def _knobs_msg(m):
    for obj in textx.get_children_of_type("MessageDecl", m):
        if obj.name == "Knobs":
            return obj
    raise AssertionError("Knobs message not found")


def test_grammar_parses_field_defaults():
    msg = _knobs_msg(_model())
    by = {f.name: f for f in msg.fields}
    # NUMBER -> int; a bare BoolLit stays the token string 'true'/'false'; a
    # QUOTED string is wrapped in the StrLit rule (so `string = "true"` keeps its
    # string type — the coercer keys on the rule, not the stringified value).
    assert by["step"].default.value == 5
    assert by["label"].default.value.__class__.__name__ == "StrLit"
    assert by["label"].default.value.s == "hi"
    assert by["wrap"].default.value == "true"       # bare bool token
    assert by["plain"].default is None   # no default declared


def test_field_shape_coerces_defaults_by_type():
    shape = {f["name"]: f for f in _field_shape(_knobs_msg(_model()))}
    assert shape["step"]["default"] == 5          # uint -> int
    assert shape["max_value"]["default"] == 100
    assert shape["wrap"]["default"] is True       # BoolLit -> bool
    assert shape["label"]["default"] == "hi"      # string stays string
    assert "default" not in shape["plain"]        # undeclared -> absent


def test_schema_carries_defaults_without_changing_digest():
    m = _model()
    schema = build_config_schema(m)["configs"]["Knobs"]
    digest_with = schema["digest"]
    # The default is metadata: the digest must hash only name/type/repeated.
    # Strip defaults and recompute via the same path -> identical digest.
    from artheia.generators.config_schema import _digest
    stripped = [{"name": f["name"], "type": f["type"], "repeated": f["repeated"]}
                for f in schema["fields"]]
    assert _digest("Knobs", stripped) == digest_with


def test_config_defaults_artifact():
    out = build_config_defaults(_model())
    knob = out["configs"]["knob"]   # keyed by PROTOTYPE name
    assert knob["config_type"] == "Knobs"
    assert knob["digest"].startswith("cfg_")
    # only declared-default fields appear in values; 'plain' is absent.
    assert knob["values"] == {"step": 5, "max_value": 100, "wrap": True,
                              "label": "hi"}


# ---- prebuilt-node args + shared unwrap_literal (regression) ---------------
# A `node prebuilt` carries its argv tail in one `args : string = "..."` param.
# _prebuilt_args_from_ast must yield the STRING tokens, not the StrLit repr
# (grammar wraps a quoted string in StrLit; the string is StrLit.s). Regression
# for the codegen bug where str(ParamLiteral.value) stringified the StrLit
# object → kDefaultArgs = "<textx:artheia.StrLit instance …>" and corrupted argv.

_PREBUILT_ART = textwrap.dedent("""
    package test.prebuilt

    node prebuilt Roudi {
        tipc type=0x8001aa01 instance=0
        path = "/usr/bin/iox-roudi"
        params {
            args : string = "-c /etc/iceoryx/roudi_config.toml"
        }
    }
""")


def _roudi_node():
    m = parse_string(_PREBUILT_ART)
    for obj in textx.get_children_of_type("NodeDecl", m):
        if obj.name == "Roudi":
            return obj
    raise AssertionError("Roudi node not found")


def test_prebuilt_args_render_the_string_not_strlit_repr():
    from artheia.generators.fc_app import _prebuilt_args_from_ast
    args = _prebuilt_args_from_ast(_roudi_node())
    # Whitespace-split argv tokens — the STRING content, never a StrLit repr.
    assert args == ["-c", "/etc/iceoryx/roudi_config.toml"]
    assert not any("StrLit" in a for a in args)


def test_unwrap_literal_strips_grammar_wrappers():
    from artheia.model import unwrap_literal
    node = _roudi_node()
    args_param = next(p for p in node.params if p.name == "args")
    # A quoted string → its content (not the StrLit object / its repr).
    assert unwrap_literal(args_param.default) == "-c /etc/iceoryx/roudi_config.toml"
    # None-safe.
    assert unwrap_literal(None) is None
