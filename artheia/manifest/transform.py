from __future__ import annotations

import importlib
from abc import abstractmethod
from collections.abc import Callable
from copy import copy
from dataclasses import asdict, is_dataclass
from typing import (
    Any,
    Generic,
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


class Layer(IsDataclass, Generic[T]):
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
        raise NotImplementedError()


class Identifiable(Layer, Generic[T]):
    @property
    def _set_identify(self) -> int:
        raise NotImplementedError()


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
                output_str += handle_field(element, f"[{i}]", indent + 1)
    else:
        output_str += f"{indent_str}{field_name}: {type(field_value).__name__}\n"
    return output_str


class NoDefault:
    pass


class Undefined(Generic[T]):
    def __eq__(self, o: object) -> bool:
        if isinstance(o, Undefined):
            return True
        return False

    def __str__(self) -> str:
        return "Undefined()"

    def __repr__(self) -> str:
        return "Undefined()"


class Default(Undefined[T]):
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

    @property
    def default(self) -> T:
        return self._default


class SetTransform(Generic[T]):
    @abstractmethod
    def apply(self, current: set[T]) -> set[T]:
        raise NotImplementedError()


class Append(SetTransform, Generic[T]):
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
    if not isinstance(layer, Undefined):
        return layer

    if not isinstance(base, Undefined):
        return base

    if isinstance(layer, Default):
        return layer

    return base


CONTEXT_T = TypeVar("CONTEXT_T")


class Defer(Generic[CONTEXT_T, T]):
    def __init__(self, f: Callable[[CONTEXT_T], T]):
        self._f = f

    def __call__(self, ctx: CONTEXT_T) -> T:
        return self._f(ctx)


def hash_with_protos(proto: IsDataclass, proto_fields: list[str]) -> int:
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
    # Import the module dynamically
    config_module = importlib.import_module(module_name)
    # Retrieve the specified symbols from the module
    return getattr(config_module, symbol)
