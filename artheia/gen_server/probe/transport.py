"""AF_TIPC / SOCK_SEQPACKET transport — the bytes layer for a probe.

Server side: bind a TIPC service address, listen, accept, recv framed messages
on a background select loop. Client side: connect to a peer's service address
(with retry, like TipcClient::connect) and send framed messages.

Mirrors platform/runtime TipcMux (bind) + NodeRef (connect).
"""
from __future__ import annotations

import select
import socket
import threading
import time
from typing import Callable, Optional

from . import wire

_AF_TIPC = socket.AF_TIPC

# Python's socket module speaks AF_TIPC natively via address tuples:
#   bind:    (TIPC_ADDR_NAMESEQ, type, lower, upper)  — publish a service range
#   connect: (TIPC_ADDR_NAME,    type, instance, domain=0)  — reach a service
# This is the same TIPC_SERVICE_ADDR / type+instance the C++ TipcMux/NodeRef
# use; the kernel routes by (type, instance), so probe and FC meet.


def _bind_addr(tipc_type: int, tipc_instance: int) -> tuple:
    # Publish the single instance as the range [instance, instance] so a
    # connect to exactly (type, instance) resolves. (Publishing [0,0] would
    # only answer instance 0 — a peer addressing any other instance fails.)
    return (socket.TIPC_ADDR_NAMESEQ, tipc_type, tipc_instance, tipc_instance)


def _connect_addr(tipc_type: int, tipc_instance: int) -> tuple:
    return (socket.TIPC_ADDR_NAME, tipc_type, tipc_instance, 0)


class TipcServer:
    """Bind one TIPC service address; recv framed messages on a select loop.

    on_frame(header, payload, conn_sock) is called on the loop thread for every
    inbound RPC frame. A handler may reply on conn_sock (for a CALL).
    """

    def __init__(self, tipc_type: int, tipc_instance: int,
                 on_frame: Callable[[wire.Header, bytes, socket.socket], None]):
        self.tipc_type = tipc_type
        self.tipc_instance = tipc_instance
        self._on_frame = on_frame
        self._listen: Optional[socket.socket] = None
        self._conns: list[socket.socket] = []
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        s = socket.socket(_AF_TIPC, socket.SOCK_SEQPACKET)
        s.bind(_bind_addr(self.tipc_type, self.tipc_instance))
        s.listen(16)
        self._listen = s
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            rlist = [self._listen] + self._conns
            try:
                ready, _, _ = select.select(rlist, [], [], 0.1)
            except (OSError, ValueError):
                break
            for sk in ready:
                if sk is self._listen:
                    try:
                        conn, _ = self._listen.accept()
                        self._conns.append(conn)
                    except OSError:
                        pass
                    continue
                try:
                    buf = sk.recv(65536)
                except OSError:
                    buf = b""
                if not buf:
                    self._conns.remove(sk)
                    sk.close()
                    continue
                if len(buf) < wire.HEADER_SIZE:
                    continue
                hdr = wire.Header.unpack(buf)
                payload = buf[wire.HEADER_SIZE:wire.HEADER_SIZE + hdr.proto_len]
                self._on_frame(hdr, payload, sk)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        for c in self._conns:
            c.close()
        self._conns.clear()
        if self._listen:
            self._listen.close()
            self._listen = None


class TipcDgramServer:
    """Bind a TIPC SOCK_RDM (datagram) service address; recv framed casts on a
    select loop. This is the PG MULTICAST receive side — the supervisor allocates
    a group {type, instance}, the member binds it here, and a broadcaster's
    name-sequence sendto delivers a copy to every bound member. SOCK_RDM (not
    SEQPACKET) because TIPC multicast is connectionless; matches the C++
    PgClient recv socket.

    on_frame(header, payload) is called on the loop thread per inbound frame.
    """

    def __init__(self, tipc_type: int, tipc_instance: int,
                 on_frame: Callable[[wire.Header, bytes], None]):
        self.tipc_type = tipc_type
        self.tipc_instance = tipc_instance
        self._on_frame = on_frame
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        s = socket.socket(_AF_TIPC, socket.SOCK_RDM)
        s.bind(_bind_addr(self.tipc_type, self.tipc_instance))
        self._sock = s
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                buf = self._sock.recv(65536)
            except OSError:
                continue
            if len(buf) < wire.HEADER_SIZE:
                continue
            hdr = wire.Header.unpack(buf)
            payload = buf[wire.HEADER_SIZE:wire.HEADER_SIZE + hdr.proto_len]
            self._on_frame(hdr, payload)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()
            self._sock = None


def tipc_multicast_send(group_type: int, data: bytes) -> None:
    """Send `data` to the WHOLE process group: one SOCK_RDM datagram to the
    name-sequence (group_type, 0..~0) → the TIPC kernel fans out a copy to every
    bound member. Matches the C++ PgClient::send_multicast_ (TIPC_ADDR_NAMESEQ
    range send). Best-effort (lossy)."""
    s = socket.socket(_AF_TIPC, socket.SOCK_RDM)
    try:
        # (TIPC_ADDR_NAMESEQ, type, lower, upper) — a RANGE addresses ALL
        # instances bound under `type`, i.e. multicast (not anycast).
        s.sendto(data, (socket.TIPC_ADDR_NAMESEQ, group_type, 0, 0xFFFFFFFF))
    except OSError:
        pass
    finally:
        s.close()


class TipcClient:
    """Connect to a peer's TIPC service address; send framed messages.

    Reply demux for CALL is handled by the owning NodeProbe (it reads the
    reply on this socket after send). One client per (type,instance) target.
    """

    def __init__(self, tipc_type: int, tipc_instance: int):
        self.tipc_type = tipc_type
        self.tipc_instance = tipc_instance
        self._sock: Optional[socket.socket] = None

    def connect(self, total_timeout_ms: int = 3000,
                attempt_ms: int = 150, idle_retry_ms: int = 50) -> bool:
        # NON-BLOCKING, MANY-ATTEMPT connect. Two failure modes have to be
        # survived, and they pull in opposite directions:
        #
        #  (1) Absent / not-yet-accepting service. A BLOCKING SOCK_SEQPACKET
        #      connect() hangs in-kernel for TIPC's own ~8s timeout, ignoring
        #      total_timeout_ms. settimeout() bounds each attempt so one blocked
        #      connect can't eat the whole budget.
        #
        #  (2) STALE co-bindings. A TIPC name (type,instance) can have several
        #      bound ports at once — a supervisor restart briefly overlaps old +
        #      new, and a SIGKILL'd predecessor leaves a binding the kernel never
        #      reaped. connect() to a TIPC_ADDR_NAME load-balances across ALL
        #      bound ports, so it may pick a DEAD one and time out even though a
        #      live binding exists. The only cure is to RETRY on a fresh socket:
        #      each attempt re-rolls which port the kernel picks, so within a few
        #      attempts one lands on the live binding. (Seen in the wild at 1
        #      live of 13 stale bindings → needs ~13 tries.)
        #
        # So: short per-attempt cap + retry immediately on a fresh socket (NO
        # inter-attempt sleep when the attempt itself consumed real time — that
        # already paced us and the next attempt re-rolls the port). Only sleep a
        # little when a connect failed INSTANTLY (ECONNREFUSED on a truly absent
        # service) to avoid a tight busy-spin burning the budget in microseconds.
        deadline = time.monotonic() + total_timeout_ms / 1000.0
        addr = _connect_addr(self.tipc_type, self.tipc_instance)
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return False
            s = socket.socket(_AF_TIPC, socket.SOCK_SEQPACKET)
            s.settimeout(min(remain, attempt_ms / 1000.0))
            t0 = time.monotonic()
            try:
                s.connect(addr)
                s.settimeout(None)        # back to blocking for send/recv
                self._sock = s
                return True
            except (OSError, socket.timeout):
                s.close()
                # If the attempt returned almost immediately it was a fast
                # refusal (no port answered) — pace before re-rolling so an
                # absent service doesn't spin. If it consumed its slice it hit a
                # (likely dead) port; retry at once to re-roll onto another.
                if time.monotonic() - t0 < (attempt_ms / 1000.0) / 2:
                    time.sleep(idle_retry_ms / 1000.0)

    def send(self, data: bytes) -> None:
        if self._sock is None and not self.connect():
            raise ConnectionError(
                f"cannot connect to TIPC {{0x{self.tipc_type:08x},"
                f"{self.tipc_instance}}}"
            )
        self._sock.sendall(data)

    def recv_reply(self, timeout: float) -> Optional[tuple[wire.Header, bytes]]:
        """Block (up to timeout) for a single reply frame on this socket."""
        if self._sock is None:
            return None
        ready, _, _ = select.select([self._sock], [], [], timeout)
        if not ready:
            return None
        buf = self._sock.recv(65536)
        if len(buf) < wire.HEADER_SIZE:
            return None
        hdr = wire.Header.unpack(buf)
        return hdr, buf[wire.HEADER_SIZE:wire.HEADER_SIZE + hdr.proto_len]

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None
