"""Tests for the manifest composition algebra + the orthogonal ARA model.

Covers the engine (:mod:`artheia.manifest.algebra`) — ConfigField precedence,
Layer.combine, set edits, EmptySet identity, simplify, validate — and the
orthogonal deployment model (:mod:`artheia.manifest.deployment`) — per-axis
combine + cross-axis invariants.
"""
from __future__ import annotations

from dataclasses import field

import pytest

from artheia.manifest.algebra import (
    Append,
    ConfigField,
    Default,
    Defer,
    EmptySet,
    Explicit,
    Identifiable,
    LayerMergeError,
    Remove,
    Undefined,
    empty_set,
    identifiable_dataclass,
    validate,
)


# --------------------------------------------------------------------------
# ConfigField precedence (the field-level monoid).
# --------------------------------------------------------------------------

def test_explicit_over_anything_wins():
    assert Undefined().combine(Explicit(5)).value == 5
    assert Default(1).combine(Explicit(5)).value == 5
    assert Explicit(1).combine(Explicit(5)).value == 5


def test_undefined_upper_inherits_lower():
    assert Explicit(3).combine(Undefined()).value == 3
    assert isinstance(Undefined().combine(Undefined()), Undefined)


def test_default_upper_loses_to_explicit_base_else_carries():
    assert Explicit(1).combine(Default(9)).value == 1   # concrete base beats fallback
    assert Default(9).combine(Default(2)).default == 2   # else upper fallback carries
    assert isinstance(Undefined().combine(Default(9)), Default)


def test_defer_upper_wins():
    out = Explicit(1).combine(Defer(lambda ctx: 7))
    assert isinstance(out, Defer)


def test_undefined_is_monoid_identity():
    # X <> mempty == X  and  mempty <> X == X
    x = Explicit(42)
    assert x.combine(Undefined()).value == 42
    assert Undefined().combine(x).value == 42


def test_simplify_raises_on_undefined_and_defer():
    with pytest.raises(LayerMergeError):
        Undefined().simplify("f")
    with pytest.raises(LayerMergeError):
        Defer(lambda c: 1).simplify("f")
    assert Default(3).simplify("f") == 3
    assert Explicit(3).simplify("f") == 3


# --------------------------------------------------------------------------
# Layer.combine + Identifiable set edits.
# --------------------------------------------------------------------------

@identifiable_dataclass
class Pkg(Identifiable):
    name: str
    binary: ConfigField = field(default_factory=Undefined)


@identifiable_dataclass
class Box(Identifiable):
    name: str
    items: object = field(default_factory=empty_set)


def test_scalar_field_right_biased():
    a = Pkg(name="p", binary=Explicit("old"))
    b = Pkg(name="p", binary=Explicit("new"))
    assert a.combine(b).binary.value == "new"


def test_append_merges_by_identity():
    base = Box(name="b", items={Pkg(name="probe", binary=Explicit("v1"))})
    over = Box(name="b", items={Append(Pkg(name="probe", binary=Explicit("v2")))})
    merged = {p.name: p.binary.value for p in base.combine(over).items}
    assert merged == {"probe": "v2"}


def test_append_adds_new_identity():
    base = Box(name="b", items={Pkg(name="a", binary=Explicit("a"))})
    over = Box(name="b", items={Append(Pkg(name="c", binary=Explicit("c")))})
    assert {p.name for p in base.combine(over).items} == {"a", "c"}


def test_remove_drops_by_identity():
    base = Box(name="b", items={Pkg(name="a"), Pkg(name="c")})
    over = Box(name="b", items={Remove(Pkg(name="a"))})
    assert {p.name for p in base.combine(over).items} == {"c"}


# --------------------------------------------------------------------------
# EmptySet — the set identity.
# --------------------------------------------------------------------------

def test_emptyset_inherits_base_set_on_combine():
    base = Box(name="b", items={Pkg(name="a")})
    over = Box(name="b")  # items defaults to EmptySet
    # combining a no-contribution layer must KEEP the base set, not wipe it.
    assert {p.name for p in base.combine(over).items} == {"a"}


def test_emptyset_simplifies_to_empty_frozenset():
    assert EmptySet().simplify("x") == frozenset()
    box = Box(name="b")
    assert box.simplify().items == frozenset()


def test_plain_sets_union_by_identity():
    # Two plain (non-edit) sets UNION — the set monoid. This is what makes the
    # orthogonal base ⊕ base composition work (services axis + demo axis merge
    # rather than one replacing the other). An empty plain set contributes
    # nothing (inherits the base); to CLEAR a member, use Remove(...).
    base = Box(name="b", items={Pkg(name="a")})
    assert base.combine(Box(name="b", items=set())).items == {Pkg(name="a")}
    over = Box(name="b", items={Pkg(name="c")})
    assert {p.name for p in base.combine(over).items} == {"a", "c"}


def test_remove_clears_a_member():
    base = Box(name="b", items={Pkg(name="a"), Pkg(name="c")})
    over = Box(name="b", items={Remove(Pkg(name="a"))})
    assert {p.name for p in base.combine(over).items} == {"c"}


def test_remove_then_append_same_identity_is_deterministic_replace():
    # {Remove(X), Append(X')} in one (unordered) edit set must deterministically
    # REPLACE: Remove runs before Append, so the new member survives. Run a few
    # times since set iteration order can vary between constructions.
    base = Box(name="b", items={Pkg(name="svc", binary=Explicit("old"))})
    for _ in range(8):
        over = Box(name="b", items={
            Remove(Pkg(name="svc")),
            Append(Pkg(name="svc", binary=Explicit("new"))),
        })
        result = list(base.combine(over).items)
        assert len(result) == 1 and result[0].binary.value == "new"


# --------------------------------------------------------------------------
# validate — structural checks on the unmaterialized layer.
# --------------------------------------------------------------------------

def test_validate_flags_required_undefined():
    issues = validate(Pkg(name="x"))   # binary is Undefined, no default
    assert any("binary" in i.path and "Undefined" in i.message for i in issues)


def test_validate_clean_when_defaults_present():
    assert validate(Box(name="b")) == []   # items EmptySet, name set -> clean


def test_validate_flags_unresolved_defer():
    issues = validate(Pkg(name="x", binary=Defer(lambda c: "v")))
    assert any("Defer" in i.message for i in issues)


def test_validate_recurses_into_set_members():
    box = Box(name="b", items={Pkg(name="bad")})   # member's binary Undefined
    issues = validate(box)
    assert any("items(0).binary" in i.path for i in issues)


# --------------------------------------------------------------------------
# Orthogonal deployment model — per-axis combine + cross-axis invariants.
# --------------------------------------------------------------------------

from artheia.manifest.deployment import (  # noqa: E402
    ApplicationLayer,
    ApplicationSetLayer,
    DeploymentLayer,
    DeploymentTarget,
    ExecutionLayer,
    MachineLayer,
    MachineSetLayer,
    ProcessLayer,
    ServiceInstanceLayer,
    ServiceLayer,
    VerifyError,
    _members,
)


def _proc(name, machine, cores=frozenset()):
    return ProcessLayer(
        name=name, executable=Explicit(f"//{name}"), start_cmd=Explicit(f"bin/{name}"),
        function_group=Explicit("app"), machine=Explicit(machine), cpu_affinity=set(cores),
    )


def _base():
    return DeploymentLayer(
        machines=MachineSetLayer(machines={MachineLayer(name="central", cores={0, 1, 2, 3})}),
        execution=ExecutionLayer(processes={_proc("counter", "central", {0, 1})}),
    )


def test_axes_compose_independently():
    over = DeploymentLayer(
        machines=MachineSetLayer(machines={Append(MachineLayer(name="compute", cores={0}))}),
        execution=ExecutionLayer(processes={Append(_proc("perc", "compute", {0}))}),
    )
    dep = _base().combine(over)
    assert {m.name for m in _members(dep.machines.machines)} == {"central", "compute"}
    assert {p.name for p in _members(dep.execution.processes)} == {"counter", "perc"}


def test_untouched_axis_is_inherited():
    # an overlay that only edits execution must NOT wipe the machines axis.
    over = DeploymentLayer(execution=ExecutionLayer(processes={Append(_proc("x", "central"))}))
    dep = _base().combine(over)
    assert {m.name for m in _members(dep.machines.machines)} == {"central"}


def test_clean_deployment_validates():
    assert [i for i in validate(_base()) if i.severity == "error"] == []


def test_simplify_yields_frozen_target():
    tgt = _base().simplify()
    assert isinstance(tgt, DeploymentTarget)
    assert {m.name for m in tgt.machines.machines} == {"central"}


def test_invariant_process_on_undeclared_machine():
    bad = _base().combine(DeploymentLayer(
        execution=ExecutionLayer(processes={Append(_proc("ghost", "nope"))})))
    msgs = [i.message for i in validate(bad)]
    assert any("not declared in machines axis" in m for m in msgs)


def test_invariant_affinity_core_absent():
    bad = _base().combine(DeploymentLayer(
        execution=ExecutionLayer(processes={Append(_proc("hog", "central", {99}))})))
    msgs = [i.message for i in validate(bad)]
    assert any("affinity core" in m and "absent" in m for m in msgs)


def test_invariant_service_owner_missing():
    bad = _base().combine(DeploymentLayer(
        service=ServiceLayer(instances={Append(ServiceInstanceLayer(
            name="svc", interface=Explicit("i"), instance_id=Explicit(1),
            endpoint=Explicit("e"), provided_by=Explicit("missing")))})))
    msgs = [i.message for i in validate(bad)]
    assert any("not in execution axis" in m for m in msgs)


def test_invariant_empty_application_warns_not_errors():
    """An application bundling zero processes (an empty composition, post-import)
    is a WARNING — surfaced so a forgotten process wiring is visible, but NOT a
    hard error: the bare-supervisor bootstrap legitimately ships an empty `apps`
    AA. (Also documents the empty-set that used to crash simplify() with
    'unhashable type: dict'.)"""
    dep = _base().combine(DeploymentLayer(
        applications=ApplicationSetLayer(applications={
            Append(ApplicationLayer(name="apps", host_machine=Explicit("central")))})))
    issues = validate(dep)
    # no error (must not block the bootstrap) ...
    assert [i for i in issues if i.severity == "error"] == []
    # ... but a warning naming the empty composition.
    warns = [i for i in issues if i.severity == "warning"]
    assert any("bundles no processes" in i.message and "apps" in i.message
               for i in warns)


def test_invariant_populated_application_is_clean():
    """An application bundling a real process raises no empty-composition
    warning."""
    dep = _base().combine(DeploymentLayer(
        applications=ApplicationSetLayer(applications={
            Append(ApplicationLayer(name="apps", host_machine=Explicit("central"),
                                    processes={"counter"}))})))
    issues = validate(dep)
    assert [i for i in issues if i.severity == "error"] == []
    assert not any("bundles no processes" in i.message for i in issues)


def test_verify_returns_warnings_does_not_raise_on_empty_app():
    """DeploymentLayer.verify() is the explicit gate a rig.py calls: it returns
    warnings (empty composition) without raising, so a bare-supervisor bootstrap
    still serializes."""
    dep = _base().combine(DeploymentLayer(
        applications=ApplicationSetLayer(applications={
            Append(ApplicationLayer(name="apps", host_machine=Explicit("central")))})))
    warns = dep.verify()
    assert any("bundles no processes" in w.message for w in warns)


def test_verify_strict_raises_on_warning():
    dep = _base().combine(DeploymentLayer(
        applications=ApplicationSetLayer(applications={
            Append(ApplicationLayer(name="apps", host_machine=Explicit("central")))})))
    with pytest.raises(VerifyError, match="bundles no processes"):
        dep.verify(strict=True)


def test_verify_raises_on_error():
    bad = _base().combine(DeploymentLayer(
        execution=ExecutionLayer(processes={Append(_proc("ghost", "nope"))})))
    with pytest.raises(VerifyError, match="not declared in machines axis"):
        bad.verify()
