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
    payload: bytes          # the inner message's proto-wire bytes (no header)
    json: str               # JSON serialization of the TraceRecord ENVELOPE
    content: "Optional[dict]" = None  # the inner msg decoded by msg_type, or None
    kind: str = ""          # TraceKind enum name (e.g. "CALL_OUT"), "" if unset
    from_state: str = ""    # STATEM only: state left ("OFF"); "" otherwise
    to_state: str = ""      # STATEM only: state entered ("STARTING")
    data_type: str = ""     # STATEM only: type name of the `data` msg in payload
    data: "Optional[dict]" = None  # STATEM only: the decoded FSM data (OTP Data
                            # term), keyed on data_type; None if absent/undecodable

    def to_dict(self, *, ts: "Optional[str]" = None) -> dict:
        """Full record as JSON-ready dict: header fields + decoded inner proto.

        `content` carries the decoded inner message; bytes fields in it are
        hex-encoded so the dict is JSON-serializable.

        `ts`, when given, is a caller-formatted human timestamp emitted as the
        `ts` field (the raw `ts_ns` is the EMITTING NODE's monotonic-from-start
        nanoseconds — not a wall clock — so a readable wall-clock stamp is the
        observer's receive time, supplied by the caller). `dst` is omitted when
        empty (the producer doesn't populate a peer yet).
        """
        def jsonable(x):
            if isinstance(x, (bytes, bytearray)):
                return x.hex()
            return x
        content = None
        if self.content is not None:
            content = {k: jsonable(v) for k, v in self.content.items()}
        out: dict = {}
        if ts is not None:
            out["ts"] = ts
        else:
            out["ts_ns"] = self.ts_ns
        if self.dst:
            out["dst"] = self.dst
        out.update({
            "src": self.src,
            "msg_type": self.msg_type,
            "kind": self.kind,
            "corr_id": self.corr_id,
            "content": content,
        })
        # STATEM records: surface the transition + the decoded FSM data (OTP
        # `{State, Data}`). Omitted on non-STATEM records so the dict stays lean.
        if self.to_state:
            out["from_state"] = self.from_state
            out["to_state"] = self.to_state
        if self.data is not None:
            out["data"] = {k: jsonable(v) for k, v in self.data.items()}
        return out


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
        # For a STATEM record the payload is the FSM `data` message (decoded
        # via the data_type field), NOT the triggering event — so decode it
        # under data_type. Every other record's payload is the traced message
        # itself, decoded under msg_type.
        data_type = str(getattr(msg, "data_type", "") or "")
        if data_type:
            fsm_data = self._decode_inner(data_type, bytes(msg.payload))
            inner = None
        else:
            fsm_data = None
            inner = self._decode_inner(msg.msg_type, bytes(msg.payload))
        # kind is a TraceKind enum (field 6). Resolve its symbolic name from
        # the enum descriptor; fall back to the raw int if unnamed. gen-proto
        # prefixes enum members with the enum name (nanopb compat) →
        # "TraceKind_CALL_OUT"; strip it so every consumer (human + json) sees
        # just CALL_OUT.
        try:
            kind_name = msg.DESCRIPTOR.fields_by_name["kind"].enum_type \
                .values_by_number[msg.kind].name
            if kind_name.startswith("TraceKind_"):
                kind_name = kind_name[len("TraceKind_"):]
        except Exception:
            kind_name = str(getattr(msg, "kind", "") or "")
        return TraceRec(
            src=msg.node_name, dst=msg.dst, msg_type=msg.msg_type,
            corr_id=msg.corr_id, ts_ns=msg.ts_ns,
            payload=bytes(msg.payload),
            json=json_format.MessageToJson(msg, indent=None),
            content=inner,
            kind=kind_name,
            # STATEM transition state names (fields 8/9); "" on non-STATEM
            # records + on an older .so that predates the proto extension.
            from_state=str(getattr(msg, "from_state", "") or ""),
            to_state=str(getattr(msg, "to_state", "") or ""),
            # STATEM FSM data (OTP Data term): field 10 type name + decoded dict.
            data_type=data_type,
            data=fsm_data,
        )

    def _decode_inner(self, msg_type: str, payload: bytes) -> Optional[dict]:
        """Decode the INNER traced message from its proto-wire bytes.

        The record's `payload` is the raw proto3 wire of the traced message
        (e.g. system_demo_GetReply); `msg_type` is its flat nanopb type name.
        Resolve that type to its _pb2 class via the probe Codec and decode to a
        plain {field: value} dict. Returns None on empty payload or any failure
        (best-effort — a record we can't decode the body of still renders its
        envelope).

        msg_type is `<flat_package>_<Message>`; we don't know a-priori where the
        package ends, so try successively-shorter prefixes as the art package
        (system.demo.Get? system.demo → Get? …) until _message_class resolves.
        Results cache inside Codec, so the trial cost is paid once per type.
        """
        if not payload or not msg_type:
            return None
        parts = msg_type.split("_")
        # Try the longest package prefix first (most specific): for
        # 'system_demo_GetReply' try 'system.demo.Get'→fail, 'system.demo'→ok.
        for cut in range(len(parts) - 1, 0, -1):
            art_package = ".".join(parts[:cut])
            try:
                cls = self.codec._message_class(art_package, msg_type)
            except Exception:
                continue
            try:
                inner = cls()
                inner.ParseFromString(payload)
                return {f.name: getattr(inner, f.name)
                        for f in cls.DESCRIPTOR.fields}
            except Exception:
                return None
        return None

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
