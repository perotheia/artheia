"""PG (process-group) support for the probe — make a probe a first-class PG member.

A probe is a sidecar/ephemeral (outside the supervised tree). To participate in a
process group it must, like a C++ FC's PgClient, follow the TIPC name-sequence
MULTICAST model where the SUPERVISOR is the namespace authority:

  - join(group_name)  — CALL PgJoinReq to the supervisor (0x80020001); it
                        allocates the group's TIPC TYPE (once per name) + a UNIQUE
                        INSTANCE and returns {group_type, instance}. The probe
                        then BINDS a SOCK_RDM recv socket at {group_type,
                        instance}; a broadcaster's one name-sequence datagram is
                        delivered to it (and every other member) by the kernel.
  - leave(group_name) — CALL PgLeaveReq; the supervisor frees the instance.
  - keepalive         — beat HeartbeatReport to the supervisor on a timer so its
                        watchdog keeps the probe's membership (joining ⇒
                        "monitor me"; a miss evicts the probe from its groups).

GROUP IDENTITY is the wire MESSAGE-TYPE NAME (the same well-known .art-derived
name the C++ side passes as msg_type_name<T>()) — NOT a hash, NOT a free-form
string. e.g. join("system_services_log_TraceRecord"). No collision, no slippage.

IDENTITY / MULTI-INSTANCE: the probe takes NO part in choosing its instance — the
supervisor allocates a unique one per join CALL. To run TWO probes that both
receive a group's broadcasts, each simply join()s; the supervisor hands each a
distinct instance, so both bind distinct addresses under the shared group_type and
both receive every name-sequence datagram. (No pid identity, no .art-static
instance juggling.)
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from . import wire
from .transport import TipcDgramServer

# The supervisor's SupervisorControlIf receiver (PgClient::kSupTipc*).
SUP_TIPC_TYPE = 0x80020001
SUP_TIPC_INSTANCE = 0

_SUP_PKG = "system.supervisor"   # codec art_package for the Pg*/Heartbeat protos


class PgProbe:
    """Wraps a started NodeProbe to give it PG membership over TIPC multicast.

    The NodeProbe owns the supervisor CALL channel (call_addr) + the codec;
    PgProbe adds the per-group recv socket + a keepalive. (A speculative
    producer-side surface — broadcast/watch/members — was removed in the
    2026-07 dead-code sweep; the probe is a RECEIVER. git history has it.)
    """

    def __init__(self, probe, node_name: str = "probe"):
        self._probe = probe                 # a started NodeProbe
        self._node_name = node_name
        self._pid = os.getpid()
        # joined groups: group_name -> {"type", "instance", "server"}
        self._groups: dict[str, dict] = {}
        # per-group received-cast handlers: group_name -> callable(fields)
        self._handlers: dict[str, Callable[[dict], None]] = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._ka_stop: Optional[threading.Event] = None
        self._ka_thread: Optional[threading.Thread] = None

    # ---- supervisor CALLs -------------------------------------------------
    def _sup_join_call(self, group_name: str, join: bool) -> dict:
        """CALL PgJoinReq → PgJoinReply{status, group_type, instance}."""
        return self._probe.call_addr(
            SUP_TIPC_TYPE, SUP_TIPC_INSTANCE, _SUP_PKG,
            "system_supervisor_PgJoinReq", "system_supervisor_PgJoinReply",
            2.0,
            node_name=self._node_name, pid=self._pid,
            group_name=group_name, join=join)

    # ---- join / leave -----------------------------------------------------
    def join(self, group_name: str,
             on_cast: Optional[Callable[[dict], None]] = None) -> dict:
        """Join the group for `group_name`. The supervisor allocates {group_type,
        instance}; bind a SOCK_RDM recv socket there and dispatch received casts
        to on_cast. Returns the reply."""
        rep = self._sup_join_call(group_name, join=True)
        if int(rep.get("status", 1)) != 0:
            return rep
        gtype = int(rep.get("group_type", 0))
        ginst = int(rep.get("instance", 0))
        if on_cast:
            self._handlers[group_name] = on_cast

        def _frame(hdr: wire.Header, payload: bytes) -> None:
            self._on_group_frame(group_name, hdr, payload)

        srv = TipcDgramServer(gtype, ginst, _frame)
        srv.start()
        with self._lock:
            self._groups[group_name] = {
                "type": gtype, "instance": ginst, "server": srv}
        return rep

    def leave(self, group_name: str) -> None:
        with self._lock:
            g = self._groups.pop(group_name, None)
        if not g:
            return
        g["server"].stop()
        try:
            self._probe.call_addr(
                SUP_TIPC_TYPE, SUP_TIPC_INSTANCE, _SUP_PKG,
                "system_supervisor_PgLeaveReq", "system_supervisor_ControlReply",
                2.0,
                node_name=self._node_name, group_name=group_name,
                group_type=g["type"], instance=g["instance"])
        except Exception:
            pass   # best-effort; supervisor may be down

    # ---- received-frame dispatch ------------------------------------------
    def _on_group_frame(self, group_name: str, hdr: wire.Header,
                        payload: bytes) -> None:
        # Decode via the group_name = the wire proto type. The codec needs the
        # defining package; the caller registered it at join() via the handler's
        # closure. We decode eagerly when we can resolve the type from the
        # known-msgs registry; else hand the handler the raw payload.
        m = self._probe._known_msgs.get(hdr.service_id)
        if m is not None:
            fields = self._probe.ctx.codec.decode(
                m.art_package, m.proto_type, payload)
        else:
            fields = {"_raw": payload, "_service_id": hdr.service_id}
        h = self._handlers.get(group_name)
        if h:
            h(fields)

    # ---- keepalive (so the watchdog keeps our membership) -----------------
    def start_keepalive(self, period_s: float = 1.0) -> None:
        """Beat HeartbeatReport to the supervisor every period_s. Joining a PG
        ⇒ the supervisor monitors us; without a beat the watchdog (3s) evicts us
        and frees our instances. A probe is a sidecar — this is its keepalive."""
        if self._ka_thread:
            return
        self._ka_stop = threading.Event()

        def _beat():
            while not self._ka_stop.is_set():
                self._seq += 1
                try:
                    self._probe.cast_addr(
                        SUP_TIPC_TYPE, SUP_TIPC_INSTANCE, _SUP_PKG,
                        "system_supervisor_HeartbeatReport",
                        node_name=self._node_name, pid=self._pid,
                        seq=self._seq, monotonic_ns=time.monotonic_ns())
                except Exception:
                    pass   # best-effort; supervisor restarting / not yet up
                self._ka_stop.wait(period_s)

        self._ka_thread = threading.Thread(target=_beat, daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        if self._ka_stop:
            self._ka_stop.set()
        if self._ka_thread:
            self._ka_thread.join(timeout=2.0)
        self._ka_thread = None

    def shutdown(self) -> None:
        self.stop_keepalive()
        for name in list(self._groups.keys()):
            self.leave(name)
