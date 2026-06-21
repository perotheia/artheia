"""StateM — the finite state machine block on a node.

A :class:`StateMSpec` describes the static transition table of a node's
``statem`` block. It is the parsed-AST→Python projection of the ``.art``
``statem { ... }`` block (grammar: ``StateMBody`` in ``artheia.tx``), lowered
by :func:`statem_from_ast` and consumed by ``gen-app`` — which is why this
lives in :mod:`artheia.generators` (a codegen concern), not the deployment
manifest.

Abstraction ladder::

    node         = thread          (one GenServer instance)
    node.statem  = FSM             (typed transitions on top of the thread)
    composition  = executable      (one process; prototypes + connects)

The C++ runtime side is ``platform/runtime/GenStateM.hh`` — a
``GenStateM<Derived, StateT, DataT>`` template layered on
``GenServer<Derived, Holder>``. Phase 4's codegen (``gen-cpp-stubs``)
emits ``<NodeName>StateMBase.hpp`` from this :class:`StateMSpec` so
the derived class only needs to fill in conditional logic +
``on_enter`` side effects.

Reasoning behind the design points lives in
``docs/tasks/PROGRESS/gen_statem/design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TransitionTarget:
    """Where a transition lands.

    Exactly one of :attr:`halt` or :attr:`state` is set.

    :attr halt:       ``True`` for the ``halt`` keyword — clean exit.
                      Maps to ``GenStateM::halt()`` in the runtime.
    :attr state:      Name of the target state.
    :attr timeout_ms: Optional state-timeout to arm on entry. Maps to
                      ``transition_to(NewState, ms)`` in the runtime.
    """

    halt: bool = False
    state: Optional[str] = None
    timeout_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if self.halt and self.state is not None:
            raise ValueError("TransitionTarget can be halt OR state, not both")
        if not self.halt and self.state is None:
            raise ValueError("TransitionTarget needs either halt or a state")
        if self.halt and self.timeout_ms is not None:
            raise ValueError("halt cannot carry a timeout")


@dataclass(frozen=True)
class TransitionRule:
    """One transition out of a state.

    Exactly one of :attr:`event_fqn` or :attr:`is_timeout` is populated:

    * ``event <Msg> → ...`` → :attr:`event_fqn` is the message FQN.
    * ``timeout → ...``      → :attr:`is_timeout` is ``True``.

    The runtime synthesises a ``StateTimeoutMsg<StateT>`` event for the
    timeout case; ``handle_event(state, StateTimeoutMsg<S>, data)``
    receives it. The codegen will emit dispatch that branches on
    ``StateTimeoutMsg`` for ``is_timeout`` rules.
    """

    target: TransitionTarget
    event_fqn: Optional[str] = None
    is_timeout: bool = False

    def __post_init__(self) -> None:
        if self.is_timeout and self.event_fqn is not None:
            raise ValueError("a rule is either event-triggered or timeout, "
                             "not both")
        if not self.is_timeout and self.event_fqn is None:
            raise ValueError("a non-timeout rule needs an event FQN")


@dataclass(frozen=True)
class StateBlock:
    """All transitions out of one state.

    :attr state: Name of the state this block describes (must be in
                 :attr:`StateMSpec.states`).
    :attr rules: Ordered tuple of transitions. Order matters for the
                 codegen — emitted as a series of conditional branches
                 in the generated ``handle_event`` overloads.
    """

    state: str
    rules: tuple[TransitionRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StateMSpec:
    """Full FSM spec on a node.

    :attr states:    All declared state names (in declaration order;
                     codegen uses this order for the enum).
    :attr initial:   Name of the initial state (must be in
                     :attr:`states`).
    :attr data_fqn:  Optional FQN of the ``data`` ``MessageDecl``.
                     Codegen lowers this to a POD type in the
                     generated base class. ``None`` means the FSM
                     carries no per-instance data (data type is
                     an empty struct).
    :attr blocks:    One :class:`StateBlock` per declared state. A
                     state may be absent from :attr:`blocks` to mean
                     "no transitions out of this state by event/timeout"
                     (the FSM stays put forever — useful for terminal
                     states reached only via ``halt``).

    Validate transition targets via :meth:`validate` before passing to
    codegen.
    """

    states: tuple[str, ...]
    initial: str
    data_fqn: Optional[str] = None
    blocks: tuple[StateBlock, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        """Static checks that don't need a parser.

        Raises :class:`ValueError` on:

        * empty :attr:`states`,
        * :attr:`initial` not in :attr:`states`,
        * a :attr:`StateBlock.state` not in :attr:`states`,
        * a :attr:`TransitionTarget.state` not in :attr:`states`,
        * duplicate state names.

        Doesn't check event types against the project's MessageDecls —
        that's the job of the loader+linker (textX does it for us when
        the grammar is in scope).
        """
        if not self.states:
            raise ValueError("statem has no states declared")
        if len(set(self.states)) != len(self.states):
            raise ValueError(
                f"duplicate state names in {self.states!r}")
        if self.initial not in self.states:
            raise ValueError(
                f"initial state {self.initial!r} not in states "
                f"{self.states!r}")
        state_set = set(self.states)
        for blk in self.blocks:
            if blk.state not in state_set:
                raise ValueError(
                    f"on-block references unknown state "
                    f"{blk.state!r}")
            for rule in blk.rules:
                tgt = rule.target
                if tgt.state is not None and tgt.state not in state_set:
                    raise ValueError(
                        f"transition target {tgt.state!r} from state "
                        f"{blk.state!r} is not a declared state")


# ---- AST → dataclass projection ---------------------------------------------

# Duration suffixes → multiplier in milliseconds.
_DURATION_SUFFIXES = {
    "ms": 1,
    "s":  1000,
    "m":  60_000,
    "h":  3_600_000,
}


def _parse_duration_to_ms(text: str) -> int:
    """Turn ``30s`` / ``500ms`` / ``2m`` into integer milliseconds.

    Raises :class:`ValueError` on a malformed token. Should never
    happen for parser output — the grammar's ``Duration`` regex
    already constrains the shape.
    """
    for suffix, mul in sorted(_DURATION_SUFFIXES.items(),
                               key=lambda x: -len(x[0])):
        if text.endswith(suffix):
            n = text[:-len(suffix)]
            return int(n) * mul
    raise ValueError(f"unrecognised duration {text!r}")


def _qualify_message(msg_decl) -> str:
    """Reconstruct the FQN of a MessageDecl reference (mirrors cluster.py)."""
    name = msg_decl.name
    parent = getattr(msg_decl, "parent", None)
    while parent is not None and type(parent).__name__ != "Model":
        parent = getattr(parent, "parent", None)
    if parent is None or not getattr(parent, "name", None):
        return name
    return f"{parent.name}.{name}"


def _target_from_ast(target_ast) -> TransitionTarget:
    """Lower a textX ``TransitionTarget`` AST node."""
    if getattr(target_ast, "halt", False):
        return TransitionTarget(halt=True)
    state_ref = target_ast.state
    # textX may resolve `state=[ID]` to either a string or a parent's
    # named element; we want the bare string name. Fall back to str().
    state_name = state_ref if isinstance(state_ref, str) else str(state_ref)
    timeout_ms: Optional[int] = None
    if getattr(target_ast, "timeout", None):
        timeout_ms = _parse_duration_to_ms(target_ast.timeout)
    return TransitionTarget(state=state_name, timeout_ms=timeout_ms)


def _rule_from_ast(rule_ast) -> TransitionRule:
    """Lower a textX ``TransitionRule`` AST node."""
    target = _target_from_ast(rule_ast.target)
    # Either `event <Msg> → ...` or `timeout → ...`. textX leaves the
    # unselected alternative attribute as None.
    event_ref = getattr(rule_ast, "event", None)
    if event_ref is None:
        return TransitionRule(target=target, is_timeout=True)
    return TransitionRule(target=target, event_fqn=_qualify_message(event_ref))


def statem_from_ast(node) -> Optional[StateMSpec]:
    """Build a :class:`StateMSpec` from a parsed textX ``NodeDecl``.

    Returns ``None`` when the node has no ``statem`` block.

    The returned spec is validated via :meth:`StateMSpec.validate`
    before being returned; malformed FSMs raise :class:`ValueError`
    at AST-lowering time.
    """
    if type(node).__name__ != "NodeDecl":
        raise TypeError(
            f"expected NodeDecl, got {type(node).__name__}")
    body = getattr(node, "statem", None)
    if body is None:
        return None

    # `states` is a sequence of string IDs in declaration order; `initial`
    # is a cross-ref but textX resolves it to the matching string in
    # `states` since we declared `initial=[ID]`.
    states = tuple(body.states)
    initial = body.initial if isinstance(body.initial, str) else str(body.initial)

    data_fqn: Optional[str] = None
    data_ref = getattr(body, "data_type", None)
    if data_ref is not None:
        data_fqn = _qualify_message(data_ref)

    blocks = []
    for blk_ast in body.on_blocks:
        st_ref = blk_ast.state
        state_name = st_ref if isinstance(st_ref, str) else str(st_ref)
        rules = tuple(_rule_from_ast(r) for r in blk_ast.rules)
        blocks.append(StateBlock(state=state_name, rules=rules))

    spec = StateMSpec(
        states=states,
        initial=initial,
        data_fqn=data_fqn,
        blocks=tuple(blocks),
    )
    spec.validate()
    return spec
