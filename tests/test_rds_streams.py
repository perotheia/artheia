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


# ---- per-PROTOTYPE writers (two prototypes of one writer node) -----------------

_TWO = """
package system.twocam
node atomic Cam {
    tipc type=0xD0B10101 instance=0
    requires_rds
    rds { stream frames { role=writer chunk_size=1024 history=2 } }
}
node atomic Viewer {
    tipc type=0xD0B10102 instance=0
    requires_rds
    rds {
        stream frames { role=reader from=cam_left.frames  chunk_size=1024 history=2 }
        stream other  { role=reader from=cam_right.frames chunk_size=1024 history=2 }
    }
}
composition TwoCams {
    prototype Cam cam_left
    prototype Cam cam_right
    prototype Viewer viewer
}
"""


def _gen_two(tmp_path):
    from click.testing import CliRunner
    from artheia.cli import main
    art = tmp_path / "system" / "twocam" / "package.art"
    art.parent.mkdir(parents=True)
    art.write_text(_TWO)
    res = CliRunner().invoke(main, [
        "gen-fc", str(art), "--composition", "TwoCams", "--out", str(tmp_path / "out"),
        "--proto-out", str(tmp_path / "proto"), "--ns", "ara::twocam"])
    assert res.exit_code == 0, res.output
    return tmp_path / "out" / "TwoCams"


def test_main_constructs_and_names_every_prototype(tmp_path):
    main_cc = (_gen_two(tmp_path) / "main" / "main.cc").read_text()
    assert "Cam cam_left;" in main_cc and "Cam cam_right;" in main_cc
    assert 'cam_left.set_node_name("cam_left");' in main_cc
    assert 'cam_right.set_node_name("cam_right");' in main_cc


def test_writer_member_keys_on_the_runtime_name(tmp_path):
    hh = (_gen_two(tmp_path) / "lib" / "Cam.hh").read_text()
    assert 'owner_instance(rds_node_name_, "frames")' in hh
    assert "make_frames_writer() const" in hh


def test_reader_members_open_each_writer_prototype(tmp_path):
    hh = (_gen_two(tmp_path) / "lib" / "Viewer.hh").read_text()
    assert '"/Cam_left/frames"' in hh and '"/Cam_right/frames"' in hh


def test_flat_accessors_are_emitted_once_per_stream_name(tmp_path):
    rs = (_gen_two(tmp_path) / "lib" / "RdsStreams.hh").read_text()
    assert rs.count("inline ::ara::rds::StreamSpec frames_spec()") == 1
