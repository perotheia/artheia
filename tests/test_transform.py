"""Regression tests for artheia/manifest/transform.py.

Two halves:

- **Legacy API** (Add/Remove/Override/apply_ops) — the flat-list
  layer-merge engine that `manifest/layer.py` still uses. Must keep
  working until all call sites migrate to the structured DSL.

- **Structured DSL** (Layer.squash + Append/Remove on sets +
  Undefined/Default value markers) — ported back from
  theia_runtime/. Exercises the bits we'll lean on as
  services/manifest and demo/manifest move over.

Note: this file imports from `artheia.manifest.transform` directly,
NOT from `artheia.manifest`. The package `__init__` triggers the
FC loader which currently crashes on the post-symlink-consolidation
layout (see TODO/system-art-aggregation.md). The transform module is
self-contained — no need to involve the broken loader to test it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest

from artheia.manifest.transform import (
    Add,
    Append,
    Default,
    Defer,
    Identifiable,
    Layer,
    Op,
    Override,
    Remove,
    SetTransformTypes,
    Undefined,
    apply_ops,
    merge_field,
    set_squash,
    transform_base,
    transform_set,
)


# ---------------------------------------------------------------------------
# Test fixtures — minimal Identifiable + Layer subclasses
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Widget(Identifiable):
    """Plain Identifiable for the legacy-API tests."""
    name: str
    width: int = 0


# Layer-flavoured test types live below — they're separate classes so
# the legacy tests can't accidentally exercise structured-DSL paths.


@dataclass(eq=False)
class Gadget(Identifiable):
    """An Identifiable that's also a Layer in the structured-DSL sense.

    Layer.squash recurses through `__dataclass_fields__`; we add a
    nested set to exercise that path.
    """
    name: str = ""
    weight: int = 0


@dataclass
class Box(Layer):
    """Top-level spec: a name + a set of nested Gadgets."""
    name: str = ""
    gadgets: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Legacy API — apply_ops over lists
# ---------------------------------------------------------------------------


def test_add_appends_when_no_existing_identity():
    base = [Widget(name="a", width=1)]
    out = apply_ops(base, [Add(Widget(name="b", width=2))])
    assert [w.name for w in out] == ["a", "b"]
    assert out[1].width == 2


def test_add_merges_when_identity_collides():
    """Add of an existing-identity element field-merges into it
    (legacy `_merge_element` semantics)."""
    base = [Widget(name="a", width=1)]
    out = apply_ops(base, [Add(Widget(name="a", width=99))])
    assert len(out) == 1
    assert out[0].width == 99


def test_remove_drops_by_identity():
    base = [Widget(name="a", width=1), Widget(name="b", width=2)]
    # Remove takes either an Identifiable (new) or a bare identity
    # (legacy callers like manifest/layer.py pass a string).
    out = apply_ops(base, [Remove(Widget(name="a"))])
    assert [w.name for w in out] == ["b"]


def test_remove_silently_ignores_missing_identity():
    base = [Widget(name="a", width=1)]
    out = apply_ops(base, [Remove(Widget(name="nonexistent"))])
    assert [w.name for w in out] == ["a"]


def test_override_patches_named_fields():
    base = [Widget(name="a", width=1), Widget(name="b", width=2)]
    out = apply_ops(base, [Override(identity="a", patch={"width": 99})])
    assert out[0].width == 99
    assert out[1].width == 2  # untouched


def test_override_silently_ignores_missing_identity():
    """If no element matches the identity, Override is a no-op — by
    design (use Add for create-or-update)."""
    base = [Widget(name="a", width=1)]
    out = apply_ops(base, [Override(identity="nonexistent", patch={"width": 0})])
    assert out[0].width == 1


def test_ops_compose_in_declared_order():
    """add/remove/override in one list — order matters."""
    base = [Widget(name="a", width=1), Widget(name="b", width=2)]
    out = apply_ops(base, [
        Add(Widget(name="c", width=3)),
        Remove(Widget(name="a")),
        Override(identity="b", patch={"width": 99}),
    ])
    assert sorted((w.name, w.width) for w in out) == [("b", 99), ("c", 3)]


def test_add_is_append_alias():
    """Add is the legacy name for Append. They must be the same object."""
    assert Add is Append


# ---------------------------------------------------------------------------
# Structured DSL — Append / Remove on sets
# ---------------------------------------------------------------------------


def test_append_to_empty_set_adds():
    out = Append(Gadget(name="a", weight=1)).apply(set())
    assert {g.name for g in out} == {"a"}


def test_append_merges_existing_identity():
    """Same-name Gadgets squash via Layer.squash — later value wins
    on changed fields, base wins on Undefined fields."""
    s: set = set()
    s = Append(Gadget(name="a", weight=1)).apply(s)
    s = Append(Gadget(name="a", weight=99)).apply(s)
    assert len(s) == 1
    g = next(iter(s))
    # Layer.squash: explicit non-default fields on the new value win.
    assert g.weight == 99


def test_remove_drops_by_set_identify():
    s = {Gadget(name="a", weight=1), Gadget(name="b", weight=2)}
    s = Remove(Gadget(name="a")).apply(s)
    assert {g.name for g in s} == {"b"}


def test_set_identify_defaults_to_hash_of_identity():
    """Subclasses that set only `_identity_field` (the default "name")
    must get a working `_set_identify` automatically — that's the
    compat bridge for legacy Identifiable subclasses."""
    g1 = Gadget(name="x", weight=1)
    g2 = Gadget(name="x", weight=2)  # same identity, different data
    g3 = Gadget(name="y", weight=1)
    assert g1._set_identify == g2._set_identify
    assert g1._set_identify != g3._set_identify


# ---------------------------------------------------------------------------
# Layer.squash — recursive composition
# ---------------------------------------------------------------------------


def test_squash_replaces_scalar_when_other_sets_it():
    base = Box(name="base", gadgets=set())
    other = Box(name="other", gadgets=set())
    out = base.squash(other)
    assert out.name == "other"


def test_squash_keeps_scalar_when_other_undefined():
    """An Undefined() on `other` means "inherit base's value"."""
    base = Box(name="base", gadgets=set())
    other = Box(name=Undefined(), gadgets=set())  # type: ignore[arg-type]
    out = base.squash(other)
    assert out.name == "base"


def test_squash_applies_set_transforms_from_layer():
    """The classic case: base has a concrete set; layer has
    {Append(new), Remove(old)} — squash applies them."""
    base = Box(name="b", gadgets={Gadget(name="x", weight=1), Gadget(name="y", weight=2)})
    layer = Box(
        name=Undefined(),  # type: ignore[arg-type]
        gadgets=cast(set, {
            Remove(Gadget(name="x")),
            Append(Gadget(name="z", weight=3)),
        }),
    )
    out = base.squash(layer)
    assert {g.name for g in out.gadgets} == {"y", "z"}


def test_squash_concrete_set_on_layer_replaces_base():
    """If `layer.gadgets` is a plain (non-transform) set, it REPLACES
    base's set wholesale. That's `transform_set`'s "other is simple"
    branch."""
    base = Box(name="b", gadgets={Gadget(name="x", weight=1)})
    layer = Box(name="b", gadgets={Gadget(name="z", weight=3)})
    out = base.squash(layer)
    assert {g.name for g in out.gadgets} == {"z"}


def test_squash_chains_left_to_right():
    """A.squash(B).squash(C): B applies onto A, then C applies onto
    that. The legacy mosaic raj_syscomp.py pattern."""
    base = Box(name="a", gadgets={Gadget(name="x")})
    layer1 = Box(name=Undefined(), gadgets=cast(set, {Append(Gadget(name="y"))}))  # type: ignore[arg-type]
    layer2 = Box(name=Undefined(), gadgets=cast(set, {Append(Gadget(name="z"))}))  # type: ignore[arg-type]
    out = base.squash(layer1).squash(layer2)
    assert {g.name for g in out.gadgets} == {"x", "y", "z"}


# ---------------------------------------------------------------------------
# Value markers — Undefined / Default
# ---------------------------------------------------------------------------


def test_undefined_equal_regardless_of_type_param():
    """Undefined instances are interchangeable — they're sentinel
    values, not data."""
    assert Undefined() == Undefined()
    assert hash(Undefined()) == hash(Undefined())


def test_default_carries_a_concrete_value():
    d = Default(42)
    assert d.default == 42


def test_default_equality_compares_inner_value():
    assert Default(42) == Default(42)
    assert Default(42) != Default(43)


def test_merge_field_layer_wins_when_set():
    assert merge_field("base", "layer") == "layer"


def test_merge_field_base_wins_when_layer_undefined():
    assert merge_field("base", Undefined()) == "base"


def test_merge_field_both_undefined_returns_undefined():
    out = merge_field(Undefined(), Undefined())
    assert isinstance(out, Undefined)


# ---------------------------------------------------------------------------
# transform_set / set_squash — the set-level engine that Layer.squash
# delegates to.
# ---------------------------------------------------------------------------


def test_transform_base_passes_concrete_set_through():
    """A set with no transforms is returned as-is."""
    s = {Gadget(name="a")}
    out = transform_base(s)
    assert out == s


def test_transform_base_materializes_transforms():
    """A set of {Append(x), Remove(y)} is "rendered" against an
    initially-empty set."""
    s = cast(set, {Append(Gadget(name="a")), Append(Gadget(name="b"))})
    out = transform_base(s)
    assert {g.name for g in out} == {"a", "b"}


def test_transform_base_empty_when_undefined():
    """Treats Undefined() as empty — useful when a base layer hasn't
    been set yet."""
    assert transform_base(Undefined()) == set()


def test_transform_set_applies_layer_over_base():
    base = {Gadget(name="a"), Gadget(name="b")}
    layer = cast(set, {Append(Gadget(name="c")), Remove(Gadget(name="a"))})
    out = transform_set(base, layer)
    assert {g.name for g in out} == {"b", "c"}


def test_set_squash_unions_when_both_simple():
    """When both base and other are plain sets (no transforms), squash
    unions them — useful for additive composition without explicit
    Append/Remove."""
    base = {Gadget(name="a")}
    other = {Gadget(name="b")}
    out = set_squash(base, other)
    assert {g.name for g in out} == {"a", "b"}


# ---------------------------------------------------------------------------
# Defer — lazy late-bound values
# ---------------------------------------------------------------------------


def test_defer_invokes_callable_on_context():
    """Defer is a marker; calling it with a context resolves it."""
    d = Defer(lambda ctx: ctx + 1)
    assert d(41) == 42


def test_defer_short_circuits_squash():
    """If `other` is a Defer, squash returns it unchanged — the
    deferral propagates to whatever upper layer eventually resolves."""
    base = Box(name="b")
    deferred = Defer(lambda ctx: Box(name=ctx))
    out = base.squash(deferred)
    assert out is deferred


# ---------------------------------------------------------------------------
# End-to-end — SoftwareSpecification with real manifest types
# ---------------------------------------------------------------------------


def test_software_specification_is_importable():
    """Sanity: ``SoftwareSpecification`` is a ``Layer`` subclass with
    a working ``.squash()`` method."""
    from artheia.manifest.rig import SoftwareSpecification, VehicleIdentity
    spec = SoftwareSpecification(vehicle=VehicleIdentity(name="test"))
    assert isinstance(spec, Layer)
    assert hasattr(spec, "squash")
    other = SoftwareSpecification()
    out = spec.squash(other)
    assert out.vehicle.name == "test"


def test_software_specification_squashes_machines_via_transforms():
    """End-to-end: structured-DSL composition with real manifest types.

    Mirrors the legacy mosaic raj_syscomp.py pattern:

      base = SoftwareSpecification(machines={Append(MachineManifest(...))})
      layer = SoftwareSpecification(machines={Append(...), Remove(...)})
      result = base.squash(layer)

    Validates that ``identifiable_dataclass`` (phase 2 of the DSL
    recovery) makes ``MachineManifest`` instances hashable so they can
    survive ``Append.apply()`` and end up in the result set.
    """
    from artheia.manifest.machine import (
        CpuArchitecture,
        CpuResource,
        HardwareResource,
        MachineManifest,
    )
    from artheia.manifest.rig import SoftwareSpecification, VehicleIdentity

    base = SoftwareSpecification(
        vehicle=VehicleIdentity(name="base"),
        machines=cast(set[SetTransformTypes], {
            Append(MachineManifest(
                name="default_host",
                hardware=HardwareResource(
                    cpu=CpuResource(architecture=CpuArchitecture.X86_64)
                ),
            )),
        }),
    )

    layer = SoftwareSpecification(
        vehicle=VehicleIdentity(name="demo", make="theia", model="gen_server-demo"),
        machines=cast(set[SetTransformTypes], {
            Append(MachineManifest(
                name="demo_host",
                hardware=HardwareResource(
                    cpu=CpuResource(architecture=CpuArchitecture.AARCH64)
                ),
            )),
            Remove(MachineManifest(name="default_host")),
        }),
    )

    out = base.squash(layer)

    # Vehicle identity from layer (overrides base).
    assert out.vehicle.name == "demo"
    assert out.vehicle.make == "theia"

    # Machines: default_host removed, demo_host added.
    names = {m.name for m in out.machines}
    assert names == {"demo_host"}, f"expected {{demo_host}}, got {names}"


def test_demo_software_applications_merge_correctly():
    """``DemoSpecLayer`` Appends an ``ApplicationManifest`` with the
    same identity as ``FcSoftware``'s — squash must MERGE the
    components lists (FC + demo), not replace base with the layer's
    smaller list. ``Layer.squash``'s ``list[Identifiable]`` branch
    (added in phase 4) is what makes this work."""
    from demo.manifest.rig import DemoSoftware
    rig = DemoSoftware.to_rig()
    assert len(rig.applications) == 1
    app = rig.applications[0]
    assert app.host_machine == "demo_host"
    # 18 FC components from FcSoftware + 3 demo from DemoSpecLayer.
    assert len(app.components) == 21
    names = {c.name for c in app.components}
    # Spot-check both sides of the merge.
    assert {"core", "com", "phm"}.issubset(names)        # FC side
    assert {"demo_p1", "demo_p2", "demo_p3"}.issubset(names)  # demo side


def test_demo_software_to_rig_equivalent_to_legacy_demo_rig():
    """Round-trip: ``DemoSoftware.to_rig()`` (new path) produces a
    :class:`Rig` shape equivalent to the legacy ``DemoRig`` (built via
    ``merge_layers(PlatformBase, [DemoLayer])``).

    Equivalence is checked field-by-field. The test stays loose
    (set-of-names rather than ordered-list equality) because the
    new path's deterministic-sort key may produce a different order
    than legacy ``apply_ops``; what matters is the same set of items.
    """
    from demo.manifest.rig import DemoRig, DemoSoftware

    materialized = DemoSoftware.to_rig()

    assert materialized.vehicle == DemoRig.vehicle

    assert {m.name for m in materialized.machines} == \
           {m.name for m in DemoRig.machines}

    assert {e.name for e in materialized.execution_manifests} == \
           {e.name for e in DemoRig.execution_manifests}

    assert {s.name for s in materialized.supervisors} == \
           {s.name for s in DemoRig.supervisors}


def test_identifiable_dataclass_makes_instances_hashable():
    """Direct check on the ``identifiable_dataclass`` decorator: an
    ``Identifiable`` subclass decorated with it is hashable and
    identity-comparable (vs the default ``@dataclass`` which clobbers
    ``__hash__``)."""
    from artheia.manifest.machine import MachineManifest
    m1 = MachineManifest(name="x")
    m2 = MachineManifest(name="x")
    m3 = MachineManifest(name="y")

    # Hashable (the whole point of phase 2).
    assert hash(m1) == hash(m2)
    assert hash(m1) != hash(m3)

    # Identity-equal (two Machines with the same name are "the same"
    # for set-membership purposes).
    assert m1 == m2
    assert m1 != m3

    # Cross-type identity never equal — even with the same name.
    from artheia.manifest.application import SwComponent
    sw = SwComponent(name="x", bazel_target="//x")
    assert m1 != sw  # different types — identity comparison rejects
    assert m1 != "x"  # NotImplemented path
