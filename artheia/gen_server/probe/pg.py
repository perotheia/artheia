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
  - broadcast(group_name, art_pkg, proto_type, **fields)
                      — resolve the group_type (CALL with join=false), encode the
                        message, and SOCK_RDM-sendto the name-sequence
                        {group_type, 0..~0} → multicast to all members.
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
from .transport import TipcDgramServer, tipc_multicast_send

# The supervisor's SupervisorControlIf receiver (PgClient::kSupTipc*).
SUP_TIPC_TYPE = 0x80020001
SUP_TIPC_INSTANCE = 0

_SUP_PKG = "system.supervisor"   # codec art_package for the Pg*/Heartbeat protos


class PgProbe:
    """Wraps a started NodeProbe to give it PG membership over TIPC multicast.

    The NodeProbe owns the supervisor CALL channel (call_addr) + the codec;
    PgProbe adds the per-group recv socket + the broadcast send + a keepalive.
    """

    def __init__(self, probe, node_name: str = "probe"):
        self._probe = probe                 # a started NodeProbe
        self._node_name = node_name
        self._pid = os.getpid()
        # joined groups: group_name -> {"type", "instance", "server"}
        self._groups: dict[str, dict] = {}
        # broadcaster's resolved group_type cache: group_name -> type
        self._resolved: dict[str, int] = {}
        # per-group received-cast handlers: group_name -> callable(fields)
        self._handlers: dict[str, Callable[[dict], None]] = {}
        # per-group inbox of decoded casts (for await_broadcast)
        self._inboxes: dict[str, list] = {}
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
        to on_cast (and into an inbox for await_broadcast). Returns the reply."""
        rep = self._sup_join_call(group_name, join=True)
        if int(rep.get("status", 1)) != 0:
            return rep
        gtype = int(rep.get("group_type", 0))
        ginst = int(rep.get("instance", 0))
        self._resolved[group_name] = gtype
        if on_cast:
            self._handlers[group_name] = on_cast
        self._inboxes.setdefault(group_name, [])

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

    # ---- broadcast --------------------------------------------------------
    def resolve(self, group_name: str) -> int:
        """Learn (allocate, if new) the group's TIPC type WITHOUT taking an
        instance — for a pure broadcaster. Cached."""
        gt = self._resolved.get(group_name)
        if gt:
            return gt
        rep = self._sup_join_call(group_name, join=False)
        gt = int(rep.get("group_type", 0))
        if gt:
            self._resolved[group_name] = gt
        return gt

    def broadcast(self, group_name: str, art_package: str, proto_type: str,
                  **fields) -> None:
        """Multicast `proto_type` to the WHOLE group `group_name`: encode, then
        one name-sequence datagram → every bound member's handle_cast. The
        group_name MUST be the wire type name the members register_cast on; the
        cast's service_id keys on proto_type so members demux it identically."""
        gtype = self.resolve(group_name)
        if not gtype:
            return
        payload = self._probe.ctx.codec.encode(art_package, proto_type, **fields)
        hdr = wire.Header(
            msg_type=wire.MSG_GEN_CAST,
            proto_len=len(payload),
            service_id=wire.service_id(proto_type),
            correlation_id=0,
            timestamp_ns=time.time_ns(),
        )
        tipc_multicast_send(gtype, wire.frame(hdr, payload))

    # ---- received-frame dispatch ------------------------------------------
    def _on_group_frame(self, group_name: str, hdr: wire.Header,
                        payload: bytes) -> None:
        # Decode via the group_name = the wire proto type. The codec needs the
        # defining package; the caller registered it at join() via the handler's
        # closure, so decode against the supervisor pkg is wrong here — instead
        # decode is deferred to the handler when it knows the type. We decode
        # eagerly when we can resolve the type from the known-msgs registry.
        m = self._probe._known_msgs.get(hdr.service_id)
        if m is not None:
            fields = self._probe.ctx.codec.decode(
                m.art_package, m.proto_type, payload)
        else:
            fields = {"_raw": payload, "_service_id": hdr.service_id}
        with self._lock:
            self._inboxes.setdefault(group_name, []).append(fields)
        h = self._handlers.get(group_name)
        if h:
            h(fields)

    def arm_decode(self, group_name: str, art_package: str) -> None:
        """Make group `group_name`'s wire type decodable on receipt — register it
        in the probe's known-msgs by service_id (group_name IS the proto type)."""
        self._probe.arm_known(art_package, group_name)

    def await_broadcast(self, group_name: str, want: int = 1,
                        timeout: float = 5.0) -> list:
        """Block until >= `want` casts have arrived on `group_name` (or timeout).
        Returns the decoded casts. Use to assert a multicast landed on N members."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                got = list(self._inboxes.get(group_name, []))
            if len(got) >= want:
                return got
            time.sleep(0.05)
        with self._lock:
            return list(self._inboxes.get(group_name, []))

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
