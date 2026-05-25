"""Node prototype inheritance: resolve `node X extends Y` post-parse.

Grammar declares `(extends=[NodeDecl|FQN])?` on NodeDecl. textX
populates `node.base` with the resolved base node when the clause
is present, leaves it None otherwise.

This module flattens that link. After resolve, the derived node
LOOKS like a self-contained node — generators consume it without
any awareness of the extends relationship. The flattening is
in-place mutation on the textX model object: the derived node
gains the base's ports / params / statem / config / kick_off /
requires_timers attributes IF and only IF the derived didn't
declare its own.

Rules:

  - tipc is ALWAYS the derived's own (the whole point of extends).
  - All other fields: derived's own value wins if present, else
    base's value is copied verbatim.
  - "Present" means textX-truthy: a non-empty list for ports/params,
    a non-None object for statem/config, the boolean flag value as
    stated.
  - Chains are resolved bottom-up: if A extends B extends C, B is
    resolved against C first, then A against the (already-resolved)
    B.
  - Cycles surface as a clear error pointing at the offending
    declaration.

The transformation is idempotent: calling resolve_inheritance
twice on the same model is a no-op the second time (derived nodes
that already absorbed their base look indistinguishable from
self-contained nodes).
"""
from __future__ import annotations

from typing import Any


def _is_present(value: Any) -> bool:
    """Did the derived NodeDecl explicitly declare this field?

    textX's container conventions:
      - list-valued fields (ports, params) default to []
      - object-valued fields (statem, config) default to None
      - bool-valued fields (kick_off, requires_timers) default to False

    "Present" means the derived's value diverges from the empty
    default — meaning the user typed it.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if hasattr(value, "__len__"):
        return len(value) > 0
    # Other scalar objects (a MessageDecl ref, a StateMBody) — non-None
    # is enough.
    return True


def _iter_nodes(model) -> list:
    return [el for el in model.elements
            if el.__class__.__name__ == "NodeDecl"]


def resolve_inheritance(model) -> None:
    """Walk the model's NodeDecls and splice base fields into any
    derived node that didn't override them. In-place mutation.

    Raises :class:`ValueError` on cycles. textX has already resolved
    the cross-reference (so `node.base` is either None or a
    NodeDecl object) — we just need to flatten the chain.

    After the flatten, applies field-level defaults (currently
    only ``reporting``: default = "true" when omitted) so generators
    don't have to special-case the None/missing form.
    """
    nodes = _iter_nodes(model)

    # Topological order: bottom-up. A derived node can only be
    # resolved after its base. Visit base before derived.
    resolved: set[int] = set()

    def _resolve(node, chain: list[int]) -> None:
        if id(node) in resolved:
            return
        if id(node) in chain:
            names = [_name_at(nid, nodes) for nid in chain] + [node.name]
            raise ValueError(
                f"node extends cycle: {' -> '.join(names)}"
            )
        base = getattr(node, "base", None)
        if base is None:
            resolved.add(id(node))
            return
        # Resolve the base first (so transitive inheritance works).
        _resolve(base, chain + [id(node)])
        _copy_inherited_fields(src=base, dst=node)
        resolved.add(id(node))

    for n in nodes:
        _resolve(n, chain=[])

    # Default-fill ANY field that the grammar makes optional but
    # generators should be able to treat as always-set. Run AFTER
    # inheritance so a base's explicit value still wins.
    for n in nodes:
        _apply_node_defaults(n)


def _apply_node_defaults(node) -> None:
    """Fill optional NodeDecl fields with their conventional defaults.

    Current defaults:
      ``reporting`` — defaults to ``"true"``. AUTOSAR Reporting
                       process is the common case; explicit
                       ``reporting=false`` is the opt-out.
    """
    if not node.reporting:
        node.reporting = "true"


def _name_at(node_id: int, nodes: list) -> str:
    for n in nodes:
        if id(n) == node_id:
            return n.name
    return "<unknown>"


def _copy_inherited_fields(*, src, dst) -> None:
    """Copy src→dst for every NodeDecl field that the dst didn't
    declare. tipc is NEVER copied (derived must state its own).
    """
    # Optional bool flags. Inherited only if dst didn't say either.
    # Since they default to False, "user typed it" === "value is True".
    if not _is_present(dst.kick_off) and _is_present(src.kick_off):
        dst.kick_off = src.kick_off
    if not _is_present(dst.requires_timers) and _is_present(src.requires_timers):
        dst.requires_timers = src.requires_timers

    # reporting — non-default BOOL field. textX stores the matched
    # BoolLit verbatim ("true" / "false") and leaves it as an empty
    # string when the production didn't match. Inherit ONLY when
    # dst's value is empty (the production didn't trigger). A
    # derived node that wrote `reporting=false` explicitly keeps
    # its own value even if the base said true.
    if not dst.reporting and src.reporting:
        dst.reporting = src.reporting

    # Object refs.
    if not _is_present(dst.config) and _is_present(src.config):
        dst.config = src.config
    if not _is_present(dst.statem) and _is_present(src.statem):
        dst.statem = src.statem

    # AUTOSAR log-context tag (per-node, optional). Inherited only
    # when dst's value is empty — a derived node that wrote its own
    # `tag = "..."` keeps it.
    if not dst.tag and src.tag:
        dst.tag = src.tag

    # Lists — copy wholesale (no element-level merging).
    if not _is_present(dst.ports) and _is_present(src.ports):
        dst.ports = list(src.ports)
    if not _is_present(dst.params) and _is_present(src.params):
        dst.params = list(src.params)
