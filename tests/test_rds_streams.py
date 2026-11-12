"""RDS stream lowering — a stream's instance is keyed on its WRITER.

A writer's InstanceSpecifier is "/<Writer>/<stream>" (its own identity). A
reader names the writer with `from=<prototype>[.<stream>]` so its generated
handle opens the SAME instance; without `from` it keeps the legacy self-keyed
instance (unchanged output). `from=` on a writer is a parse error.
"""
from __future__ import annotations

import pytest
from textx import TextXSemanticError

from artheia.generators.fc_app import _rds_streams_from_ast
from artheia.model import parse_string

_SRC = """
package test.rds
node atomic Prod {
    tipc type=0x10030001 instance=0
    requires_rds
    rds { stream frames { role=writer chunk_size=1048576 history=4 } }
}
node atomic Cons {
    tipc type=0x10030002 instance=0
    requires_rds
    rds {
        stream frames { role=reader from=frame_producer chunk_size=1048576 history=4 }
        stream img    { role=reader from=cam_rig.cam_front_l chunk_size=4194304 history=2 }
        stream legacy { role=reader chunk_size=64 history=4 }
    }
}
"""


def _node(model, name):
    return next(e for e in model.elements
                if e.__class__.__name__ == "NodeDecl" and e.name == name)


def _by_name(streams):
    return {s["name"]: s for s in streams}


def test_writer_keys_on_its_own_identity():
    m = parse_string(_SRC)
    (w,) = _rds_streams_from_ast(_node(m, "Prod"), "frame_producer")
    assert w["role"] == "writer"
    assert w["instance"] == "/Frame_producer/frames"
    assert w["from"] == ""


def test_reader_from_opens_the_writers_instance():
    m = parse_string(_SRC)
    r = _by_name(_rds_streams_from_ast(_node(m, "Cons"), "frame_consumer"))
    # same instance as the writer above -> the two handles meet in iceoryx
    assert r["frames"]["instance"] == "/Frame_producer/frames"
    assert r["frames"]["from"] == "frame_producer.frames"
    assert (r["frames"]["chunk_size"], r["frames"]["history"]) == (1048576, 4)


def test_reader_from_with_a_differently_named_writer_stream():
    m = parse_string(_SRC)
    r = _by_name(_rds_streams_from_ast(_node(m, "Cons"), "frame_consumer"))
    assert r["img"]["instance"] == "/Cam_rig/cam_front_l"
    assert r["img"]["from"] == "cam_rig.cam_front_l"


def test_reader_without_from_keeps_the_legacy_instance():
    m = parse_string(_SRC)
    r = _by_name(_rds_streams_from_ast(_node(m, "Cons"), "frame_consumer"))
    assert r["legacy"]["instance"] == "/Frame_consumer/legacy"
    assert r["legacy"]["from"] == ""


def test_from_on_a_writer_is_rejected():
    with pytest.raises(TextXSemanticError, match="from="):
        parse_string("""
        package test.rds
        node atomic Bad {
            tipc type=0x10030003 instance=0
            requires_rds
            rds { stream s { role=writer from=other chunk_size=64 } }
        }
        """)
