"""Layer-merge transforms for the AUTOSAR manifest model.

DSL recovery — ported from theia_runtime/artheia/artheia/manifest/transform.py
(originally Mosaic's tools/syscomp/engine/transform.py). The earlier
"simplified" version of this module dropped the high-leverage DSL
features in favour of parallel `Add`/`Remove`/`Override` lists on a flat
`Layer`. This file restores them.

Three layers of API in this file:

1. **Layer + squash** — base class for any dataclass that participates
   in layer composition. ``base.squash(other)`` returns a new instance
   where ``other``'s set fields are applied as transforms over
   ``base``'s, recursively. Nested ``Layer`` fields squash through.

2. **SetTransform** (``Append`` / ``Remove``) — inline annotations
   inside a set field. ``Append(X)`` adds (or merges-by-identity)
   into the base set; ``Remove(X)`` drops by identity. The same set
   field accepts either bare elements OR a set of transforms.

3. **Value markers** — ``Undefined`` (unset; inherit from base layer),
   ``Default(x)`` (use ``x`` if base also unset), ``Defer(f)`` (call
   ``f(ctx)`` during ``simplify`` to resolve late-bound values).

Plus compat shims at the bottom for the older flat-list API (`Add`,
`Override`, `Op`, `apply_ops`) so callers like ``manifest/layer.py``
keep working during the migration. Those shims should be removed once
all callers move to the structured DSL.
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
# Layer — anything that can be squashed.
# ---------------------------------------------------------------------------

class Layer(IsDataclass, Generic[T]):
    """A dataclass that participates in layer composition.

    Subclasses are ordinary ``@dataclass``es. Override
    :attr:`_resolver` if the simplified type differs from ``Self``.

    :meth:`squash` walks the dataclass fields and merges from ``other``
    in declaration order. Set fields apply :class:`SetTransform`
    elements; nested ``Layer`` fields recurse; scalar fields take
    ``other``'s value unless it's :class:`Undefined`.
    """

    def squash(self, other: Any) -> Any:
        if isinstance(other, Undefined):
            return self
        if isinstance(other, Defer):
            return other
        # Support for squashing lists. If other is a list of the same type as self, squash each element in order
        if isinstance(other, list) and all(
            isinstance(item, type(self)) for item in other
        ):
            if len(other) == 0:
                return self
            else:
                return self.squash(other[0]).squash(other[1:])
        if not isinstance(other, type(self)):
            return other

        squashed: dict[str, Any] = {}
        for name in self.__dataclass_fields__.keys():
            var = getattr(self, name)
            other_var = getattr(other, name)

            if isinstance(var, Layer):
                squashed[name] = var.squash(other_var)
            elif isinstance(var, set):
                squashed[name] = transform_set(var, other_var)
            elif isinstance(other_var, set):
                squashed[name] = transform_base(other_var)
            else:
                squashed[name] = merge_field(var, other_var)

        return self.__class__(**squashed)

    def _simplify_set(self, value: set[T] | frozenset[T], context: str) -> frozenset[T]:
        elements = set()

        for i, element in enumerate(value):
            if isinstance(element, Layer) and not isinstance(element, Identifiable):
                raise TypeError(
                    f"Layers in set {context}({i}) of type {type(element).__name__} must inherit from Identifiable"
                )
            elements.add(self._simplify_element(element, f"{context}({i})"))

        return frozenset(elements)

    def _simplify_element(self, value: Any, context: str) -> Any:
        if isinstance(value, Default):
            return value.default
        if isinstance(
            value,
            (
                Undefined,
                Defer,
            ),
        ):
            raise ValueError(f"{type(value).__name__} value in field {context}")
        if isinstance(value, Layer):
            return value.simplify(context)
        if isinstance(value, set) or isinstance(value, frozenset):
            return self._simplify_set(value, context)

        return value

    def simplify(self, context: Optional[str] = None) -> T:
        if context is None:
            context = "root"

        fields = asdict(self)  # type: ignore[call-overload]
        resolved_field = {}
        for field_name in fields.keys():
            value = getattr(self, field_name)

            field_context = f"{context}.{field_name}"
            resolved_field[field_name] = self._simplify_element(value, field_context)

        try:
            return self._resolver(**resolved_field)
        except TypeError as e:
            raise ValueError(
                f"Error resolving {context}: {e} when simplifying with {self._resolver}"
            )

    @property
    def _resolver(self) -> type[T]:
        """The concrete type that ``simplify()`` materializes to.

        Default: ``type(self)`` — useful when the same dataclass plays
        both Layer (composable, may contain Undefined/Append/Remove)
        and resolved-spec (concrete) roles. Override when the resolved
        type differs structurally.
        """
        return type(self)


# ---------------------------------------------------------------------------
# Identifiable — set-membership identity.
# ---------------------------------------------------------------------------

class Identifiable(Layer, Generic[T]):
    """Mixin: a Layer whose members live in a ``set`` keyed by identity.

    Subclasses pick ONE of two identity mechanisms:

    - ``_identity_field`` (str, defaults to ``"name"``) — name of the
      dataclass field that uniquely identifies the element. This is
      the cheap, declarative path used by every existing manifest
      dataclass (`SwComponent`, `ServiceInstance`, …).

    - ``_set_identify`` (int property) — explicit, override if the
      identity is a tuple, computed, or otherwise not a simple field
      lookup. ``Identifiable._set_identify`` defaults to a hash of the
      ``_identity_field`` value, so plain subclasses work without any
      override.

    The duplicate path exists because we have two callers:

    1. The legacy list-based ``apply_ops`` (in this file, near the
       compat shims) uses ``_identity_field`` via the ``_identity``
       property — preserves equality, not just hash collision.
    2. The new set-based ``Append`` / ``Remove`` uses ``_set_identify``
       (an int) for fast set-membership checks.
    """

    _identity_field: str = "name"

    @property
    def _identity(self) -> Any:
        """The value of the identity field — used by list-based merging."""
        return getattr(self, self._identity_field)

    @property
    def _set_identify(self) -> int:
        """An integer identity key for set-based ``Append``/``Remove``.

        Default: hash of the identity-field value. Override when that's
        not the right thing (e.g. identity is a tuple of multiple
        fields).
        """
        return hash(self._identity)

    # Note on hashability: ``@dataclass`` (with default ``eq=True``)
    # sets ``__hash__ = None`` on subclasses, making instances
    # unhashable. The :func:`identifiable_dataclass` decorator below
    # restores ``__hash__`` to :attr:`_set_identify`-based hashing
    # AFTER ``@dataclass`` runs — use it instead of bare ``@dataclass``
    # on every ``Identifiable`` subclass that needs to live in a
    # ``set[X]`` (i.e. all of them).


def identifiable_dataclass(cls=None, **dataclass_kwargs):
    """Drop-in replacement for ``@dataclass`` for ``Identifiable``
    subclasses.

    Applies ``@dataclass(**kwargs)`` exactly as the stdlib decorator
    would, then restores identity-based ``__hash__`` and ``__eq__``
    (clobbered by the default ``eq=True``) so the resulting instances
    can live in ``set[X]`` and flow through :class:`Append` /
    :class:`Remove`.

    Usage:

    .. code-block:: python

        from artheia.manifest.transform import (
            Identifiable, identifiable_dataclass,
        )

        @identifiable_dataclass
        class SwComponent(Identifiable):
            name: str
            bazel_target: str = ""
    """

    def wrap(klass):
        decorated = dataclasses.dataclass(klass, **dataclass_kwargs)
        # Restore hash/eq from the Identifiable base class. ``__hash__``
        # is the key fix; ``__eq__`` is restored too so two Identifiable
        # instances with the same identity compare equal (matches what
        # set membership expects).
        decorated.__hash__ = _identifiable_hash
        decorated.__eq__ = _identifiable_eq
        return decorated

    # Support both @identifiable_dataclass and @identifiable_dataclass(...)
    if cls is None:
        return wrap
    return wrap(cls)


def _identifiable_hash(self) -> int:
    """Module-level helper so ``identifiable_dataclass`` can install
    a stable reference (avoids capturing the Identifiable class in a
    closure)."""
    return self._set_identify


def _identifiable_eq(self, other: object) -> bool:
    """Module-level helper — see :func:`_identifiable_hash`. Cross-type
    identity is never equal (a ``Machine`` named "foo" is not the same
    as a ``Process`` named "foo")."""
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

    # Validate the field
    if isinstance(field_value, Undefined):
        output_str += f"{indent_str}{field_name}: {str(field_value)}\n"
    elif isinstance(field_value, Defer):
        output_str += f"{indent_str}{field_name}: Defer\n"
    elif isinstance(field_value, Layer) or is_dataclass(field_value):
        output_str += type_tree_str(field_value, field_name, indent)
    elif isinstance(field_value, set) or isinstance(field_value, frozenset):
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
# Value markers — Undefined / Default / Defer.
# ---------------------------------------------------------------------------

class NoDefault:
    pass


class Undefined(Generic[T]):
    """Marker: this field is unset; inherit from the base layer.

    All Undefined instances compare equal regardless of their generic
    parameter — they're interchangeable placeholders.
    """

    def __eq__(self, o: object) -> bool:
        if isinstance(o, Undefined):
            return True
        return False

    def __hash__(self) -> int:
        return hash(Undefined)

    def __str__(self) -> str:
        return "Undefined()"

    def __repr__(self) -> str:
        return "Undefined()"


class Default(Undefined[T]):
    """Marker: use ``default`` if the base layer is also unset.

    Stronger than ``Undefined`` — survives squash if no layer above
    sets a concrete value, then resolves to ``default`` at simplify.
    """

    def __init__(self, default: T) -> None:
        super().__init__()
        self._default = default

    def __str__(self) -> str:
        return f"Default({str(self._default)})"

    def __repr__(self) -> str:
        return f"Default({repr(self._default)})"

    def __eq__(self, o: object) -> bool:
        if isinstance(o, Default):
            return bool(self._default == o._default)
        return False

    def __hash__(self) -> int:
        return hash(("Default", self._default))

    @property
    def default(self) -> T:
        return self._default


CONTEXT_T = TypeVar("CONTEXT_T")


class Defer(Generic[CONTEXT_T, T]):
    """Marker: resolve this value lazily by calling ``f(ctx)``.

    Used for fields whose value isn't knowable at the point a layer
    is written (e.g. an endpoint that depends on machine-binding done
    in a higher layer). ``simplify()`` raises if a Defer survives —
    it must be resolved by an explicit caller-side step before then.
    """

    def __init__(self, f: Callable[[CONTEXT_T], T]):
        self._f = f

    def __call__(self, ctx: CONTEXT_T) -> T:
        return self._f(ctx)


# ---------------------------------------------------------------------------
# Set transforms — Append / Remove.
# ---------------------------------------------------------------------------

class SetTransform(Generic[T]):
    @abstractmethod
    def apply(self, current: set[T]) -> set[T]:
        raise NotImplementedError()


class Append(SetTransform, Generic[T]):
    """Add (or merge-by-identity into) the target set.

    If ``value`` is :class:`Identifiable` and the set already contains
    an element with the same ``_set_identify``, the existing element
    is :meth:`Layer.squash`-merged with ``value``. Otherwise ``value``
    is added as a new element.
    """

    def __init__(self, value: T):
        self.value = value

    def apply(self, current: set[T]) -> set[T]:
        # Update
        if isinstance(self.value, Identifiable):
            new_elements = []

            added = False
            for e in current:
                assert isinstance(e, Identifiable)
                if e._set_identify == self.value._set_identify:
                    assert isinstance(e, Layer)
                    added = True
                    e = e.squash(self.value)
                new_elements.append(e)
            if not added:
                new_elements.append(self.value)  # type: ignore

            return set(new_elements)

        # Add new element
        return set(list(current) + [self.value])


class Remove(SetTransform, Generic[T]):
    """Drop the element matching ``value`` from the target set.

    If ``value`` is :class:`Identifiable`, comparison is by
    ``_set_identify`` (only the identity field needs to be filled in);
    otherwise by ``==``.
    """

    def __init__(self, value: T):
        self.value = value

    def apply(self, current: set[T]) -> set[T]:
        if isinstance(self.value, Identifiable):
            new_elements = []
            for e in current:
                assert isinstance(e, Identifiable)
                if e._set_identify != self.value._set_identify:
                    new_elements.append(e)
            return set(new_elements)  # type: ignore

        return {c for c in current if c != self.value}


SetTransformTypes = Union[Append[Identifiable], Remove[Identifiable]]
SimpleSetTransformTypes = Union[Append[T], Remove[T]]


def transform_base(
    base: set[T] | set[SetTransformTypes] | Undefined[set[T]],
) -> set[T]:
    """Resolve a base set: empty if undefined, materialize transforms
    if present, else return as-is."""
    if isinstance(base, Undefined):
        return set()

    if not any(isinstance(v, SetTransform) for v in base):
        return base  # type: ignore

    # Don't allow mixed logic
    assert all(
        isinstance(v, SetTransform) for v in base
    ), "Cannot mix LayerTransform and non-LayerTransform"

    new_list: set[T] = set()
    for t in base:
        assert isinstance(t, SetTransform)
        new_list = t.apply(new_list)
    return new_list


def transform_set(
    base: set[T] | set[SetTransformTypes] | Undefined[set[T]],
    other: set[T] | set[SetTransformTypes] | Undefined[set[T]],
) -> set[T] | Undefined[set[T]]:
    """Apply ``other`` over ``base``. The two-arg core of squash."""
    base_set = transform_base(base)

    if isinstance(other, Undefined):
        return base_set

    # Other is a simple set
    if not any(isinstance(v, SetTransform) for v in other):
        return other  # type: ignore

    assert all(
        isinstance(v, SetTransform) for v in other
    ), "Cannot mix LayerTransform and non-LayerTransform"

    new_list: set[T] = copy(base_set)
    for t in other:
        assert isinstance(t, SetTransform)
        new_list = t.apply(new_list)

    return new_list


def merge_field(base: T | Undefined, layer: T | Undefined) -> T | Undefined:
    """Scalar field merge: layer wins if set, else base, else Default
    survives to ``simplify``."""
    if not isinstance(layer, Undefined):
        return layer

    if not isinstance(base, Undefined):
        return base

    if isinstance(layer, Default):
        return layer

    return base


def hash_with_protos(proto: IsDataclass, proto_fields: list[str]) -> int:
    """Hash that survives protobuf re-serialisation by hashing the bytes
    of named protobuf-typed fields rather than the message objects."""
    fields = asdict(proto)  # type: ignore[call-overload]

    hash_values = [
        getattr(proto, field) for field in fields if field not in proto_fields
    ]

    proto_values: list[Optional[str]] = []
    for field in proto_fields:
        value = getattr(proto, field)
        if value is None:
            proto_values.append(None)
        else:
            proto_values.append(value.SerializeToString())

    return hash(
        (
            *hash_values,
            *proto_values,
        )
    )


# TODO: Fix this at some point
@no_type_check
def set_squash(
    base: set[T] | set[SetTransformTypes] | Undefined[set[T]],
    other: set[T] | set[SetTransformTypes] | Undefined[set[T]],
) -> set[T] | Undefined[set[T]]:
    """Set-squash: like ``transform_set`` but if both sides are simple
    (no transforms), union them. Used by user-facing top-level squash."""
    base_set = transform_base(base)

    if isinstance(other, Undefined):
        return base_set

    # Both simple sets
    if not any(isinstance(v, SetTransform) for v in base_set) and not any(
        isinstance(v, SetTransform) for v in other
    ):
        return base_set | other

    return transform_set(base, other)


def import_config(module_name: str, symbol: str) -> object:
    """Late-binding config import helper. Used by `Defer`-style
    indirection where a layer references a vehicle-specific module
    by name."""
    config_module = importlib.import_module(module_name)
    return getattr(config_module, symbol)


# ---------------------------------------------------------------------------
# Compatibility shims for the legacy list-based API.
#
# The previous version of this module exposed Add/Remove/Override/Op +
# apply_ops as the layer-merge primitives, with manifest/layer.py
# carrying parallel `add_<X>` / `remove_<X>` / `override_<X>` lists
# per element kind. That API stays functional during the migration
# so existing callers (services/manifest/fc.py, demo/manifest/rig.py,
# artheia/manifest/layer.py) keep working while individual call sites
# move to the structured Append/Remove/squash DSL.
#
# Removal plan: when no `Override`, `apply_ops`, or `_identity_field`-
# only callers remain, delete this section. The `Append` name above
# is already the new replacement for `Add` — they are exact aliases.
# ---------------------------------------------------------------------------

# `Add` is the legacy name for `Append`. New code uses Append.
Add = Append


@dataclasses.dataclass(frozen=True)
class Override:
    """Patch field(s) of an existing element identified by ``identity``.

    Legacy primitive — prefer field-level overrides in the structured
    DSL (a sibling `Append(SwComponent(name=..., field=newvalue))`
    inside the parent set, which merges by identity).
    """

    identity: Any
    patch: dict[str, Any]


Op = Union[Append, Remove, Override]


def _identity_of(elem: Any) -> Any:
    """Return the identity of ``elem`` if it's :class:`Identifiable`,
    else fall back to a ``name`` attribute, else ``id(elem)``."""
    if isinstance(elem, Identifiable):
        return elem._identity
    return getattr(elem, "name", id(elem))


def _merge_fields(base: T, patch: dict[str, Any]) -> T:
    """Return a copy of ``base`` with ``patch`` keys overwritten."""
    if not is_dataclass(base):
        raise TypeError(f"cannot merge fields onto non-dataclass {type(base).__name__}")
    return replace(base, **patch)


def _field_default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return dataclasses.MISSING


def _merge_element(base: T, new: T) -> T:
    """Field-wise merge of two same-identity dataclass elements.

    Legacy semantics: fields explicitly set on ``new`` (i.e. not equal
    to their default) win; everything else stays from ``base``. Lists
    are unioned by identity, recursing into elements when they share
    an identity.
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
    """Union two lists by identity; same-identity elements get merged."""
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
    """Apply a sequence of Append/Remove/Override ops to a list.

    Legacy entry point used by ``manifest/layer.py``. The Remove path
    here is identity-keyed via ``_identity_of`` (i.e. matches by the
    `name` field if the element is Identifiable, else by `id()`),
    whereas the new ``Remove.apply`` operates on sets keyed by
    ``_set_identify``. Both routes coexist during the migration.
    """
    out: list = list(target_list)
    index = {_identity_of(e): i for i, e in enumerate(out)}

    for op in ops:
        if isinstance(op, Append):
            ident = _identity_of(op.value)
            if ident in index:
                out[index[ident]] = _merge_element(out[index[ident]], op.value)
            else:
                index[ident] = len(out)
                out.append(op.value)
        elif isinstance(op, Remove):
            # Legacy Remove takes either an identity or an Identifiable.
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
