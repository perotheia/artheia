"""End-to-end LSP smoke test.

Spawns `artheia-lsp` as a real subprocess and drives it over stdio using
the LSP JSON-RPC framing (`Content-Length` + JSON body) — the same way
VS Code does. Exercises:

  - initialize / initialized handshake
  - textDocument/didOpen (clean file → no diagnostics)
  - textDocument/didChange (broken file → 1 diagnostic with line/col)
  - textDocument/completion (returns keywords + workspace symbols)
  - textDocument/definition (resolves a known cross-ref to a location)

This catches everything except the visual rendering side: if the test is
green, an LSP-aware editor will get the same answers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pytest


REPO = Path(__file__).resolve().parents[1]


class _LspClient:
    """Minimal JSON-RPC client over stdio with the LSP framing."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._msg_id = 0
        self._responses: dict[int, dict] = {}
        self._notifications: Queue[dict] = Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        while True:
            # Read headers until the blank separator. pygls sends both
            # Content-Length and Content-Type, so we can't assume a single
            # header line — drain until "\r\n" by itself.
            length: int | None = None
            while True:
                raw = stdout.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    break
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            if length is None:
                continue
            body = stdout.read(length)
            try:
                msg = json.loads(body)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self._responses[msg["id"]] = msg
            else:
                self._notifications.put(msg)

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(header + body)
        stdin.flush()

    def request(self, method: str, params: Any, *, timeout: float = 5.0) -> dict:
        self._msg_id += 1
        my_id = self._msg_id
        self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            if my_id in self._responses:
                return self._responses.pop(my_id)
            time.sleep(0.005)
        raise AssertionError(f"timeout waiting for response to {method}")

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait_notification(self, method: str, *, timeout: float = 5.0) -> dict:
        """Drain until we see a notification matching `method`."""
        deadline = time.time() + timeout
        leftover: list[dict] = []
        try:
            while time.time() < deadline:
                try:
                    msg = self._notifications.get(timeout=0.1)
                except Empty:
                    continue
                if msg.get("method") == method:
                    for m in leftover:
                        self._notifications.put(m)
                    return msg
                leftover.append(msg)
        finally:
            for m in leftover:
                self._notifications.put(m)
        raise AssertionError(f"timeout waiting for notification {method}")


@pytest.fixture
def lsp_client():
    server = shutil.which("artheia-lsp")
    if server is None:
        # Fall back to the venv bin if the test is run with system python
        candidate = REPO / ".venv" / "bin" / "artheia-lsp"
        if candidate.exists():
            server = str(candidate)
    if server is None:
        pytest.skip("artheia-lsp not on PATH")

    proc = subprocess.Popen(
        [server],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    client = _LspClient(proc)
    try:
        result = client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": REPO.as_uri(),
            "capabilities": {
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False},
                    "completion": {"completionItem": {"snippetSupport": False}},
                    "definition": {"linkSupport": False},
                    "publishDiagnostics": {},
                },
            },
            "workspaceFolders": [{"uri": REPO.as_uri(), "name": "artheia"}],
        }, timeout=15.0)
        assert "result" in result
        client.notify("initialized", {})
        yield client
    finally:
        try:
            client.request("shutdown", None, timeout=2.0)
            client.notify("exit", None)
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()


_GOOD_SRC = """\
package p
message M { uint32 a = 1 }
interface senderReceiver If { data M m }
node atomic N {
    tipc type=0x80010001 instance=0
    ports { sender out provides If }
}
"""

# Missing closing brace on the node — should produce a diagnostic.
_BAD_SRC = """\
package p
node atomic N {
    tipc type=0x80010001 instance=0
"""


def _did_open(client: _LspClient, uri: str, src: str) -> None:
    client.notify("textDocument/didOpen", {
        "textDocument": {
            "uri": uri,
            "languageId": "artheia",
            "version": 1,
            "text": src,
        },
    })


def _did_change(client: _LspClient, uri: str, src: str, version: int) -> None:
    client.notify("textDocument/didChange", {
        "textDocument": {"uri": uri, "version": version},
        "contentChanges": [{"text": src}],
    })


def test_clean_file_gets_no_diagnostics(lsp_client: _LspClient, tmp_path: Path):
    f = tmp_path / "clean.art"
    f.write_text(_GOOD_SRC)
    uri = f.as_uri()
    _did_open(lsp_client, uri, _GOOD_SRC)
    msg = lsp_client.wait_notification("textDocument/publishDiagnostics")
    assert msg["params"]["uri"] == uri
    assert msg["params"]["diagnostics"] == []


def test_broken_file_surfaces_diagnostic(lsp_client: _LspClient, tmp_path: Path):
    f = tmp_path / "broken.art"
    f.write_text(_BAD_SRC)
    uri = f.as_uri()
    _did_open(lsp_client, uri, _BAD_SRC)
    msg = lsp_client.wait_notification("textDocument/publishDiagnostics")
    diags = msg["params"]["diagnostics"]
    assert len(diags) == 1
    d = diags[0]
    assert d["severity"] == 1  # Error
    assert d["source"] == "artheia"
    # message points at the lexer/parser problem
    assert d["range"]["start"]["line"] >= 0
    assert d["range"]["start"]["character"] >= 0


def test_completion_returns_keywords_and_symbols(lsp_client: _LspClient, tmp_path: Path):
    f = tmp_path / "doc.art"
    f.write_text(_GOOD_SRC)
    uri = f.as_uri()
    _did_open(lsp_client, uri, _GOOD_SRC)
    lsp_client.wait_notification("textDocument/publishDiagnostics")
    resp = lsp_client.request("textDocument/completion", {
        "textDocument": {"uri": uri},
        "position": {"line": 6, "character": 0},
    })
    items = resp["result"]["items"]
    labels = {item["label"] for item in items}
    # keywords present
    assert {"node", "composition", "message", "gateway_route"}.issubset(labels)
    # workspace symbols from the parsed file present
    assert {"M", "If", "N"}.issubset(labels)


def test_definition_resolves_cross_ref(lsp_client: _LspClient, tmp_path: Path):
    f = tmp_path / "doc.art"
    f.write_text(_GOOD_SRC)
    uri = f.as_uri()
    _did_open(lsp_client, uri, _GOOD_SRC)
    lsp_client.wait_notification("textDocument/publishDiagnostics")
    # Cursor on the `If` in `provides If` (line 5 in 0-indexed).
    line_idx = _GOOD_SRC.split("\n").index("    ports { sender out provides If }")
    col_idx = _GOOD_SRC.split("\n")[line_idx].index("If") + 1
    resp = lsp_client.request("textDocument/definition", {
        "textDocument": {"uri": uri},
        "position": {"line": line_idx, "character": col_idx},
    })
    result = resp["result"]
    assert result is not None
    assert result["uri"] == uri
    # Definition of `interface senderReceiver If { ... }` starts at line 2.
    assert result["range"]["start"]["line"] == 2


def test_diagnostics_clear_after_fix(lsp_client: _LspClient, tmp_path: Path):
    """Open broken, then fix it, and the next publish must have empty diagnostics."""
    f = tmp_path / "fix.art"
    f.write_text(_BAD_SRC)
    uri = f.as_uri()
    _did_open(lsp_client, uri, _BAD_SRC)
    first = lsp_client.wait_notification("textDocument/publishDiagnostics")
    assert len(first["params"]["diagnostics"]) == 1
    _did_change(lsp_client, uri, _GOOD_SRC, version=2)
    second = lsp_client.wait_notification("textDocument/publishDiagnostics")
    assert second["params"]["diagnostics"] == []
