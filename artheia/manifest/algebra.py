"""Layer composition algebra — Semigroup / Monoid over configuration records.

This is the canonical composition engine for manifests. A *layer* is a
dataclass whose fields are wrapped configuration values; layers compose by a
right-biased monoidal append (``combine``), and a finished layer is then
**materialized** (``simplify``) into a clean, frozen *target* dataclass that the
serializers consume.

The model has three pieces:

1. **ConfigField[Ctx, A]** — the per-field lifecycle, a sum type:
   ``Undefined`` (the monoid identity — absent, inherit from below),
   ``Default(a)`` (a fallback that resolves to ``a`` if nothing above sets it),
   ``Explicit(a)`` (a concrete value), ``Defer(ctx -> a)`` (late-bound, resolved
   at simplify time). ``combine`` is the field-level append with a fixed
   precedence; ``Functor`` ``map`` for free.

2. **Layer / Identifiable / MonoidSet** — a dataclass mixin (``combine`` walks
   the fields, recursing into nested layers and folding set edits), identity-
   keyed set membership, and the ``Append``/``Remove`` set edits authored as
   ``cast(Set, {Append(x), Remove(y)})`` blocks in the DSL.

3. **simplify + validate** — ``simplify(layer)`` materializes a layer to its
   target (raising on a surviving ``Undefined``/``Defer``). ``validate(layer)``
   is the NEW capability: a structural + logic consistency pass over the
   UNMATERIALIZED layer tree that *collects* issues (required-but-undefined,
   duplicate identities, unresolved Defer, plus per-type domain invariants via
   an optional ``_invariants`` hook) so a generator can reject a bad spec with
   precise field paths BEFORE serializing it to JSON.

Phase separation is deliberate: a ``Layer`` is the chaotic, mid-merge,
*inspectable* artifact; a target is the pristine product. A dataclass MAY serve
as both (the default ``_resolver`` returns ``type(self)``), but the distinction
is what makes pre-serialization validation possible.

Naming: this module is the clean rewrite of the former ``applicative.py``. The
historical spellings are preserved as aliases for back-compat —
``Empty``=``Undefined``, ``Pure``=``Default``, ``mappend``/``squash``=``combine``,
``Insert``/``Delete``=``Append``/``Remove``.
"""

from __future__ import annotations

import dataclasses
import importlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import copy
from dataclasses import asdict, is_dataclass
from typing import (
    Any,
    Generic,
    Iterable,
    Optional,
    Protocol,
    TypeVar,
    Union,
    no_type_check,
    runtime_checkable,
)

A = TypeVar("A")
T = TypeVar("T")
ID = TypeVar("ID")
Ctx = TypeVar("Ctx")


class LayerMergeError(Exception):
    """Raised when two layers cannot be combined, or a marker survives to
    materialization (an unresolved ``Undefined``/``Defer`` at ``simplify``)."""


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

class IsDataclass(Protocol):
    __dataclass_fields__: dict[str, Any]


@runtime_checkable
class IsProtobuf(Protocol):
    @abstractmethod
    def SerializeToString(self) -> bytes:
        raise NotImplementedError()  # pragma: no cover

    @abstractmethod
    def IsInitialized(self) -> bool:
        raise NotImplementedError()  # pragma: no cover


# ---------------------------------------------------------------------------
# 1. ConfigField — the per-field lifecycle sum type.
# ---------------------------------------------------------------------------
#
# combine() precedence (right-biased; `over` is the upper layer):
#   here  <> Defer(g)     = Defer(g)        # late-bound upper wins
#   here  <> Explicit(x)  = Explicit(x)     # concrete upper wins
#   here  <> Undefined    = here            # absent upper -> inherit below
#   Explicit(x) <> Default = Explicit(x)    # a concrete base beats a fallback
#   here  <> Default(d)   = Default(d)      # else the fallback carries
#
# Undefined is the monoid identity (mempty): X <> Undefined == X and (because a
# bare Undefined on the left contributes nothing) Undefined <> X == X.


class ConfigField(Generic[Ctx, A]):
    """Base of the field lifecycle. Not instantiated directly — use one of
    ``Undefined`` / ``Default`` / ``Explicit`` / ``Defer``."""

    def combine(self, over: "ConfigField[Ctx, A]") -> "ConfigField[Ctx, A]":
        """Right-biased monoidal append of two field values."""
        if isinstance(over, Defer):
            return over
        if isinstance(over, Explicit):
            return over
        if isinstance(over, Default):
            # A concrete base beats an upper fallback; otherwise the fallback.
            return self if isinstance(self, Explicit) else over
        # `over` is a bare Undefined -> inherit the lower value.
        return self

    def map(self, f: Callable[[A], A]) -> "ConfigField[Ctx, A]":
        """Functor map — applies ``f`` to the carried value, if any."""
        if isinstance(self, Explicit):
            return Explicit(f(self.value))
        if isinstance(self, Default):
            return Default(f(self.default))
        if isinstance(self, Defer):
            g = self._f
            return Defer(lambda ctx: f(g(ctx)))
        return self

    def simplify(self, context_path: str) -> A:
        """Materialize this field to its concrete value, or raise."""
        if isinstance(self, Explicit):
            return self.value
        if isinstance(self, Default):
            return self.default
        if isinstance(self, Defer):
            raise LayerMergeError(
                f"unresolved Defer at {context_path} (resolve it before simplify)"
            )
        raise LayerMergeError(f"unresolved Undefined at {context_path}")


class Undefined(ConfigField[Ctx, A]):
    """The monoid identity: absent value, inherit from the layer below.

    All ``Undefined`` instances are interchangeable regardless of type param,
    so they compare equal (and ``Default`` deliberately is NOT ``==`` an
    ``Undefined`` — it carries a value)."""

    def __eq__(self, o: object) -> bool:
        # A plain Undefined equals any other plain Undefined, but not a Default
        # (a Default is a subclass that carries a value — distinct identity).
        return isinstance(o, Undefined) and not isinstance(o, Default)

    def __hash__(self) -> int:
        return hash(Undefined)

    def __str__(self) -> str:
        return "Undefined()"

    __repr__ = __str__


class Default(Undefined[Ctx, A]):
    """A fallback: resolves to ``default`` if no layer above sets a concrete
    value. Survives ``combine`` while everything above is ``Undefined``."""

    def __init__(self, default: A) -> None:
        super().__init__()
        self._default = default

    @property
    def default(self) -> A:
        return self._default

    def __eq__(self, o: object) -> bool:
        return isinstance(o, Default) and bool(self._default == o._default)

    def __hash__(self) -> int:
        return hash(("Default", self._default))

    def __str__(self) -> str:
        return f"Default({self._default!s})"

    def __repr__(self) -> str:
        return f"Default({self._default!r})"


class Explicit(ConfigField[Ctx, A]):
    """A concrete, author-supplied value."""

    def __init__(self, value: A) -> None:
        self.value = value

    def __eq__(self, o: object) -> bool:
        return isinstance(o, Explicit) and bool(self.value == o.value)

    def __hash__(self) -> int:
        return hash(("Explicit", self.value))

    def __str__(self) -> str:
        return f"Explicit({self.value!s})"

    def __repr__(self) -> str:
        return f"Explicit({self.value!r})"


class Defer(ConfigField[Ctx, A]):
    """A late-bound value: ``f(ctx)`` is called at an explicit resolve step.

    ``simplify`` raises if a ``Defer`` survives — a caller-side pass must
    resolve deferred fields against a context first."""

    def __init__(self, f: Callable[[Ctx], A]) -> None:
        self._f = f

    def __call__(self, ctx: Ctx) -> A:
        return self._f(ctx)

    def __str__(self) -> str:
        return "Defer(<callable>)"

    __repr__ = __str__


class EmptySet(Undefined[Ctx, A]):
    """The monoid identity for a SET field: inherit-on-combine like
    :class:`Undefined`, but materialize to ``frozenset()`` instead of raising.

    A set field's default should be ``EmptySet`` (not a bare ``Undefined``,
    which would be flagged required, and not an empty ``set()``, which combine
    treats as an explicit "replace with nothing"). It means "no contribution at
    this layer; an empty set if nothing below contributes either"."""

    def simplify(self, context_path: str) -> Any:
        return frozenset()

    def __str__(self) -> str:
        return "EmptySet()"

    __repr__ = __str__


def empty_set() -> "EmptySet":
    """Default factory for a set-typed Layer field (see :class:`EmptySet`)."""
    return EmptySet()


# --- historical value-marker aliases (applicative.py spelling) -------------
Empty = Undefined   # the monoid identity
Pure = Default      # a carried fallback


def alt_field(here: Any, over: Any) -> Any:
    """Right-biased scalar combine for two ConfigFields (``Alternative``'s
    ``<|>``): the upper value wins if present, else the lower, else a surviving
    ``Default`` carries to simplify. Kept as a free function for the legacy
    surface; new code uses ``ConfigField.combine``."""
    if isinstance(here, ConfigField) and isinstance(over, ConfigField):
        return here.combine(over)
    # Bare (non-ConfigField) values: treat a non-Undefined upper as the winner.
    if not isinstance(over, Undefined):
        return over
    if not isinstance(here, Undefined):
        return here
    return over if isinstance(over, Default) else here


# ---------------------------------------------------------------------------
# 2. Set edits — Append / Remove (aka Insert / Delete).
# ---------------------------------------------------------------------------

class SetTransform(Generic[T]):
    """An element-level edit carried inside a set field. A set field holds
    EITHER bare members OR a set of edits, never a mix."""

    @abstractmethod
    def apply(self, current: set[T]) -> set[T]:
        raise NotImplementedError()  # pragma: no cover


class Append(SetTransform, Generic[T]):
    """Add to — or merge-by-identity into — the target set.

    When ``value`` is :class:`Identifiable` and a member with the same
    ``_set_identify`` already exists, that member is ``combine``-d with
    ``value``; otherwise ``value`` joins as a new member."""

    def __init__(self, value: T) -> None:
        self.value = value

    def apply(self, current: set[T]) -> set[T]:
        if not isinstance(self.value, Identifiable):
            return current | {self.value}

        result: set[T] = set()
        absorbed = False
        for member in current:
            if (
                isinstance(member, Identifiable)
                and member._set_identify == self.value._set_identify
            ):
                member = member.combine(self.value)
                absorbed = True
            result.add(member)
        if not absorbed:
            result.add(self.value)
        return result


class Remove(SetTransform, Generic[T]):
    """Drop the member matching ``value``. Identifiable → match by
    ``_set_identify`` (only the identity field need be set); else by ``==``."""

    def __init__(self, value: T) -> None:
        self.value = value

    def apply(self, current: set[T]) -> set[T]:
        if isinstance(self.value, Identifiable):
            key = self.value._set_identify
            return {
                m for m in current
                if not (isinstance(m, Identifiable) and m._set_identify == key)
            }
        return {m for m in current if m != self.value}


# --- historical set-edit aliases -------------------------------------------
SetEdit = SetTransform
Insert = Append
Delete = Remove

SetTransformTypes = Union[Append["Identifiable"], Remove["Identifiable"]]
SimpleSetTransformTypes = Union[Append[T], Remove[T]]


def _is_edit_set(members: Iterable[Any]) -> bool:
    """Classify a set as edits-or-members, rejecting a mix."""
    flags = [isinstance(m, SetTransform) for m in members]
    if any(flags) and not all(flags):
        raise LayerMergeError(
            "a set field must hold either plain members or edits, not both"
        )
    return bool(flags) and all(flags)


def _ordered_edits(edits: Iterable[Any]) -> list[Any]:
    """Edits in a deterministic apply order: all Removes first, then Appends.

    An edit set is unordered (it's a Python set), so ``{Remove(X), Append(X')}``
    must not depend on iteration order. Applying Removes before Appends gives the
    intended "replace": Remove(X) drops the old member, then Append(X') adds the
    new one — never the reverse (which would drop what was just added)."""
    edits = list(edits)
    removes = [e for e in edits if isinstance(e, Remove)]
    others = [e for e in edits if not isinstance(e, Remove)]
    return removes + others


def fold_transforms(base: Any) -> set[Any]:
    """Reduce a base set to plain members: empty if absent, fold edits over an
    empty start if it carries edits, else pass through."""
    if isinstance(base, Undefined):
        return set()
    if not _is_edit_set(base):
        return base
    acc: set[Any] = set()
    for edit in _ordered_edits(base):
        acc = edit.apply(acc)
    return acc


def ap_transforms(base: Any, over: Any) -> Any:
    """Apply ``over`` onto ``base`` — the two-arg core of the set combine."""
    base_set = fold_transforms(base)
    if isinstance(over, Undefined):
        return base_set
    if not _is_edit_set(over):
        return over
    acc: set[Any] = copy(base_set)
    for edit in _ordered_edits(over):
        acc = edit.apply(acc)
    return acc


@no_type_check
def mappend_set(base: Any, over: Any) -> Any:
    """Set-level combine: like :func:`ap_transforms`, but when both sides are
    plain (no edits), union them."""
    base_set = fold_transforms(base)
    if isinstance(over, Undefined):
        return base_set
    if not _is_edit_set(base_set) and not _is_edit_set(over):
        return base_set | over
    return ap_transforms(base, over)


# ---------------------------------------------------------------------------
# MonoidSet — a Semigroup wrapper over a set, for explicit algebraic use.
# ---------------------------------------------------------------------------

class MonoidSet(Generic[ID, A]):
    """An identity-keyed set that composes as a Semigroup.

    Lets the DSL write native ``{...}`` blocks and ``|`` unions on a set field
    and have them merge by identity (recursing via ``combine`` when elements
    are themselves layers). The plain-set + ``Append``/``Remove`` path on a
    Layer field is the common case; ``MonoidSet`` is the explicit-object form
    for callers that want a first-class combinable set."""

    def __init__(self, elements: set[A], get_id: Callable[[A], ID]):
        self.elements = set(elements)
        self.get_id = get_id

    def combine(self, other: "MonoidSet[ID, A]") -> "MonoidSet[ID, A]":
        merged = set(self.elements)
        for incoming in other.elements:
            incoming_id = self.get_id(incoming)
            existing = next(
                (x for x in merged if self.get_id(x) == incoming_id), None
            )
            if (
                existing is not None
                and isinstance(existing, Layer)
                and isinstance(incoming, type(existing))
            ):
                merged.discard(existing)
                merged.add(existing.combine(incoming))
            else:
                merged = {x for x in merged if self.get_id(x) != incoming_id}
                merged.add(incoming)
        return MonoidSet(merged, self.get_id)

    def __or__(self, other: "set[A] | MonoidSet[ID, A]") -> "MonoidSet[ID, A]":
        incoming = other.elements if isinstance(other, MonoidSet) else other
        return self.combine(MonoidSet(incoming, self.get_id))

    def simplify(self, context_path: str) -> frozenset[Any]:
        out = []
        for item in self.elements:
            out.append(
                item.simplify(context_path) if hasattr(item, "simplify") else item
            )
        return frozenset(out)


# ---------------------------------------------------------------------------
# 3. Layer — the composable dataclass mixin.
# ---------------------------------------------------------------------------

class Layer(IsDataclass, Generic[T]):
    """A dataclass that participates in layer composition.

    ``combine`` (aliases: ``mappend``, ``squash``) walks the dataclass fields
    in declaration order, combining each from ``over``: nested ``Layer`` fields
    recurse, ``set`` fields fold :class:`SetTransform` members, identity-bearing
    ``list`` fields union by identity, and scalar (``ConfigField``) fields take
    the right-biased ``combine``. Override :attr:`_resolver` when the simplified
    *target* type differs from ``Self``."""

    # -- combine ------------------------------------------------------------

    def _combine_field(self, here: Any, above: Any) -> Any:
        if isinstance(here, Layer):
            return here.combine(above)
        if isinstance(here, MonoidSet):
            return here.combine(above)
        # Set-field algebra. EmptySet is the set identity: it contributes
        # nothing, so combining it with anything yields the other side (a real
        # set, an edit-set, or another EmptySet). A real/edit set on `here`
        # folds `above` onto it; a real/edit set on `above` over an EmptySet
        # `here` materializes `above`.
        here_is_set = isinstance(here, set) or isinstance(here, EmptySet)
        above_is_set = isinstance(above, set) or isinstance(above, EmptySet)
        if here_is_set or above_is_set:
            if isinstance(here, EmptySet) and isinstance(above, EmptySet):
                return here
            if isinstance(here, EmptySet):
                # Apply `above` onto an empty base: fold any Append/Remove edits
                # to plain members (folding a plain-member set is a no-op). An
                # edit-set Appended onto an EMPTY base (e.g. a deploy delta over
                # an apps manifest with no applications yet) would otherwise leak
                # raw Append objects through simplify.
                return fold_transforms(above)
            if isinstance(above, EmptySet):
                return here  # keep base set; nothing added above
            if isinstance(here, set):
                # Both real sets: union-by-identity when both are plain members
                # (two base layers contributing to the same axis merge, they do
                # NOT replace); apply edits when `above` carries Append/Remove.
                return mappend_set(here, above)
            return fold_transforms(above)
        if (
            isinstance(here, list)
            and isinstance(above, list)
            and all(isinstance(x, Identifiable) for x in (*here, *above))
        ):
            return _merge_lists(here, above)
        return alt_field(here, above)

    def combine(self, over: Any) -> Any:
        """Right-biased monoidal append: ``over``'s present fields win."""
        if isinstance(over, Undefined):
            return self
        if isinstance(over, Defer):
            return over
        if isinstance(over, list) and all(
            isinstance(item, type(self)) for item in over
        ):
            acc: Any = self
            for item in over:
                acc = acc.combine(item)
            return acc
        if not isinstance(over, type(self)):
            return over

        combined = {
            name: self._combine_field(getattr(self, name), getattr(over, name))
            for name in self.__dataclass_fields__.keys()
        }
        return self.__class__(**combined)

    # Historical spellings.
    mappend = combine
    squash = combine

    # -- simplify -----------------------------------------------------------

    def _check_set_members(self, value: Iterable[Any], context: str) -> list[Any]:
        members = list(value)
        for i, element in enumerate(members):
            if isinstance(element, Layer) and not isinstance(element, Identifiable):
                raise TypeError(
                    f"set {context}({i}) holds a {type(element).__name__} Layer "
                    f"that does not inherit from Identifiable"
                )
        return members

    def _simplify_set(self, value: Any, context: str) -> frozenset[Any]:
        return frozenset(
            self._simplify_element(element, f"{context}({i})")
            for i, element in enumerate(self._check_set_members(value, context))
        )

    def _simplify_element(self, value: Any, context: str) -> Any:
        if isinstance(value, ConfigField):
            return value.simplify(context)
        if isinstance(value, Layer):
            return value.simplify(context)
        if isinstance(value, MonoidSet):
            return value.simplify(context)
        if isinstance(value, (set, frozenset)):
            return self._simplify_set(value, context)
        return value

    def simplify(self, context: Optional[str] = None) -> T:
        """Materialize this layer to its frozen target, or raise on a
        surviving ``Undefined``/``Defer``."""
        context = context or "root"
        resolved = {
            name: self._simplify_element(
                getattr(self, name), f"{context}.{name}"
            )
            for name in asdict(self).keys()  # type: ignore[call-overload]
        }
        try:
            return self._resolver(**resolved)
        except TypeError as e:
            raise LayerMergeError(
                f"cannot resolve {context} via {self._resolver}: {e}"
            )

    @property
    def _resolver(self) -> type[T]:
        """The concrete target type :meth:`simplify` materializes to.
        Defaults to ``type(self)`` (one dataclass serving as both layer and
        target); override when the target shape differs."""
        return type(self)

    # -- validate (structural + logic checks on the UNMATERIALIZED layer) ---

    def _invariants(self, context: str) -> "list[Issue]":
        """Per-type domain invariants. Override to assert cross-field / cross-
        reference rules (e.g. a process maps to a declared machine). Returns
        a list of :class:`Issue`; the default has none. Runs over the
        unmaterialized layer, so it sees ``ConfigField`` markers + edits."""
        return []


# ---------------------------------------------------------------------------
# Identifiable — set-membership identity.
# ---------------------------------------------------------------------------

class Identifiable(Layer, Generic[T]):
    """A Layer whose members live in a ``set`` keyed by identity.

    ``_identity_field`` (default ``"name"``) names the field that identifies an
    element; ``_set_identify`` (an int) is the set-membership key. Override
    ``_set_identify`` when identity spans several fields."""

    _identity_field: str = "name"

    @property
    def _identity(self) -> Any:
        return getattr(self, self._identity_field)

    @property
    def _set_identify(self) -> int:
        return hash(self._identity)


def _identifiable_hash(self) -> int:
    return self._set_identify


def _identifiable_eq(self, other: object) -> bool:
    return (
        isinstance(other, Identifiable)
        and type(self) is type(other)
        and self._set_identify == other._set_identify
    )


def identifiable_dataclass(cls=None, **dataclass_kwargs):
    """Drop-in replacement for ``@dataclass`` on :class:`Identifiable`
    subclasses: applies ``@dataclass`` then reinstates identity-based
    ``__hash__``/``__eq__`` (clobbered by the default ``eq=True``) so instances
    can live in ``set[X]`` and flow through ``Append``/``Remove``."""

    def wrap(klass):
        decorated = dataclasses.dataclass(klass, **dataclass_kwargs)
        decorated.__hash__ = _identifiable_hash
        decorated.__eq__ = _identifiable_eq
        return decorated

    return wrap if cls is None else wrap(cls)


# ---------------------------------------------------------------------------
# Identity-keyed list merge — shared element helpers (legacy list-Rig path).
# ---------------------------------------------------------------------------

def _identity_of(elem: Any) -> Any:
    if isinstance(elem, Identifiable):
        return elem._identity
    if hasattr(elem, "name"):
        return elem.name
    return id(elem)


def _field_default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


def _merge_fields(base: T, patch: dict[str, Any]) -> T:
    if not is_dataclass(base):
        raise TypeError(f"cannot patch fields onto non-dataclass {type(base).__name__}")
    return dataclasses.replace(base, **patch)  # type: ignore[type-var]


def _merge_element(base: T, new: T) -> T:
    """Field-wise merge of two same-identity dataclasses: a field set on
    ``new`` (i.e. differing from its default) overrides ``base``; nested
    Layers combine."""
    if not (is_dataclass(base) and is_dataclass(new)):
        return new
    patch: dict[str, Any] = {}
    for f in dataclasses.fields(base):  # type: ignore[arg-type]
        nv = getattr(new, f.name)
        bv = getattr(base, f.name)
        if isinstance(bv, Layer) and isinstance(nv, Layer):
            patch[f.name] = bv.combine(nv)
        elif nv != _field_default(f):
            patch[f.name] = nv
    return _merge_fields(base, patch)


def _merge_lists(base: list, new: list) -> list:
    """Union two identity-bearing lists: same-identity members in ``new``
    supplant (merge into) their twins in ``base``; surplus members append."""
    out = list(base)
    index = {_identity_of(e): i for i, e in enumerate(out)}
    for e in new:
        key = _identity_of(e)
        if key in index:
            out[index[key]] = _merge_element(out[index[key]], e)
        else:
            index[key] = len(out)
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# validate() — consistency checks on the UNMATERIALIZED layer tree.
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Issue:
    """A single consistency finding from :func:`validate`."""
    path: str           # dotted field path, e.g. "root.compute_elements(0).name"
    message: str
    severity: str = "error"   # "error" | "warning"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.path}: {self.message}"


def validate(layer: Any, context: str = "root") -> list[Issue]:
    """Walk an UNMATERIALIZED layer tree and collect consistency issues
    WITHOUT materializing it. Catches, with precise field paths:

    - a required field still ``Undefined`` (no default, nothing set);
    - an unresolved ``Defer`` (would fail serialization);
    - duplicate identities within a set field;
    - per-type domain invariants (a layer's ``_invariants`` hook).

    Returns issues (does not raise) so a generator can report them all and
    decide whether to proceed. ``Default`` is fine (it resolves); only a bare
    ``Undefined`` on a field with no default is flagged."""
    issues: list[Issue] = []
    _validate_layer(layer, context, issues)
    return issues


def _validate_layer(layer: Any, context: str, issues: list[Issue]) -> None:
    if not isinstance(layer, Layer):
        return
    for name in layer.__dataclass_fields__.keys():
        value = getattr(layer, name)
        path = f"{context}.{name}"
        _validate_value(value, path, issues)
    # Domain invariants for this layer type, run on the unmaterialized form.
    try:
        issues.extend(layer._invariants(context))
    except Exception as e:  # an invariant hook must never crash validation
        issues.append(Issue(context, f"invariant check raised {e!r}"))


def _validate_value(value: Any, path: str, issues: list[Issue]) -> None:
    if isinstance(value, Defer):
        issues.append(Issue(path, "unresolved Defer (resolve before serialize)"))
    elif isinstance(value, EmptySet):
        pass  # the set identity — resolves to frozenset() — fine
    elif isinstance(value, Default):
        pass  # resolves to its default — fine
    elif isinstance(value, Undefined):
        issues.append(Issue(path, "required value is Undefined"))
    elif isinstance(value, Explicit):
        _validate_value(value.value, path, issues)
    elif isinstance(value, Layer):
        _validate_layer(value, path, issues)
    elif isinstance(value, MonoidSet):
        _validate_set(value.elements, path, issues)
    elif isinstance(value, (set, frozenset)):
        _validate_set(value, path, issues)


def _validate_set(members: Iterable[Any], path: str, issues: list[Issue]) -> None:
    members = list(members)
    # Duplicate-identity detection among Identifiable members (edits excluded).
    seen: dict[int, int] = {}
    for i, m in enumerate(members):
        if isinstance(m, SetTransform):
            continue
        if isinstance(m, Identifiable):
            key = m._set_identify
            if key in seen:
                issues.append(
                    Issue(
                        f"{path}({i})",
                        f"duplicate identity {m._identity!r} "
                        f"(also at index {seen[key]})",
                    )
                )
            else:
                seen[key] = i
        _validate_value(m, f"{path}({i})", issues)


# ---------------------------------------------------------------------------
# Misc helpers carried over.
# ---------------------------------------------------------------------------

def hash_with_protos(proto: IsDataclass, proto_fields: list[str]) -> int:
    """Hash that survives protobuf re-serialisation by hashing the bytes of the
    named protobuf-typed fields rather than the message objects."""
    fields = asdict(proto)  # type: ignore[call-overload]
    hash_values = [getattr(proto, f) for f in fields if f not in proto_fields]
    proto_values: list[Optional[bytes]] = []
    for f in proto_fields:
        v = getattr(proto, f)
        proto_values.append(None if v is None else v.SerializeToString())
    return hash((*hash_values, *proto_values))


def import_config(module_name: str, symbol: str) -> object:
    """Late-binding config import for ``Defer``-style indirection."""
    return getattr(importlib.import_module(module_name), symbol)


def type_tree_str(t: IsDataclass, context: str = "root", indent: int = 0) -> str:
    """Pretty-print a layer/dataclass tree for debugging."""
    pad = "  " * indent
    out = f"{pad}{context}: {type(t).__name__}\n"
    for name in asdict(t).keys():  # type: ignore[call-overload]
        out += _handle_field(getattr(t, name), name, indent + 1)
    return out


def _handle_field(value: Any, name: str, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, (Undefined, Defer)):
        return f"{pad}{name}: {value}\n"
    if isinstance(value, Layer) or is_dataclass(value):
        return type_tree_str(value, name, indent)
    if isinstance(value, (set, frozenset)):
        out = f"{pad}{name}: set[{len(value)}]\n"
        for i, el in enumerate(value):
            if isinstance(el, Layer) or is_dataclass(el):
                out += type_tree_str(el, f"{name}({i})", indent + 1)
            else:
                out += _handle_field(el, f"[{i}]", indent + 1)
        return out
    return f"{pad}{name}: {type(value).__name__}\n"
