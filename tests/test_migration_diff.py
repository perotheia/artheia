"""Tests for artheia gen-migration (generators/migration_diff.py).

The schema-diff scaffolder: given OLD and NEW gen-schema outputs, derive the
per-node transform rules. Proto tags are POSITIONAL (field order == tag), so the
diff is by index: same-tag/diff-name -> rename, appended -> add, truncated tail
-> remove, same-tag/diff-type -> custom stub. Unchanged digests + new-only
configs emit nothing.
"""
from __future__ import annotations

import json

from artheia.generators.migration_diff import diff_schemas, generate_migrations


def _schema(configs):
    return {"configs": configs}


def _cfg(digest, fields, nodes=("n",)):
    return {"digest": digest, "proto_type": "p", "art_package": "a",
            "nodes": list(nodes),
            "fields": [{"name": n, "type": t, "repeated": False}
                       for (n, t) in fields]}


def test_add_field():
    old = _schema({"C": _cfg("d1", [("a", "uint32"), ("b", "string")])})
    new = _schema({"C": _cfg("d2", [("a", "uint32"), ("b", "string"),
                                    ("c", "uint32")])})
    t = diff_schemas(old, new)["C"]
    assert t["from_digest"] == "d1" and t["to_digest"] == "d2"
    assert t["rules"] == [{"op": "add", "field": "c", "default": 0}]
    assert "_review" not in t   # mechanical add needs no confirmation


def test_add_field_defaults_by_type():
    old = _schema({"C": _cfg("d1", [("a", "uint32")])})
    new = _schema({"C": _cfg("d2", [("a", "uint32"), ("s", "string"),
                                    ("f", "bool")])})
    rules = diff_schemas(old, new)["C"]["rules"]
    assert {"op": "add", "field": "s", "default": ""} in rules
    assert {"op": "add", "field": "f", "default": False} in rules


def test_remove_tail_field():
    old = _schema({"C": _cfg("d1", [("a", "uint32"), ("b", "string")])})
    new = _schema({"C": _cfg("d2", [("a", "uint32")])})
    t = diff_schemas(old, new)["C"]
    assert t["rules"] == [{"op": "remove", "field": "b"}]


def test_rename_same_tag():
    # same tag (index 1), different name -> rename + a review note.
    old = _schema({"C": _cfg("d1", [("a", "uint32"), ("name", "string")])})
    new = _schema({"C": _cfg("d2", [("a", "uint32"), ("tag", "string")])})
    t = diff_schemas(old, new)["C"]
    assert t["rules"] == [{"op": "rename", "from": "name", "to": "tag"}]
    assert t["_review"] and "rename" in t["_review"][0].lower()


def test_type_change_emits_custom_stub():
    old = _schema({"C": _cfg("d1", [("a", "uint32")])})
    new = _schema({"C": _cfg("d2", [("a", "string")])})  # uint32 -> string
    t = diff_schemas(old, new)["C"]
    assert t["rules"][0]["op"] == "custom"
    assert t["rules"][0]["fn"] == "fixup_a"
    assert t["_review"]


def test_unchanged_digest_emits_nothing():
    old = _schema({"C": _cfg("same", [("a", "uint32")])})
    new = _schema({"C": _cfg("same", [("a", "uint32")])})
    assert diff_schemas(old, new) == {}


def test_new_config_skipped():
    # a config only in NEW is a fresh binding — no stored old value to migrate.
    old = _schema({})
    new = _schema({"C": _cfg("d2", [("a", "uint32")])})
    assert diff_schemas(old, new) == {}


def test_dropped_config_skipped():
    old = _schema({"C": _cfg("d1", [("a", "uint32")])})
    new = _schema({})
    assert diff_schemas(old, new) == {}


def test_combined_rename_and_add():
    old = _schema({"C": _cfg("d1", [("poll", "uint32"), ("name", "string")])})
    new = _schema({"C": _cfg("d2", [("poll", "uint32"), ("tag", "string"),
                                    ("on", "bool")])})
    rules = diff_schemas(old, new)["C"]["rules"]
    assert {"op": "rename", "from": "name", "to": "tag"} in rules
    assert {"op": "add", "field": "on", "default": False} in rules


def test_generate_writes_files_and_build(tmp_path):
    old = _schema({"C": _cfg("d1", [("a", "uint32")], nodes=("counter",))})
    new = _schema({"C": _cfg("d2", [("a", "uint32"), ("b", "uint32")],
                             nodes=("counter",))})
    fp = tmp_path / "from.json"; fp.write_text(json.dumps(old))
    tp = tmp_path / "to.json"; tp.write_text(json.dumps(new))
    written = generate_migrations(str(fp), str(tp), str(tmp_path))
    assert "C" in written
    out = json.loads((tmp_path / "counter_v1_to_v2.json").read_text())
    assert out["rules"] == [{"op": "add", "field": "b", "default": 0}]
    # BUILD gets the managed block + this node's plugin entry.
    build = (tmp_path / "BUILD.bazel").read_text()
    assert "gen-migration plugins (managed)" in build
    assert 'migration_plugin(name = "counter"' in build


def test_build_merge_preserves_other_nodes(tmp_path):
    # A pre-existing managed block with node 'other' must survive a diff that
    # only touches 'counter' (merge, not replace).
    build = tmp_path / "BUILD.bazel"
    build.write_text(
        'load(":plugin.bzl", "migration_plugin")\n\n'
        "# >>> gen-migration plugins (managed) >>>\n"
        'migration_plugin(name = "other", src = "other_v1_to_v2.cc")\n'
        "# <<< gen-migration plugins (managed) <<<\n")
    old = _schema({"C": _cfg("d1", [("a", "uint32")], nodes=("counter",))})
    new = _schema({"C": _cfg("d2", [("a", "uint32"), ("b", "uint32")],
                             nodes=("counter",))})
    fp = tmp_path / "from.json"; fp.write_text(json.dumps(old))
    tp = tmp_path / "to.json"; tp.write_text(json.dumps(new))
    generate_migrations(str(fp), str(tp), str(tmp_path))
    text = build.read_text()
    assert 'migration_plugin(name = "counter"' in text
    assert 'migration_plugin(name = "other"' in text   # preserved
