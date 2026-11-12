"""gen-manifest's executor.py sidecar follows the cluster's processes.

executor.py is write-once for HAND-EDITS. It used to be write-once full stop
once it had a `_sup` group, so a composition added to the cluster later was
manifested but never reached the supervisor tree (never started) until the
user deleted the file or passed --force. Now: an untouched (pristine) sidecar is
regenerated when the process set changes; a hand-edited one is kept, with a
warning naming the missing/stale processes.
"""
from __future__ import annotations

from artheia.generators.manifest_gen import _inspect_executor, generate_manifest

_ONE = """
package system.ex
node atomic Ex {
    tipc type=0xD0AA0101 instance=0
}
composition A { prototype Ex a }
composition B { prototype Ex b }
cluster Rig {
    composition A app
}
"""
_TWO = _ONE.replace("    composition A app\n", "    composition A app\n    composition B two\n")


def _gen(tmp_path, src):
    art = tmp_path / "system" / "ex" / "component.art"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(src)
    out = tmp_path / "manifest" / "ex" / "manifest.py"
    generate_manifest(str(art), str(out))
    return out.parent / "executor.py"


def test_pristine_sidecar_follows_an_added_composition(tmp_path, capsys):
    ex = _gen(tmp_path, _ONE)
    leaves, pristine = _inspect_executor(ex.read_text())
    assert leaves == {"app"} and pristine
    _gen(tmp_path, _TWO)
    assert "refreshing executor.py" in capsys.readouterr().out
    assert _inspect_executor(ex.read_text()) == ({"app", "two"}, True)


def test_unchanged_cluster_keeps_the_sidecar(tmp_path, capsys):
    ex = _gen(tmp_path, _ONE)
    before = ex.read_text()
    _gen(tmp_path, _ONE)
    assert "keep existing executor.py" in capsys.readouterr().out
    assert ex.read_text() == before


def test_hand_edited_sidecar_is_kept_and_warns(tmp_path, capsys):
    ex = _gen(tmp_path, _ONE)
    edited = ex.read_text().replace("RestartStrategy.ONE_FOR_ONE",
                                    "RestartStrategy.REST_FOR_ONE")
    ex.write_text(edited)
    assert _inspect_executor(edited)[1] is False
    _gen(tmp_path, _TWO)
    err = capsys.readouterr().err
    assert "HAND-EDITED" in err and "'two'" in err
    assert ex.read_text() == edited        # the user's edit survives
