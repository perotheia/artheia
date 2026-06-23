"""TraceObserver — JOIN the log[trace] PG group, yield decoded records.

The OTP `dbg:tracer` shape for Theia, now over PG (process-group) multicast: the
observer pg_joins the TraceRecord group (the supervisor allocates its delivery
address), and TraceStreamPump PG-multicasts every record — the kernel fans out a
copy to this observer (and every other joiner). Each record is decoded
(libprotobuf via the probe Codec) into a header dict + JSON. All internal TIPC,
no gRPC, no Subscribe RPC, no ring backlog (this is a LIVE tail).

Group identity = the wire type NAME (system_services_log_TraceRecord), the same
well-known name the C++ pump uses (msg_type_name<TraceRecord>()).

    from artheia.observer import TraceObserver
    obs = TraceObserver.from_log_art("services/log/system/log/component.art",
                                     proto_root="platform/proto")
    obs.start()                       # pg_join the TraceRecord group
    for rec in obs.records(timeout=5):# stream decoded TraceRecords (live)
        print(rec.src, rec.msg_type, rec.kind, rec.json)
    obs.stop()
"""
from __future__ import annotations

import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from artheia.gen_server.probe.codec import Codec
from artheia.gen_server.probe.context import ArtheiaContext
from artheia.gen_server.probe.pg import PgProbe

# The trace types live in the log package; names are stable per the .art.
_LOG_PKG = "system.services.log"
_RECORD = "system_services_log_TraceRecord"
# The PG group name = the TraceRecord wire type name (well-known, .art-derived).
_TRACE_GROUP = _RECORD


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
                 kind_filter: int = 0, node_filter: str = ""):
        self.ctx = ctx
        self.codec: Codec = ctx.codec
        # Filters are now CLIENT-SIDE (PG multicast is unfiltered — every joiner
        # gets every record). 0/"" = keep everything.
        self._kind_filter = kind_filter
        self._node_filter = node_filter

        # A started NodeProbe backs the PgProbe (its call_addr does the supervisor
        # join CALL; its codec is shared). Any probe identity works — PG delivery
        # address is supervisor-allocated, not the probe's .art address. We
        # impersonate a log node purely to have a valid started probe.
        self._probe = ctx.probe("TraceStreamPump").start()
        self._pg = PgProbe(self._probe, node_name="trace-observer")
        # NOTE: deliberately NOT arm_decode'd — the observer owns the RICH decode
        # (_decode also cracks the inner traced message from TraceRecord.payload),
        # so PgProbe hands us the RAW record bytes (_raw) and we decode here.
        self._q: "queue.Queue[TraceRec]" = queue.Queue()

    @classmethod
    def from_log_art(cls, log_art: str | Path, *, proto_root: str | Path,
                     **kw) -> "TraceObserver":
        ctx = ArtheiaContext(str(log_art), proto_root)
        return cls(ctx, **kw)

    # ---- lifecycle --------------------------------------------------------
    def start(self, timeout: float = 3.0) -> "TraceObserver":
        """pg_join the TraceRecord group; the supervisor allocates our delivery
        address and the pump multicasts records to us. Keepalive heartbeats keep
        our membership alive (watchdog-monitored sidecar)."""
        rep = self._pg.join(_TRACE_GROUP, on_cast=self._on_rec)
        if int(rep.get("status", 1)) != 0:
            self._probe.stop()
            raise ConnectionError(
                "TraceObserver: pg_join of the TraceRecord group failed "
                f"(reply={rep})")
        self._pg.start_keepalive(period_s=1.0)
        return self

    def stop(self) -> None:
        self._pg.shutdown()       # leave the group + stop keepalive
        self._probe.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- inbound (PG recv thread) -----------------------------------------
    def _on_rec(self, fields: dict) -> None:
        # PgProbe hands us the RAW record bytes (we did not arm_decode), so our
        # rich _decode runs — it also cracks the inner traced message.
        raw = fields.get("_raw")
        if raw is None:
            return
        rec = self._decode(raw)
        if rec is not None and self._passes(rec):
            self._q.put(rec)

    def _passes(self, rec: "TraceRec") -> bool:
        if self._node_filter and rec.src != self._node_filter:
            return False
        # kind_filter is a TraceKind bitmask/ordinal; 0 = all. Best-effort match
        # by the symbolic name's enum number is overkill here — keep all unless a
        # node filter is set. (Trace KIND filtering stays server-side via the
        # supervisor's per-node Tracer config, not the observer.)
        return True

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
        (e.g. system_app_MyReply); `msg_type` is its flat nanopb type name.
        Resolve that type to its _pb2 class via the probe Codec and decode to a
        plain {field: value} dict. Returns None on empty payload or any failure
        (best-effort — a record we can't decode the body of still renders its
        envelope).

        msg_type is `<flat_package>_<Message>`; we don't know a-priori where the
        package ends, so try successively-shorter prefixes as the art package
        (system.app.My? system.app → My? …) until _message_class resolves.
        Results cache inside Codec, so the trial cost is paid once per type.
        """
        if not payload or not msg_type:
            return None
        parts = msg_type.split("_")
        # Try the longest package prefix first (most specific): for
        # 'system_app_MyReply' try 'system.app.My'→fail, 'system.app'→ok.
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
