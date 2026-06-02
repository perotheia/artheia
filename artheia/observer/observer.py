"""TraceObserver — subscribe to the log[trace] firehose, yield decoded records.

The OTP `dbg:tracer` shape for Theia: bind a subscriber TIPC address, call
TraceCtl.Subscribe, then receive the records the TraceStreamPump fans out —
each decoded (libprotobuf via the probe Codec) into a header dict + JSON
payload. All internal TIPC, no gRPC.

Generic: the collector addresses + the Subscribe service_id are resolved from
the parsed log `.art` (an ArtheiaContext), never hardcoded — so the observer
tracks whatever the .art declares.

    from artheia.observer import TraceObserver
    obs = TraceObserver.from_log_art("services/log/system/log/component.art",
                                     proto_root="platform/proto")
    obs.start()                       # bind + Subscribe
    for rec in obs.records(timeout=5):# stream decoded TraceRecords
        print(rec.node_name, rec.msg_type, rec.kind, rec.json)
    obs.stop()
"""
from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from artheia.gen_server.probe import wire
from artheia.gen_server.probe.codec import Codec
from artheia.gen_server.probe.context import ArtheiaContext
from artheia.gen_server.probe.transport import TipcClient, TipcServer

# The trace types live in the log package; names are stable per the .art.
_LOG_PKG = "system.services.log"
_RECORD = "system_services_log_TraceRecord"
_SUBSCRIBE = "system_services_log_SubscribeReq"

# Observer subscriber service type (per the cluster-netid/observer-addr design:
# one TraceSubscriber type, distinct instance per observer).
SUBSCRIBER_TYPE = 0x8001001A


@dataclass
class TraceRec:
    """One decoded trace record: the header fields + the raw payload + JSON."""
    src: str                # emitting node
    dst: str                # peer node ("" if none)
    msg_type: str           # wire-type name
    corr_id: int
    ts_ns: int
    payload: bytes          # the wrapped [header][proto-wire] of the traced msg
    json: str               # JSON serialization of the decoded TraceRecord


class TraceObserver:
    def __init__(self, ctx: ArtheiaContext, *,
                 subscriber_type: int = SUBSCRIBER_TYPE,
                 kind_filter: int = 0, node_filter: str = ""):
        self.ctx = ctx
        self.codec: Codec = ctx.codec
        self._sub_type = subscriber_type
        self._sub_instance = os.getpid() & 0xFFFF
        self._kind_filter = kind_filter
        self._node_filter = node_filter

        # Collector endpoints resolved from the .art (generic, not hardcoded).
        self._ctl = ctx.ref("TraceCtl")
        self._sub_op = self._ctl.find_op("Subscribe")

        self._server: Optional[TipcServer] = None
        self._q: "queue.Queue[TraceRec]" = queue.Queue()
        self._record_sid = wire.service_id(_RECORD)
        self._corr = 0
        self._lock = threading.Lock()

    @classmethod
    def from_log_art(cls, log_art: str | Path, *, proto_root: str | Path,
                     **kw) -> "TraceObserver":
        ctx = ArtheiaContext(str(log_art), proto_root)
        return cls(ctx, **kw)

    # ---- lifecycle --------------------------------------------------------
    def start(self, timeout: float = 3.0) -> "TraceObserver":
        """Bind the subscriber socket, then Subscribe to TraceCtl."""
        self._server = TipcServer(self._sub_type, self._sub_instance,
                                  self._on_frame)
        self._server.start()
        if not self._subscribe(timeout):
            self._server.stop()
            raise ConnectionError("TraceObserver: Subscribe to TraceCtl failed")
        return self

    def _subscribe(self, timeout: float) -> bool:
        req = self.codec.encode(
            _LOG_PKG, self._sub_op.request.proto_type,
            sub_type=self._sub_type, sub_instance=self._sub_instance,
            kind=self._kind_filter, target_node=self._node_filter)
        ctl = TipcClient(self._ctl.tipc_type, self._ctl.tipc_instance)
        if not ctl.connect():
            return False
        with self._lock:
            self._corr = (self._corr + 1) & 0xFFFFFFFF
            corr = self._corr
        hdr = wire.Header(msg_type=wire.MSG_GEN_CALL, proto_len=len(req),
                          service_id=self._sub_op.request.service_id,
                          correlation_id=corr)
        ctl.send(wire.frame(hdr, req))
        reply = ctl.recv_reply(timeout=timeout)   # TraceEmpty ack
        ctl.close()
        return reply is not None

    def stop(self) -> None:
        if self._server:
            self._server.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- inbound fan-out (server loop thread) -----------------------------
    def _on_frame(self, hdr: wire.Header, payload: bytes, _conn) -> None:
        if hdr.service_id != self._record_sid:
            return
        rec = self._decode(payload)
        if rec is not None:
            self._q.put(rec)

    def _decode(self, payload: bytes) -> Optional[TraceRec]:
        from google.protobuf import json_format
        cls = self.codec._message_class(_LOG_PKG, _RECORD)  # cached _pb2 class
        msg = cls()
        msg.ParseFromString(payload)
        # The proto field is `node_name` (= src; back-compat alias per
        # services/log/system/log/package.art), NOT `src`. Read the real
        # field name; TraceRec keeps `src` as the observer's vocabulary.
        return TraceRec(
            src=msg.node_name, dst=msg.dst, msg_type=msg.msg_type,
            corr_id=msg.corr_id, ts_ns=msg.ts_ns,
            payload=bytes(msg.payload),
            json=json_format.MessageToJson(msg, indent=None),
        )

    # ---- record stream ----------------------------------------------------
    def records(self, timeout: float = 5.0) -> Iterator[TraceRec]:
        """Yield decoded records as they arrive, until `timeout` of silence."""
        while True:
            try:
                yield self._q.get(timeout=timeout)
            except queue.Empty:
                return

    def next_record(self, timeout: float = 5.0) -> Optional[TraceRec]:
        """Block for the next record (None on timeout)."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None
