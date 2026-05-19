"""Artheia Language Server implementation (pygls 2.x).

Re-uses the textX parser exposed by `artheia.model` to:

  - publish diagnostics on open/change/save,
  - resolve goto-definition for cross-refs (port → interface, prototype →
    node, ConnectDecl source/target → port, gateway_route → node, type
    references in messages and interfaces),
  - offer completion for keywords, in-workspace symbols (messages,
    interfaces, nodes), and gateway-catalog message names loaded from
    `gateway_catalog.json` files anywhere in the workspace.

Errors raised by textX carry a position (`.line` / `.col`); we translate
those into LSP Position/Range. The full source is reparsed on every change;
for the corpus sizes Artheia targets this is well under a millisecond.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer
from textx import TextXError, TextXSemanticError, TextXSyntaxError

from ..model import load_metamodel


logger = logging.getLogger(__name__)


# ---- workspace state ------------------------------------------------------

@dataclass
class _DocState:
    text: str = ""
    model: object | None = None
    error: TextXError | None = None


@dataclass
class _Workspace:
    docs: dict[str, _DocState] = field(default_factory=dict)
    catalog_messages: set[str] = field(default_factory=set)


_KEYWORDS = [
    "package", "import",
    "message", "enum", "interface", "senderReceiver", "clientServer",
    "data", "operation", "returns", "in", "out", "inout",
    "node", "atomic", "tipc", "type", "instance",
    "ports", "sender", "receiver", "client", "server",
    "provides", "requires",
    "composition", "prototype", "connect", "to",
    "params",
    "bus", "kind", "channels",
    "gateway_route", "can", "flexray", "id", "slot", "channel",
    "channel_idx", "dlc", "extended_id", "rtr", "cycle", "pdu_offset",
    "signal", "direction",
    "true", "false",
    "int32", "int64", "uint32", "uint64",
    "sint32", "sint64", "fixed32", "fixed64", "sfixed32", "sfixed64",
    "float", "double", "bool", "string", "bytes",
]


# ---- utilities ------------------------------------------------------------

def _uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


def _offset_to_position(text: str, offset: int) -> lsp.Position:
    if offset < 0:
        return lsp.Position(line=0, character=0)
    line = text.count("\n", 0, offset)
    last_nl = text.rfind("\n", 0, offset)
    col = offset - (last_nl + 1) if last_nl != -1 else offset
    return lsp.Position(line=line, character=col)


def _range_from_obj(text: str, obj) -> lsp.Range:
    start = getattr(obj, "_tx_position", None) or 0
    end = getattr(obj, "_tx_position_end", None) or start + 1
    return lsp.Range(
        start=_offset_to_position(text, start),
        end=_offset_to_position(text, end),
    )


def _range_for_textx_error(text: str, err: TextXError) -> lsp.Range:
    line = getattr(err, "line", None) or 1
    col = getattr(err, "col", None) or 1
    pos = lsp.Position(line=max(0, line - 1), character=max(0, col - 1))
    return lsp.Range(start=pos, end=lsp.Position(line=pos.line, character=pos.character + 1))


# ---- parsing --------------------------------------------------------------

def _parse(text: str):
    mm = load_metamodel()
    try:
        return mm.model_from_str(text), None
    except (TextXSyntaxError, TextXSemanticError, TextXError) as e:
        return None, e


# ---- catalog loading ------------------------------------------------------

_CATALOG_GLOBS = ("gateway_catalog*.json", "*.gateway-catalog.json")


def _scan_workspace_catalogs(root: Path) -> set[str]:
    """Find gateway catalogs under the workspace and union their message names.

    Only files matching `gateway_catalog*.json` or `*.gateway-catalog.json`
    are considered. Older `*catalog*.json` matches were too broad and
    picked up unrelated JSON (npm catalogs, etc.).
    """
    out: set[str] = set()
    if not root.is_dir():
        return out
    for pattern in _CATALOG_GLOBS:
        for path in root.rglob(pattern):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            messages = data.get("messages")
            if isinstance(messages, dict):
                out.update(messages.keys())
    return out


# ---- per-construct helpers ------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _identifier_at(text: str, offset: int) -> tuple[str | None, int, int]:
    if offset < 0 or offset > len(text):
        return None, 0, 0
    start = offset
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    end = offset
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    if start == end:
        return None, start, end
    return text[start:end], start, end


def _model_iter(model, cls_name: str) -> Iterable:
    for el in model.elements:
        if el.__class__.__name__ == cls_name:
            yield el


def _is_stub(el) -> bool:
    """A declaration is a 'stub' if its body has no children — the kind
    fusée emits for cross-file forward declarations. Prefer non-stubs when
    multiple files declare the same name. The attribute set covers every
    body-bearing top-level rule in the grammar (messages, enums,
    interfaces, nodes, compositions)."""
    for attr in ("fields", "values", "data", "operations", "ports", "elements"):
        children = getattr(el, attr, None)
        if children is not None:
            return len(children) == 0
    return False


def _find_definition(model, name: str):
    """Return the first declaration whose `name` matches, or None.

    Per-file lookup. Cross-file resolution lives in `_definition` below.
    """
    if model is None:
        return None
    for el in model.elements:
        if getattr(el, "name", None) == name:
            return el
    return None


def _find_definition_in_model(model, name: str):
    """Return (target, is_stub) or None — used by the cross-file resolver."""
    target = _find_definition(model, name)
    if target is None:
        return None
    return target, _is_stub(target)


# ---- server factory -------------------------------------------------------

def create_server() -> LanguageServer:
    ls = LanguageServer("artheia-lsp", "0.0.1")
    ws = _Workspace()

    # Eagerly load the metamodel so the first edit doesn't pay the cost.
    load_metamodel()

    def _publish(uri: str, state: _DocState) -> None:
        diagnostics: list[lsp.Diagnostic] = []
        if state.error is not None:
            diagnostics.append(lsp.Diagnostic(
                range=_range_for_textx_error(state.text, state.error),
                message=str(state.error),
                severity=lsp.DiagnosticSeverity.Error,
                source="artheia",
            ))
        ls.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
        )

    def _refresh(uri: str, text: str) -> _DocState:
        model, err = _parse(text)
        state = _DocState(text=text, model=model, error=err)
        ws.docs[uri] = state
        _publish(uri, state)
        return state

    # ---- lifecycle -------------------------------------------------------

    @ls.feature(lsp.INITIALIZED)
    def _on_initialized(params):
        # Discover gateway catalogs in the workspace (best-effort).
        try:
            folders = ls.workspace.folders  # type: ignore[attr-defined]
        except Exception:
            folders = {}
        loaded = 0
        for folder in folders.values():
            root = _uri_to_path(folder.uri)
            ws.catalog_messages.update(_scan_workspace_catalogs(root))
            # Eagerly parse every .art in the workspace so goto-definition
            # works across unopened files. We skip .venv / node_modules
            # directories to avoid stalling on vendor trees we don't own.
            for art in root.rglob("*.art"):
                if any(p in {".venv", "node_modules", ".git"} for p in art.parts):
                    continue
                uri = art.as_uri()
                if uri in ws.docs:
                    continue
                try:
                    text = art.read_text()
                except OSError:
                    continue
                model, err = _parse(text)
                ws.docs[uri] = _DocState(text=text, model=model, error=err)
                loaded += 1
        logger.info(
            "artheia-lsp ready (%d catalog messages, %d .art files preloaded)",
            len(ws.catalog_messages), loaded,
        )

    @ls.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    def _did_open(params: lsp.DidOpenTextDocumentParams):
        _refresh(params.text_document.uri, params.text_document.text)

    @ls.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
    def _did_change(params: lsp.DidChangeTextDocumentParams):
        doc = ls.workspace.get_text_document(params.text_document.uri)
        _refresh(params.text_document.uri, doc.source)

    @ls.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    def _did_save(params: lsp.DidSaveTextDocumentParams):
        doc = ls.workspace.get_text_document(params.text_document.uri)
        _refresh(params.text_document.uri, doc.source)

    # ---- goto-definition -------------------------------------------------

    @ls.feature(lsp.TEXT_DOCUMENT_DEFINITION)
    def _definition(params: lsp.DefinitionParams):
        state = ws.docs.get(params.text_document.uri)
        if state is None:
            return None
        doc = ls.workspace.get_text_document(params.text_document.uri)
        offset = doc.offset_at_position(params.position)
        name, _, _ = _identifier_at(state.text, offset)
        if not name:
            return None
        # Search every loaded doc; prefer non-stub declarations so a jump
        # from a forward-decl `interface X { }` lands on the real body
        # somewhere else in the workspace.
        best: tuple[str, _DocState, object] | None = None
        best_is_stub = True
        for uri, s in ws.docs.items():
            hit = _find_definition_in_model(s.model, name)
            if hit is None:
                continue
            target, is_stub = hit
            if best is None or (best_is_stub and not is_stub):
                best = (uri, s, target)
                best_is_stub = is_stub
                if not is_stub:
                    break
        if best is None:
            return None
        uri, s, target = best
        return lsp.Location(uri=uri, range=_range_from_obj(s.text, target))

    # ---- completion ------------------------------------------------------

    def _workspace_symbols() -> list[str]:
        names: set[str] = set()
        for s in ws.docs.values():
            if s.model is None:
                continue
            for el in s.model.elements:
                n = getattr(el, "name", None)
                if isinstance(n, str):
                    names.add(n)
        return sorted(names)

    @ls.feature(
        lsp.TEXT_DOCUMENT_COMPLETION,
        lsp.CompletionOptions(trigger_characters=[".", " ", "\n"]),
    )
    def _completion(params: lsp.CompletionParams):
        state = ws.docs.get(params.text_document.uri)
        items: list[lsp.CompletionItem] = []
        for kw in _KEYWORDS:
            items.append(lsp.CompletionItem(
                label=kw,
                kind=lsp.CompletionItemKind.Keyword,
            ))
        for sym in _workspace_symbols():
            items.append(lsp.CompletionItem(
                label=sym,
                kind=lsp.CompletionItemKind.Class,
                detail="(workspace symbol)",
            ))
        for sym in sorted(ws.catalog_messages):
            items.append(lsp.CompletionItem(
                label=sym,
                kind=lsp.CompletionItemKind.Struct,
                detail="(gateway message)",
            ))
        return lsp.CompletionList(is_incomplete=False, items=items)

    return ls


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    create_server().start_io()


if __name__ == "__main__":
    main()
