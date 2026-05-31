"""NodeProbe — a Python gen_server that mocks one node over TIPC.

Impersonates the node it's built from (binds that node's TIPC address) and
supports every gen_server operation against other nodes/FCs:

  active  (probe drives the FC):
    cast(target, msg, **fields)         -> fire-and-forget   (MSG_GEN_CAST)
    call(target, op, timeout, **fields) -> sync request/reply (MSG_GEN_CALL)
  passive (probe mocks a peer the FC talks to):
    on_cast(msg_name, handler)          react to inbound casts
    on_call(op_name, responder)         answer inbound calls (-> reply dict)
    expect_cast(msg_name, timeout)      block until a cast arrives; return it
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import wire
from .context import ArtheiaContext, RemoteRef
from .transport import TipcClient, TipcServer


class NodeProbe:
    def __init__(self, ctx: ArtheiaContext, me: RemoteRef):
        self.ctx = ctx
        self.me = me
        self._server: Optional[TipcServer] = None
        self._clients: dict[tuple[int, int], TipcClient] = {}
        self._corr = 0
        self._lock = threading.Lock()

        # passive handlers, keyed by inbound service_id
        self._cast_handlers: dict[int, Callable[[dict], None]] = {}
        self._call_responders: dict[int, _CallResponder] = {}
        # received casts, by service_id, for expect_cast()
        self._inbox: dict[int, queue.Queue] = {}
        # received calls, by request service_id, for expect_call()
        self._call_inbox: dict[int, queue.Queue] = {}

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> "NodeProbe":
        self._server = TipcServer(
            self.me.tipc_type, self.me.tipc_instance, self._on_frame)
        self._server.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.stop()
        self.reset_clients()

    def reset_clients(self) -> None:
        """Drop (and close) cached outbound connections — the next cast/call
        reconnects fresh. Useful when a peer recycled its accept socket."""
        for c in self._clients.values():
            c.close()
        self._clients.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- active: drive an FC ---------------------------------------------
    def cast(self, target, msg_name: str, **fields) -> None:
        """Fire a cast of `msg_name` at `target` (node name or RemoteRef)."""
        tref = self._resolve(target)
        m = tref.find_msg(msg_name)
        payload = self.ctx.codec.encode(m.art_package, m.proto_type, **fields)
        hdr = wire.Header(
            msg_type=wire.MSG_GEN_CAST,
            proto_len=len(payload),
            service_id=m.service_id,
            correlation_id=0,
            timestamp_ns=time.time_ns(),
        )
        self._client(tref).send(wire.frame(hdr, payload))

    def call(self, target, op_name: str, timeout: float = 2.0, **fields) -> dict:
        """Call `op_name` on `target`; block for the reply; return it as dict."""
        tref = self._resolve(target)
        op = tref.find_op(op_name)
        req = op.request
        payload = self.ctx.codec.encode(req.art_package, req.proto_type, **fields)
        with self._lock:
            self._corr = (self._corr + 1) & 0xFFFFFFFF
            corr = self._corr
        hdr = wire.Header(
            msg_type=wire.MSG_GEN_CALL,
            proto_len=len(payload),
            service_id=req.service_id,
            correlation_id=corr,
            timestamp_ns=time.time_ns(),
        )
        client = self._client(tref)
        client.send(wire.frame(hdr, payload))
        # The FC replies on the same SEQPACKET connection.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = client.recv_reply(deadline - time.monotonic())
            if got is None:
                break
            rhdr, rpayload = got
            if rhdr.msg_type != wire.MSG_GEN_CALL_REPLY:
                continue
            if rhdr.correlation_id != corr:
                continue
            if op.reply is None:
                return {}
            return self.ctx.codec.decode(
                op.reply.art_package, op.reply.proto_type, rpayload)
        raise TimeoutError(
            f"call {self.me.name}->{tref.name}.{op_name} timed out after {timeout}s")

    # ---- passive: mock a peer --------------------------------------------
    def on_cast(self, msg_name: str, handler: Callable[[dict], None]) -> None:
        m = self.me.find_msg(msg_name)
        self._cast_handlers[m.service_id] = (m, handler)

    def on_call(self, op_name: str,
                responder: Callable[[dict], dict]) -> None:
        op = self.me.find_op(op_name)
        self._call_responders[op.request.service_id] = _CallResponder(op, responder)

    def expect_cast(self, msg_name: str, timeout: float = 2.0) -> dict:
        """Block until a cast of `msg_name` lands on this probe; return fields."""
        m = self.me.find_msg(msg_name)
        q = self._inbox.setdefault(m.service_id, queue.Queue())
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"{self.me.name} expected cast {msg_name!r} within {timeout}s")

    def expect_call(self, op_name: str, reply: dict | None = None,
                    timeout: float = 2.0) -> dict:
        """Block until a CALL of `op_name` lands on this probe.

        Returns the decoded request fields (for the test to assert on) and
        sends `reply` back to the caller on the same connection. `reply` is the
        reply-message field dict (e.g. {"value": 42}); pass {} or None for an
        empty/void reply. Complements on_call() — use this when the test wants
        to capture+assert the request inline rather than register a responder.

        Registers a one-shot capture for this op's request service_id, so a
        call that arrives BEFORE expect_call() is reachable too (queued).
        """
        op = self.me.find_op(op_name)
        sid = op.request.service_id
        q = self._call_inbox.setdefault(sid, queue.Queue())
        try:
            pending: "_PendingCall" = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"{self.me.name} expected call {op_name!r} within {timeout}s")
        # Send the reply on the captured connection (caller is blocked on it).
        if op.reply is not None:
            rpayload = self.ctx.codec.encode(
                op.reply.art_package, op.reply.proto_type, **(reply or {}))
            rhdr = wire.Header(
                msg_type=wire.MSG_GEN_CALL_REPLY,
                proto_len=len(rpayload),
                service_id=op.reply.service_id,
                correlation_id=pending.correlation_id,
                timestamp_ns=time.time_ns(),
            )
            pending.conn.sendall(wire.frame(rhdr, rpayload))
        return pending.request

    # ---- inbound dispatch (server loop thread) ---------------------------
    def _on_frame(self, hdr: wire.Header, payload: bytes, conn) -> None:
        if hdr.msg_type == wire.MSG_GEN_CAST:
            self._dispatch_cast(hdr, payload)
        elif hdr.msg_type == wire.MSG_GEN_CALL:
            self._dispatch_call(hdr, payload, conn)
        # replies are read synchronously in call(); ignore here.

    def _dispatch_cast(self, hdr: wire.Header, payload: bytes) -> None:
        entry = self._cast_handlers.get(hdr.service_id)
        # Always feed expect_cast inbox (decode if we can identify the type).
        m = entry[0] if entry else self._msg_by_service_id(hdr.service_id)
        fields = self.ctx.codec.decode(m.art_package, m.proto_type, payload) \
            if m else {"_raw": payload}
        if hdr.service_id in self._inbox:
            self._inbox[hdr.service_id].put(fields)
        if entry:
            entry[1](fields)

    def _dispatch_call(self, hdr: wire.Header, payload: bytes, conn) -> None:
        r = self._call_responders.get(hdr.service_id)
        if r is not None:
            # Registered responder (on_call): decode, respond, reply inline.
            req_fields = self.ctx.codec.decode(
                r.op.request.art_package, r.op.request.proto_type, payload)
            reply = r.responder(req_fields) or {}
            if r.op.reply is None:
                return
            rpayload = self.ctx.codec.encode(
                r.op.reply.art_package, r.op.reply.proto_type, **reply)
            rhdr = wire.Header(
                msg_type=wire.MSG_GEN_CALL_REPLY,
                proto_len=len(rpayload),
                service_id=r.op.reply.service_id,
                correlation_id=hdr.correlation_id,
                timestamp_ns=time.time_ns(),
            )
            conn.sendall(wire.frame(rhdr, rpayload))
            return

        # No responder — hand the call to a waiting (or future) expect_call().
        # It owns the reply (on the captured conn). Decode the request via the
        # op whose request service_id matches.
        op = self._op_by_request_service_id(hdr.service_id)
        req_fields = self.ctx.codec.decode(
            op.request.art_package, op.request.proto_type, payload) if op \
            else {"_raw": payload}
        q = self._call_inbox.setdefault(hdr.service_id, queue.Queue())
        q.put(_PendingCall(request=req_fields, conn=conn,
                           correlation_id=hdr.correlation_id))

    # ---- helpers ----------------------------------------------------------
    def _resolve(self, target) -> RemoteRef:
        return target if isinstance(target, RemoteRef) else self.ctx.ref(target)

    def _client(self, tref: RemoteRef) -> TipcClient:
        key = (tref.tipc_type, tref.tipc_instance)
        c = self._clients.get(key)
        if c is None:
            c = TipcClient(tref.tipc_type, tref.tipc_instance)
            if not c.connect():
                raise ConnectionError(f"probe could not connect to {tref.name}")
            self._clients[key] = c
        return c

    def _msg_by_service_id(self, sid: int):
        """Find a message on THIS node whose service_id matches (for inbox)."""
        for p in self.me.ports:
            for d in p.data:
                if d.service_id == sid:
                    return d
            for op in p.ops:
                for m in (op.request, op.reply):
                    if m and m.service_id == sid:
                        return m
        return None

    def _op_by_request_service_id(self, sid: int):
        """Find an op on THIS node whose REQUEST service_id matches."""
        for p in self.me.ports:
            for op in p.ops:
                if op.request.service_id == sid:
                    return op
        return None


@dataclass
class _PendingCall:
    """A received CALL awaiting expect_call() to reply on its connection."""
    request: dict
    conn: object
    correlation_id: int


class _CallResponder:
    def __init__(self, op, responder):
        self.op = op
        self.responder = responder
