"""Metamodel loading + semantic validation for Artheia."""
from .flatten import flatten_composition
from .loader import (
    load_metamodel,
    parse_bus_component_nodes_only,
    parse_file,
    parse_file_standalone,
    parse_string,
)


def unwrap_literal(lit):
    """The raw Python value behind a `ParamLiteral` default — the ONE place that
    knows the grammar's literal wrapping, so every consumer (generators,
    validators) unwraps identically.

    A param/field default is a `ParamLiteral` whose `.value` is one of:
      - `StrLit`  (a QUOTED string) → the content is `StrLit.s`  → return that str
      - a bare `true`/`false` token → `.value` is the str 'true'/'false' → return it
      - a NUMBER  → `.value` is the int/float text → return it
    Passing the ParamLiteral, the inner literal, or a bare value all work
    (defensive `getattr` chain), so callers can hand us `p.default` directly.
    Returns None for a missing default. Does NOT coerce type (bool/int) — that's
    the caller's job by the param's declared type; this only strips the grammar
    wrappers so a quoted "true" is never mistaken for a bool.
    """
    if lit is None:
        return None
    # ParamLiteral → its .value (the inner literal); a bare value has no .value.
    v = getattr(lit, "value", lit)
    # StrLit wraps a quoted string; the string is .s. (A bare bool/number token
    # has no .s — it's already the str/number.)
    s = getattr(v, "s", None)
    return s if s is not None else v


__all__ = [
    "flatten_composition",
    "load_metamodel",
    "parse_bus_component_nodes_only",
    "parse_file",
    "parse_file_standalone",
    "parse_string",
    "unwrap_literal",
]
