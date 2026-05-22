"""Flatten composition-of-composition references.

A :class:`CompositionDecl` body can hold three kinds of elements:

- :class:`PrototypeDecl`         — concrete node instance
- :class:`CompositionRefDecl`    — instance of another composition
- :class:`ConnectDecl`           — port-to-port wiring at parent scope

For codegen + validation, callers want a *flat* view: every prototype +
every connect that's reachable from a composition, including those that
came from nested ``composition Foo bar`` references. This module owns
that flattening rule so every walker treats nested compositions the
same way.

Rules (final design choices — don't override per-caller):

- Inner ``PrototypeDecl`` names appear **verbatim** in the parent's
  flat list. No instance-name prefixing.
- Inner ``ConnectDecl``s are spliced in alongside the parent's connects
  (they reference inner prototype names, which now resolve at parent
  scope).
- ``CompositionRefDecl`` instance names (the ``bar`` in
  ``composition Foo bar``) are not exposed in the flat view — they're
  presentational only.

Cycle detection lives here too — :func:`flatten_composition` raises if
``Outer`` transitively references itself. Validators call this same
function before codegen runs, so a cycle is reported once with a clear
chain.
"""

from __future__ import annotations

from typing import Any


def _cls(obj: Any) -> str:
    return type(obj).__name__


def flatten_composition(
    composition: Any,
) -> tuple[list[Any], list[Any]]:
    """Return ``(prototypes, connects)`` for a flattened composition.

    Walks any nested :class:`CompositionRefDecl` elements recursively.
    Raises :class:`ValueError` on cycles.
    """
    prototypes: list[Any] = []
    connects: list[Any] = []

    def _walk(comp: Any, stack: list[str]) -> None:
        name = getattr(comp, "name", "<?>")
        if name in stack:
            chain = " -> ".join(stack + [name])
            raise ValueError(f"composition cycle: {chain}")
        stack = stack + [name]

        for el in comp.elements:
            kind = _cls(el)
            if kind == "PrototypeDecl":
                prototypes.append(el)
            elif kind == "CompositionRefDecl":
                target = el.type
                if target is None:
                    raise ValueError(
                        f"composition {name!r}: unresolved reference {el.name!r} "
                        f"(missing `import <pkg>.*`?)"
                    )
                _walk(target, stack)
            elif kind == "ConnectDecl":
                connects.append(el)
            # Unknown element types are tolerated for forward-compat.

    _walk(composition, [])
    return prototypes, connects
