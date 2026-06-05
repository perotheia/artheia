"""Probe TIPC transport tests — connect resilience to stale co-bindings.

A TIPC service name (type, instance) can have several bound ports at once:
a supervisor restart briefly overlaps the old + new binding, and a SIGKILL'd
predecessor leaves a binding the kernel hasn't reaped. connect() to a
TIPC_ADDR_NAME load-balances across ALL bound ports, so a single attempt can
land on a DEAD port and time out even though a live binding exists. The probe's
TipcClient.connect() must RETRY on a fresh socket — each attempt re-rolls which
port the kernel picks — until it lands on the live binding within its budget.

These tests skip cleanly when AF_TIPC is unavailable (no `modprobe tipc`).
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from artheia.gen_server.probe.transport import TipcClient


def _tipc_available() -> bool:
    try:
        s = socket.socket(socket.AF_TIPC, socket.SOCK_SEQPACKET)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _tipc_available(), reason="AF_TIPC unavailable (run `modprobe tipc`)"
)


# A high, unlikely-to-collide TIPC service type for the test rig.
_TEST_TYPE = 0x7F00BEEF
_TEST_INST = 0


class _Binding:
    """A TIPC service binding on (_TEST_TYPE, _TEST_INST). `accepting=True`
    drains its accept queue (a LIVE service); `accepting=False` binds + listens
    but never accept()s (a WEDGED/dead port that a connect SYN times out on)."""

    def __init__(self, accepting: bool):
        self.accepting = accepting
        self._sock = socket.socket(socket.AF_TIPC, socket.SOCK_SEQPACKET)
        # bind the single instance as the range [inst, inst]
        self._sock.bind((socket.TIPC_ADDR_NAMESEQ, _TEST_TYPE,
                         _TEST_INST, _TEST_INST))
        self._sock.listen(16)
        self._run = True
        self._conns: list[socket.socket] = []
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        self._sock.settimeout(0.1)
        while self._run:
            if not self.accepting:
                time.sleep(0.05)
                continue
            try:
                c, _ = self._sock.accept()
                self._conns.append(c)
            except (OSError, socket.timeout):
                pass

    def close(self):
        self._run = False
        self._t.join(timeout=1.0)
        for c in self._conns:
            c.close()
        self._sock.close()


def test_connect_succeeds_with_live_binding():
    """One live binding, no stale ports → connect succeeds quickly."""
    live = _Binding(accepting=True)
    try:
        c = TipcClient(_TEST_TYPE, _TEST_INST)
        assert c.connect(total_timeout_ms=3000) is True
        c.close()
    finally:
        live.close()


def test_connect_survives_dead_co_bindings():
    """The regression: several WEDGED bindings co-exist with one LIVE binding
    on the same TIPC name. connect() load-balances onto a dead port most of the
    time, but the retry-on-fresh-socket loop re-rolls until it hits the live one
    within the budget. Reproduces the 1-live-of-N stale-binding pileup that made
    `tdb info` fail ~90% of the time before the fix."""
    dead = [_Binding(accepting=False) for _ in range(4)]
    live = _Binding(accepting=True)
    try:
        c = TipcClient(_TEST_TYPE, _TEST_INST)
        t0 = time.monotonic()
        ok = c.connect(total_timeout_ms=4000)
        elapsed = time.monotonic() - t0
        assert ok is True, "connect must find the live binding among dead ones"
        # Should not have to burn the whole budget — a handful of re-rolls.
        assert elapsed < 3.5, f"connect took {elapsed:.2f}s (budget nearly spent)"
        c.close()
    finally:
        live.close()
        for d in dead:
            d.close()


def test_connect_absent_service_fails_within_budget():
    """No binding at all → connect() returns False within ~the budget (not the
    kernel's ~8s blocking-connect timeout, which would ignore total_timeout_ms)."""
    c = TipcClient(_TEST_TYPE + 1, _TEST_INST)  # nothing bound here
    t0 = time.monotonic()
    ok = c.connect(total_timeout_ms=1000)
    elapsed = time.monotonic() - t0
    assert ok is False
    # bounded: never the in-kernel ~8s hang; allow slack for slow CI.
    assert elapsed < 3.0, f"absent-service connect took {elapsed:.2f}s"
