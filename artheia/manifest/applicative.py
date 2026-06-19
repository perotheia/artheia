"""Layer composition as a (poorly-typed) applicative functor.

Every dataclass that mixes in :class:`Layer` is, in effect, a record of
values wrapped in a small effect: a value may be *present*, *absent*
(:class:`Empty`), *pending a fallback* (:class:`Pure`), or *deferred*
(:class:`Defer`). ``base.mappend(over)`` is the binary combine of that
algebra — a right-biased monoidal append lifted field-by-field over the
record, recursing into nested layers and folding set-level
:class:`Insert` / :class:`Delete` edits.

Three tiers of API live here:

1. **Layer + mappend** — base mixin for any dataclass that takes part
   in composition. ``base.mappend(over)`` yields a fresh instance in
   which ``over``'s present fields win over ``base``'s, recursively.
   Nested ``Layer`` fields combine through; ``mempty``-marked fields
   inherit from below.

2. **SetEdit** (``Insert`` / ``Delete``) — element-level edits carried
   inside a set field. ``Insert(X)`` adds (or merges-by-identity) into
   the running set; ``Delete(X)`` drops by identity. A set field holds
   either bare members OR a set of edits, never a mix.

3. **Value markers** — ``Empty`` (absent; inherit from below),
   ``Pure(x)`` (resolve to ``x`` if nothing below sets it), ``Defer(f)``
   (call ``f(ctx)`` at :meth:`Layer.simplify` time for late-bound
   values).

A compatibility shim section at the end keeps the older flat-list
``Add`` / ``Override`` / ``Op`` / ``apply_ops`` surface alive for
callers (e.g. ``manifest/layer.py``) that have not yet moved to the
structured edit DSL. That section is slated for deletion once nothing
imports it.
"""

from __future__ import annotations

import dataclasses
import importlib
from abc import abstractmethod
from collections.abc import Callable
from copy import copy
from dataclasses import asdict, is_dataclass, replace
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

T = TypeVar("T")


class LayerMergeError(Exception):
    """Raised when two layers cannot be combined or a marker survives
    into :meth:`Layer.simplify`."""


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
# Layer — the carrier of the applicative combine.
# ---------------------------------------------------------------------------

class Layer(IsDataclass, Generic[T]):
    """A dataclass that participates in layer composition.

    Subclasses are ordinary ``@dataclass``es. Override
    :attr:`_resolver` when the simplified type differs from ``Self``.

    :meth:`mappend` walks the dataclass fields and combines from
    ``over`` in declaration order. Set fields fold :class:`SetEdit`
    members; nested ``Layer`` fields recurse; scalar fields take
    ``over``'s value unless it is :class:`Empty`.
    """

    def _combine_field(self, here: Any, above: Any) -> Any:
        """Combine one field's pair of values. Branches on the *kind*
        of the lower value first, then the upper — there is exactly one
        rule per kind, so this stays a flat lookup rather than a nested
        cascade."""
        if isinstance(here, Layer):
            return here.mappend(above)
        if isinstance(here, set):
            return ap_transforms(here, above)
        if isinstance(above, set):
            return fold_transforms(above)
        if (
            isinstance(here, list)
            and isinstance(above, list)
            and all(isinstance(x, Identifiable) for x in (*here, *above))
        ):
            # Two identity-bearing lists union by identity: a same-key
            # member in `above` supplants its twin in `here`, and any
            # surplus members of `above` are appended. Lifts the old
            # list-union behaviour into the structured DSL so a
            # ``list[SwComponent]`` populated by two layers is not
            # wholesale-replaced.
            return _merge_lists(here, above)
        return alt_field(here, above)

    def mappend(self, over: Any) -> Any:
        # Marker short-circuits, ordered cheapest-first.
        if isinstance(over, Empty):
            return self
        if isinstance(over, Defer):
            return over
        # A list of same-typed layers folds left: combine each in turn.
        if isinstance(over, list) and all(
            isinstance(item, type(self)) for item in over
        ):
            acc: Any = self
            for item in over:
                acc = acc.mappend(item)
            return acc
        if not isinstance(over, type(self)):
            return over

        combined = {
            name: self._combine_field(getattr(self, name), getattr(over, name))
            for name in self.__dataclass_fields__.keys()
        }
        return self.__class__(**combined)

    def _simplify_set(self, value: set[T] | frozenset[T], context: str) -> frozenset[T]:
        resolved = {
            self._simplify_element(element, f"{context}({i})")
            for i, element in enumerate(self._check_set_members(value, context))
        }
        return frozenset(resolved)

    def _check_set_members(self, value: Iterable[Any], context: str) -> list[Any]:
        members = list(value)
        for i, element in enumerate(members):
            if isinstance(element, Layer) and not isinstance(element, Identifiable):
                raise TypeError(
                    f"set {context}({i}) holds a {type(element).__name__} Layer "
                    f"that does not inherit from Identifiable"
                )
        return members

    def _simplify_element(self, value: Any, context: str) -> Any:
        if isinstance(value, Pure):
            return value.default
        if isinstance(value, (Empty, Defer)):
            raise LayerMergeError(
                f"unresolved {type(value).__name__} at {context}"
            )
        if isinstance(value, Layer):
            return value.simplify(context)
        if isinstance(value, (set, frozenset)):
            return self._simplify_set(value, context)
        return value

    def simplify(self, context: Optional[str] = None) -> T:
        context = context or "root"

        resolved_field = {
            field_name: self._simplify_element(
                getattr(self, field_name), f"{context}.{field_name}"
            )
            for field_name in asdict(self).keys()  # type: ignore[call-overload]
        }

        try:
            return self._resolver(**resolved_field)
        except TypeError as e:
            raise LayerMergeError(
                f"cannot resolve {context} via {self._resolver}: {e}"
            )

    @property
    def _resolver(self) -> type[T]:
        """The concrete type :meth:`simplify` materializes to.

        Defaults to ``type(self)`` — handy when one dataclass serves
        both as a composable Layer (may carry Empty/Insert/Delete) and
        as the resolved spec. Override when the resolved shape differs.
        """
        return type(self)


# ---------------------------------------------------------------------------
# Identifiable — set-membership identity.
# ---------------------------------------------------------------------------

class Identifiable(Layer, Generic[T]):
    """Mixin: a Layer whose members live in a ``set`` keyed by identity.

    Subclasses pick ONE of two identity mechanisms:

    - ``_identity_field`` (str, defaults to ``"name"``) — name of the
      dataclass field that uniquely identifies the element. The cheap,
      declarative path used by every manifest dataclass (`SwComponent`,
      `ServiceInstance`, …).

    - ``_set_identify`` (int property) — explicit override for when the
      identity is a tuple, computed, or otherwise not a plain field
      lookup. The default hashes the ``_identity_field`` value, so plain
      subclasses need no override.

    Two mechanisms exist because two callers need different things:

    1. The legacy list-based ``apply_ops`` (near the compat shims) uses
       ``_identity_field`` through the ``_identity`` property — it
       compares by equality, not only by hash collision.
    2. The set-based ``Insert`` / ``Delete`` uses ``_set_identify`` (an
       int) for fast set-membership.
    """

    _identity_field: str = "name"

    @property
    def _identity(self) -> Any:
        """Value of the identity field — used by list-based merging."""
        return getattr(self, self._identity_field)

    @property
    def _set_identify(self) -> int:
        """Integer identity key for set-based ``Insert``/``Delete``.

        Defaults to the hash of the identity-field value. Override when
        that is wrong (e.g. identity spans several fields).
        """
        return hash(self._identity)

    # Hashability note: ``@dataclass`` (default ``eq=True``) sets
    # ``__hash__ = None`` on subclasses, so instances are unhashable.
    # :func:`identifiable_dataclass` reinstates ``__hash__`` based on
    # :attr:`_set_identify` AFTER ``@dataclass`` runs — use it in place
    # of bare ``@dataclass`` on every ``Identifiable`` subclass that
    # must live in a ``set[X]`` (i.e. all of them).


def identifiable_dataclass(cls=None, **dataclass_kwargs):
    """Drop-in replacement for ``@dataclass`` on ``Identifiable``
    subclasses.

    Applies ``@dataclass(**kwargs)`` exactly as the stdlib decorator
    does, then reinstates identity-based ``__hash__`` and ``__eq__``
    (clobbered by the default ``eq=True``) so instances can live in
    ``set[X]`` and flow through :class:`Insert` / :class:`Delete`.

    Usage:

    .. code-block:: python

        from artheia.manifest.applicative import (
            Identifiable, identifiable_dataclass,
        )

        @identifiable_dataclass
        class SwComponent(Identifiable):
            name: str
            bazel_target: str = ""
    """

    def wrap(klass):
        decorated = dataclasses.dataclass(klass, **dataclass_kwargs)
        # Reinstate hash/eq from the Identifiable base. ``__hash__`` is
        # the load-bearing fix; ``__eq__`` is restored too so two
        # Identifiable instances with the same identity compare equal
        # (what set membership expects).
        decorated.__hash__ = _identifiable_hash
        decorated.__eq__ = _identifiable_eq
        return decorated

    # Support both @identifiable_dataclass and @identifiable_dataclass(...)
    if cls is None:
        return wrap
    return wrap(cls)


def _identifiable_hash(self) -> int:
    """Module-level helper so ``identifiable_dataclass`` installs a
    stable reference (rather than closing over the Identifiable
    class)."""
    return self._set_identify


def _identifiable_eq(self, other: object) -> bool:
    """Module-level helper — see :func:`_identifiable_hash`. Identity
    never crosses types (a ``Machine`` named "foo" is not a ``Process``
    named "foo")."""
    if not isinstance(other, Identifiable):
        return NotImplemented
    if type(self) is not type(other):
        return False
    return self._set_identify == other._set_identify


def type_tree_str(t: IsDataclass, context: str = "root", indent: int = 0) -> str:
    fields = asdict(t)  # type: ignore[call-overload]
    indent_str = "  " * indent
    output_str = f"{indent_str}{context}: {type(t).__name__}\n"

    for field_name in fields.keys():
        field_value = getattr(t, field_name)
        output_str += handle_field(field_value, field_name, indent + 1)

    return output_str


def handle_field(field_value: Any, field_name: str, indent: int = 0) -> str:
    indent_str = "  " * indent
    output_str = ""

    if isinstance(field_value, Empty):
        output_str += f"{indent_str}{field_name}: {str(field_value)}\n"
    elif isinstance(field_value, Defer):
        output_str += f"{indent_str}{field_name}: Defer\n"
    elif isinstance(field_value, Layer) or is_dataclass(field_value):
        output_str += type_tree_str(field_value, field_name, indent)
    elif isinstance(field_value, (set, frozenset)):
        output_str += f"{indent_str}{field_name}: set[{len(field_value)}]\n"
        for i, element in enumerate(field_value):
            if isinstance(element, Layer) or is_dataclass(element):
                output_str += type_tree_str(element, f"{field_name}({i})", indent + 1)
            else:
                output_str += handle_field(field_value, f"[{i}]", indent + 1)
    else:
        output_str += f"{indent_str}{field_name}: {type(field_value).__name__}\n"
    return output_str


# ---------------------------------------------------------------------------
# Value markers — Empty / Pure / Defer.
# ---------------------------------------------------------------------------

class NoDefault:
    pass


class Empty(Generic[T]):
    """Marker: this field is absent; inherit from the layer below.

    The monoid identity of the field algebra. All ``Empty`` instances
    are interchangeable regardless of generic parameter, so they
    compare equal.
    """

    def __eq__(self, o: object) -> bool:
        return isinstance(o, Empty)

    def __hash__(self) -> int:
        return hash(Empty)

    def __str__(self) -> str:
        return "Empty()"

    def __repr__(self) -> str:
        return "Empty()"


class Pure(Empty[T]):
    """Marker: resolve to ``default`` if nothing below sets a value.

    Stronger than :class:`Empty` — it survives the combine while no
    layer above supplies a concrete value, then resolves to ``default``
    at simplify time.
    """

    def __init__(self, default: T) -> None:
        super().__init__()
        self._default = default

    def __str__(self) -> str:
        return f"Pure({str(self._default)})"

    def __repr__(self) -> str:
        return f"Pure({repr(self._default)})"

    def __eq__(self, o: object) -> bool:
        if isinstance(o, Pure):
            return bool(self._default == o._default)
        return False

    def __hash__(self) -> int:
        return hash(("Pure", self._default))

    @property
    def default(self) -> T:
        return self._default


CONTEXT_T = TypeVar("CONTEXT_T")


class Defer(Generic[CONTEXT_T, T]):
    """Marker: resolve lazily by calling ``f(ctx)``.

    For fields whose value is unknown when a layer is authored (e.g. an
    endpoint that depends on machine binding done in a higher layer).
    :meth:`Layer.simplify` raises if a ``Defer`` survives — an explicit
    caller-side step must resolve it first.
    """

    def __init__(self, f: Callable[[CONTEXT_T], T]):
        self._f = f

    def __call__(self, ctx: CONTEXT_T) -> T:
        return self._f(ctx)


# ---------------------------------------------------------------------------
# Set edits — Insert / Delete.
# ---------------------------------------------------------------------------

class SetEdit(Generic[T]):
    @abstractmethod
    def apply(self, current: set[T]) -> set[T]:
        raise NotImplementedError()


class Insert(SetEdit, Generic[T]):
    """Add to (or merge-by-identity into) the target set.

    When ``value`` is :class:`Identifiable` and the set already holds a
    member with the same ``_set_identify``, that member is
    :meth:`Layer.mappend`-combined with ``value``. Otherwise ``value``
    joins as a new member.
    """

    def __init__(self, value: T):
        self.value = value

    def apply(self, current: set[T]) -> set[T]:
        if not isinstance(self.value, Identifiable):
            return current | {self.value}

        result: set[T] = set()
        absorbed = False
        for member in current:
            assert isinstance(member, Identifiable)
            if member._set_identify == self.value._set_identify:
                assert isinstance(member, Layer)
                member = member.mappend(self.value)
                absorbed = True
            result.add(member)
        if not absorbed:
            result.add(self.value)
        return result


class Delete(SetEdit, Generic[T]):
    """Drop the member matching ``value`` from the target set.

    When ``value`` is :class:`Identifiable`, matching is by
    ``_set_identify`` (only the identity field need be filled in);
    otherwise by ``==``.
    """

    def __init__(self, value: T):
        self.value = value

    def apply(self, current: set[T]) -> set[T]:
        if isinstance(self.value, Identifiable):
            key = self.value._set_identify
            return {
                m for m in current
                if not (isinstance(m, Identifiable) and m._set_identify == key)
            }
        return {m for m in current if m != self.value}


SetTransformTypes = Union[Insert[Identifiable], Delete[Identifiable]]
SimpleSetTransformTypes = Union[Insert[T], Delete[T]]


def _is_edit_set(members: Iterable[Any]) -> bool:
    """Classify a set as edits-or-members, rejecting a mix.

    Returns True when every member is a :class:`SetEdit`, False when
    none is. A mixed set is a programming error and raises.
    """
    flags = [isinstance(m, SetEdit) for m in members]
    if any(flags) and not all(flags):
        raise LayerMergeError(
            "a set field must hold either plain members or edits, not both"
        )
    return bool(flags) and all(flags)


def fold_transforms(
    base: set[T] | set[SetTransformTypes] | Empty[set[T]],
) -> set[T]:
    """Reduce a base set to plain members: empty if absent, fold edits
    over an empty start if it carries edits, else pass through."""
    if isinstance(base, Empty):
        return set()
    if not _is_edit_set(base):
        return base  # type: ignore

    acc: set[T] = set()
    for edit in base:
        assert isinstance(edit, SetEdit)
        acc = edit.apply(acc)
    return acc


def ap_transforms(
    base: set[T] | set[SetTransformTypes] | Empty[set[T]],
    over: set[T] | set[SetTransformTypes] | Empty[set[T]],
) -> set[T] | Empty[set[T]]:
    """Apply ``over`` onto ``base`` — the two-arg core of the combine."""
    base_set = fold_transforms(base)

    if isinstance(over, Empty):
        return base_set
    if not _is_edit_set(over):
        return over  # type: ignore

    acc: set[T] = copy(base_set)
    for edit in over:
        assert isinstance(edit, SetEdit)
        acc = edit.apply(acc)
    return acc


def alt_field(here: T | Empty, above: T | Empty) -> T | Empty:
    """Right-biased scalar combine (``Alternative``'s ``<|>``): the
    upper value wins if present, else the lower, else a surviving
    ``Pure`` carries to simplify."""
    if not isinstance(above, Empty):
        return above
    if not isinstance(here, Empty):
        return here
    if isinstance(above, Pure):
        return above
    return here


def hash_with_protos(proto: IsDataclass, proto_fields: list[str]) -> int:
    """Hash that survives protobuf re-serialisation by hashing the
    bytes of the named protobuf-typed fields rather than the message
    objects themselves."""
    fields = asdict(proto)  # type: ignore[call-overload]

    hash_values = [
        getattr(proto, field) for field in fields if field not in proto_fields
    ]

    proto_values: list[Optional[str]] = []
    for field in proto_fields:
        value = getattr(proto, field)
        proto_values.append(None if value is None else value.SerializeToString())

    return hash((*hash_values, *proto_values))


@no_type_check
def mappend_set(
    base: set[T] | set[SetTransformTypes] | Empty[set[T]],
    over: set[T] | set[SetTransformTypes] | Empty[set[T]],
) -> set[T] | Empty[set[T]]:
    """Set-level combine: like :func:`ap_transforms`, but when both
    sides are plain (no edits), union them. Used by the user-facing
    top-level combine."""
    base_set = fold_transforms(base)

    if isinstance(over, Empty):
        return base_set

    if not _is_edit_set(base_set) and not _is_edit_set(over):
        return base_set | over

    return ap_transforms(base, over)


def import_config(module_name: str, symbol: str) -> object:
    """Late-binding config import helper. Used by ``Defer``-style
    indirection where a layer names a vehicle-specific module."""
    config_module = importlib.import_module(module_name)
    return getattr(config_module, symbol)


# ---------------------------------------------------------------------------
# Compatibility shims for the legacy list-based API.
#
# The previous module exposed Add/Remove/Override/Op + apply_ops as the
# merge primitives, with manifest/layer.py carrying parallel
# `add_<X>` / `remove_<X>` / `override_<X>` lists per element kind. That
# surface stays functional during the migration so existing callers
# (services/manifest/fc.py, demo/manifest/rig.py, artheia/manifest/
# layer.py) keep working while individual call sites move to the
# structured Insert/Delete/mappend DSL.
#
# Removal plan: once no `Override`, `apply_ops`, or `_identity_field`-
# only callers remain, delete this section. `Insert` is the new name for
# `Add` (and `Append`); `Delete` for `Remove` — they are exact aliases.
# ---------------------------------------------------------------------------

# Back-compat aliases. New code uses Insert/Delete.
Append = Insert
Add = Insert
Remove = Delete


@dataclasses.dataclass(frozen=True)
class Override:
    """Patch field(s) of an existing element keyed by ``identity``.

    Legacy primitive — prefer a field-level override in the structured
    DSL (a sibling ``Insert(SwComponent(name=..., field=newvalue))``
    inside the parent set, which merges by identity).
    """

    identity: Any
    patch: dict[str, Any]


Op = Union[Insert, Delete, Override]


def _identity_of(elem: Any) -> Any:
    """Identity of ``elem`` if :class:`Identifiable`, else its ``name``
    attribute, else ``id(elem)``."""
    if isinstance(elem, Identifiable):
        return elem._identity
    return getattr(elem, "name", id(elem))


def _merge_fields(base: T, patch: dict[str, Any]) -> T:
    """Copy ``base`` with ``patch`` keys overwritten."""
    if not is_dataclass(base):
        raise TypeError(f"cannot patch fields onto non-dataclass {type(base).__name__}")
    return replace(base, **patch)


def _field_default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return dataclasses.MISSING


def _merge_element(base: T, new: T) -> T:
    """Field-wise merge of two same-identity dataclass elements.

    Legacy semantics: fields explicitly set on ``new`` (not equal to
    their default) win; the rest stay from ``base``. Lists union by
    identity, recursing into elements that share an identity.
    """
    if not is_dataclass(base) or not is_dataclass(new):
        return new
    if type(base) is not type(new):
        return new

    patch: dict[str, Any] = {}
    for f in dataclasses.fields(new):
        base_v = getattr(base, f.name)
        new_v = getattr(new, f.name)

        if isinstance(new_v, list) and isinstance(base_v, list):
            patch[f.name] = _merge_lists(base_v, new_v)
            continue

        default = _field_default(f)
        if new_v == default and base_v != default:
            continue
        patch[f.name] = new_v

    return replace(base, **patch)


def _merge_lists(base: list, new: list) -> list:
    """Union two lists by identity; same-identity elements are merged."""
    out: list = list(base)
    index = {_identity_of(e): i for i, e in enumerate(out)}
    for elem in new:
        ident = _identity_of(elem)
        if ident in index:
            out[index[ident]] = _merge_element(out[index[ident]], elem)
        else:
            index[ident] = len(out)
            out.append(elem)
    return out


def apply_ops(target_list: list, ops: Iterable[Op]) -> list:
    """Apply a sequence of Insert/Delete/Override ops to a list.

    Legacy entry point used by ``manifest/layer.py``. The Delete path
    here is identity-keyed via ``_identity_of`` (matches by the `name`
    field for Identifiable elements, else by `id()`), whereas the new
    ``Delete.apply`` works on sets keyed by ``_set_identify``. Both
    routes coexist during the migration.
    """
    out: list = list(target_list)
    index = {_identity_of(e): i for i, e in enumerate(out)}

    for op in ops:
        if isinstance(op, Insert):
            ident = _identity_of(op.value)
            if ident in index:
                out[index[ident]] = _merge_element(out[index[ident]], op.value)
            else:
                index[ident] = len(out)
                out.append(op.value)
        elif isinstance(op, Delete):
            # Legacy Delete takes either an identity or an Identifiable.
            ident = op.value._identity if isinstance(op.value, Identifiable) else op.value
            i = index.pop(ident, None)
            if i is not None:
                out.pop(i)
                index = {_identity_of(e): j for j, e in enumerate(out)}
        elif isinstance(op, Override):
            i = index.get(op.identity)
            if i is None:
                continue
            out[i] = _merge_fields(out[i], op.patch)
        else:  # pragma: no cover
            raise TypeError(f"unknown op {type(op).__name__}")

    return out
