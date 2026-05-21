"""Layer-merge transforms for the AUTOSAR manifest model.

Three needs justify a small piece of machinery here:

1. *Membership* — add or remove a SwComponent / ServiceInstance from
   the set the base layer ships.
2. *Per-field overrides* — an upper layer wants to tweak one field of
   one element without restating the whole thing. Example: the
   platform's ``log`` ServiceInstance defaults to ``binding=TIPC``;
   Macan overrides it to ``binding=INET`` plus an endpoint so logs
   stream to a remote machine.
3. *Defaulting* — fields left unset in a lower layer get filled by an
   upper layer.

Mosaic's ``Layer.squash`` machinery did the same job but came with a
parallel ``Undefined``/``Default``/``Identifiable`` type system and a
frozen-counterpart resolver. We don't need that ceremony — plain
``@dataclass`` plus a class-level ``_identity_field`` is enough.

The two primitives are :class:`Add` and :class:`Remove` (membership) and
:class:`Override` (field-level merge keyed by identity). They compose
via :func:`apply_layer` and the convenience wrapper
:func:`merge_layers`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Iterable, TypeVar

T = TypeVar("T")


class Identifiable:
    """Mixin: an element with a stable identity key for layer merging.

    Subclasses set the class-level ``_identity_field`` to the name of
    the dataclass field that uniquely identifies the element within its
    containing list. Default: ``"name"``.
    """

    _identity_field: str = "name"

    @property
    def _identity(self) -> Any:
        return getattr(self, self._identity_field)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Add:
    """Add an element to the target list.

    If an element with the same identity already exists, it's *overridden*
    rather than duplicated (the new element wins field-wise; unset fields
    fall back to the old element via :class:`Override` semantics).
    """

    value: Any


@dataclass(frozen=True)
class Remove:
    """Remove the element with this identity from the target list."""

    identity: Any


@dataclass(frozen=True)
class Override:
    """Patch field(s) of an existing element identified by ``identity``.

    Only the fields explicitly set in ``patch`` overwrite the base;
    everything else stays. If no element with ``identity`` exists, the
    override is silently dropped — use :class:`Add` for that case.
    """

    identity: Any
    patch: dict[str, Any]


Op = Add | Remove | Override


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _identity_of(elem: Any) -> Any:
    """Return the identity of ``elem`` if it's :class:`Identifiable`, else
    fall back to a ``name`` attribute, else ``id(elem)``."""
    if isinstance(elem, Identifiable):
        return elem._identity
    return getattr(elem, "name", id(elem))


def _merge_fields(base: T, patch: dict[str, Any]) -> T:
    """Return a copy of ``base`` with ``patch`` keys overwritten."""
    if not dataclasses.is_dataclass(base):
        raise TypeError(f"cannot merge fields onto non-dataclass {type(base).__name__}")
    return dataclasses.replace(base, **patch)


def _merge_element(base: T, new: T) -> T:
    """Field-wise merge of two same-identity dataclass elements.

    Fields explicitly set on ``new`` (i.e. not equal to their default)
    win; everything else stays from ``base``. For lists we union by
    identity, recursing into elements when they share an identity.

    This is the bulk of :class:`Add` for an existing element and of
    :class:`Override` when the caller passed a whole replacement dataclass.
    """
    if not dataclasses.is_dataclass(base) or not dataclasses.is_dataclass(new):
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

        # If the new value equals the field's default, treat it as "not set"
        # and keep the base value. Otherwise the new wins.
        default = _field_default(f)
        if new_v == default and base_v != default:
            continue
        patch[f.name] = new_v

    return dataclasses.replace(base, **patch)


def _field_default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return dataclasses.MISSING


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
    """Apply a sequence of Add / Remove / Override ops to a list."""
    out: list = list(target_list)
    index = {_identity_of(e): i for i, e in enumerate(out)}

    for op in ops:
        if isinstance(op, Add):
            ident = _identity_of(op.value)
            if ident in index:
                out[index[ident]] = _merge_element(out[index[ident]], op.value)
            else:
                index[ident] = len(out)
                out.append(op.value)
        elif isinstance(op, Remove):
            i = index.pop(op.identity, None)
            if i is not None:
                out.pop(i)
                # rebuild the index — the cost is fine for layer-merge sizes
                index = {_identity_of(e): j for j, e in enumerate(out)}
        elif isinstance(op, Override):
            i = index.get(op.identity)
            if i is None:
                continue
            out[i] = _merge_fields(out[i], op.patch)
        else:  # pragma: no cover
            raise TypeError(f"unknown op {type(op).__name__}")

    return out
