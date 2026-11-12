"""FC-namespace enum aliases don't clash when two enums share a value label.

proto3 scopes enum values package-wide, and the .proto artheia emits prefixes
them (ColorA_NONE / ColorB_NONE), so the wire side was always legal. The C++
convenience aliases in <pkg>_codecs.hh flattened every label into the FC
namespace (`inline constexpr ColorA NONE`), so a second `NONE` was a
redefinition. A duplicated label now gets an enum-qualified alias; a unique one
keeps the bare alias (existing generated code doesn't move).
"""
from __future__ import annotations

from click.testing import CliRunner

from artheia.cli import main

_ART = """
package system.ea

enum ColorA {
    NONE = 0
    RED  = 1
}
enum ColorB {
    NONE = 0
    BLUE = 1
}
message Paint {
    ColorA first  = 1
    ColorB second = 2
}
interface clientServer EaIf {
    operation Get(in p: Paint) returns Paint
}
node atomic EaCtrl {
    tipc type=0xD0EA0101 instance=0
    ports { server ctl provides EaIf }
}
"""


def _codecs(tmp_path):
    art = tmp_path / "system" / "ea" / "package.art"
    art.parent.mkdir(parents=True)
    art.write_text(_ART)
    res = CliRunner().invoke(main, [
        "gen-fc-lib", str(art), "--out", str(tmp_path / "src"),
        "--proto-out", str(tmp_path / "proto"), "--ns", "ara::ea"])
    assert res.exit_code == 0, res.output
    (hh,) = (tmp_path / "src" / "lib").glob("*_codecs.hh")
    return hh.read_text()


def test_duplicate_label_is_enum_qualified(tmp_path):
    hh = _codecs(tmp_path)
    assert "inline constexpr ColorA ColorA_NONE = " in hh
    assert "inline constexpr ColorB ColorB_NONE = " in hh
    assert "inline constexpr ColorA NONE = " not in hh
    assert "inline constexpr ColorB NONE = " not in hh


def test_unique_labels_keep_the_bare_alias(tmp_path):
    hh = _codecs(tmp_path)
    assert "inline constexpr ColorA RED = " in hh
    assert "inline constexpr ColorB BLUE = " in hh
