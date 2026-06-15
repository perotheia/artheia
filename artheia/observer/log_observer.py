"""LogObserver — subscribe to the log[logging] firehose, yield decoded log lines.

The LOG analogue of TraceObserver (observer.py). Where TraceObserver follows the
dispatcher EXECUTION records, this follows the node LOG LINES that LogStreamPump
tails off each node's sink and re-hoses. Same shape: bind a subscriber TIPC
address, call LogDaemon.Subscribe, receive the LogRecords the pump fans out,
each decoded (libprotobuf via the probe Codec) into a LogRec.

    from artheia.observer import LogObserver
    obs = LogObserver.from_log_art("services/log/system/log/component.art",
                                   proto_root="platform/proto")
    obs.start()                       # bind + Subscribe (spins up the tailer)
    for rec in obs.records(timeout=5):# stream decoded LogRecords
        print(rec.node, rec.level, rec.line)
    obs.stop()                        # unsubscribe (winds the tailer down)

The adb-style <tag-glob>:<level> filter is applied subscriber-side by the caller
(tdb/rtdb) — the hose stays dumb. This observer just decodes + yields.
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

_LOG_PKG = "system.services.log"
_RECORD = "system_services_log_LogRecord"
_SUBSCRIBE = "system_services_log_LogSubscribeReq"

# Observer subscriber service type for the LOG stream — distinct from the trace
# observer's SUBSCRIBER_TYPE (0x8001001A) so the two streams don't collide on
# one process. Distinct instance per observer (pid-derived).
LOG_SUBSCRIBER_TYPE = 0x8001001B

# LogLevel ordinals (system_services_log.LogLevel) ↔ single-letter codes, the
# adb vocabulary the filter DSL uses. Index = ordinal.
LEVEL_CODES = ["V", "D", "I", "W", "E", "F"]


@dataclass
class LogRec:
    """One decoded log line: the parsed header fields + the verbatim text."""
    node: str           # emitting FC node (= supervised worker name)
    tag: str            # the app/syslog tag the line carries
    level: str          # LogLevel enum NAME (e.g. "INFO"); "" if unset
    level_ord: int      # the ordinal (0=VERBOSE … 5=FATAL) for >= filtering
    ts_ns: int          # wall time the line was emitted (epoch ns); 0 if unknown
    line: str           # the verbatim log text (no trailing newline)

    @property
    def level_code(self) -> str:
        """Single-letter level code (V/D/I/W/E/F) for the adb-style render."""
        return LEVEL_CODES[self.level_ord] if 0 <= self.level_ord < len(LEVEL_CODES) else "?"

    def to_dict(self, *, ts: "Optional[str]" = None) -> dict:
        out: dict = {}
        if ts is not None:
            out["ts"] = ts
        else:
            out["ts_ns"] = self.ts_ns
        out.update({
            "node": self.node,
            "tag": self.tag,
            "level": self.level,
            "line": self.line,
        })
        return out


class LogObserver:
    def __init__(self, ctx: ArtheiaContext, *,
                 subscriber_type: int = LOG_SUBSCRIBER_TYPE,
                 level_min: int = 0, tag_filter: str = ""):
        self.ctx = ctx
        self.codec: Codec = ctx.codec
        self._sub_type = subscriber_type
        self._sub_instance = os.getpid() & 0xFFFF
        self._level_min = level_min
        self._tag_filter = tag_filter

        # LogDaemon endpoint + the Subscribe op resolved from the .art (generic).
        self._ctl = ctx.ref("LogDaemon")
        self._sub_op = self._ctl.find_op("Subscribe")

        self._server: Optional[TipcServer] = None
        self._q: "queue.Queue[LogRec]" = queue.Queue()
        self._record_sid = wire.service_id(_RECORD)
        self._corr = 0
        self._lock = threading.Lock()

    @classmethod
    def from_log_art(cls, log_art: str | Path, *, proto_root: str | Path,
                     **kw) -> "LogObserver":
        ctx = ArtheiaContext(str(log_art), proto_root)
        return cls(ctx, **kw)

    # ---- lifecycle --------------------------------------------------------
    def start(self, timeout: float = 3.0) -> "LogObserver":
        """Bind the subscriber socket, then Subscribe to LogDaemon."""
        self._server = TipcServer(self._sub_type, self._sub_instance,
                                  self._on_frame)
        self._server.start()
        if not self._subscribe(timeout):
            self._server.stop()
            raise ConnectionError("LogObserver: Subscribe to LogDaemon failed")
        return self

    def _subscribe(self, timeout: float) -> bool:
        req = self.codec.encode(
            _LOG_PKG, self._sub_op.request.proto_type,
            sub_type=self._sub_type, sub_instance=self._sub_instance,
            level_min=self._level_min, tag_filter=self._tag_filter)
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
        reply = ctl.recv_reply(timeout=timeout)   # LogEmpty ack
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

    def _decode(self, payload: bytes) -> Optional[LogRec]:
        cls = self.codec._message_class(_LOG_PKG, _RECORD)  # cached _pb2 class
        msg = cls()
        msg.ParseFromString(payload)
        # level is the LogLevel enum (field 3) — resolve its symbolic name.
        # gen-proto prefixes enum members with the enum name (nanopb compat),
        # so values_by_number gives "LogLevel_INFO" — strip the prefix.
        try:
            level_name = msg.DESCRIPTOR.fields_by_name["level"].enum_type \
                .values_by_number[msg.level].name
            if level_name.startswith("LogLevel_"):
                level_name = level_name[len("LogLevel_"):]
        except Exception:
            level_name = str(getattr(msg, "level", "") or "")
        return LogRec(
            node=msg.node, tag=msg.tag,
            level=level_name, level_ord=int(getattr(msg, "level", 0) or 0),
            ts_ns=int(getattr(msg, "ts_ns", 0) or 0),
            line=msg.line,
        )

    # ---- record stream ----------------------------------------------------
    def records(self, timeout: float = 5.0) -> Iterator[LogRec]:
        """Yield decoded log records as they arrive, until `timeout` of silence."""
        while True:
            try:
                yield self._q.get(timeout=timeout)
            except queue.Empty:
                return

    def next_record(self, timeout: float = 5.0) -> Optional[LogRec]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None
