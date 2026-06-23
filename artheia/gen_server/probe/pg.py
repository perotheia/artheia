"""PG (process-group) support for the probe — make a probe a first-class PG member.

A probe is a sidecar/ephemeral (outside the supervised tree). To participate in a
process group it must, like a C++ FC's PgClient:
  - pg_join(group)  — cast PgJoin to the supervisor (0x80020001) so broadcasters
                      deliver group's messages to THIS probe's bound address.
  - pg_watch(group) — cast PgWatch (be pushed the group's membership; for a probe
                      acting as a broadcaster).
  - pg_leave(group) — cast PgLeave.
  - keepalive       — send HeartbeatReport to the supervisor on a timer so the
                      watchdog keeps the probe's membership (joining ⇒ "monitor
                      me"; a miss evicts the probe from its groups).
  - on PgMembership — the supervisor pushes it back to the probe's address; the
                      probe decodes + caches it (members() for a broadcaster loop).

group_id = djb2_low16 of the wire message-type name (the same RemoteCodec hash the
C++ side uses), so `pg_join(group_for("system_services_log_TraceRecord"))`.

IDENTITY / MULTI-INSTANCE: a probe's PG identity = its pid (the supervisor's
registry key) + its bound (tipc_type, tipc_instance) delivery address. To run TWO
probes of the same node-type, each must bind a UNIQUE instance (the .art static
instance load-balances). Use ctx.probe(node, instance=unique_instance()) so each
gets a distinct delivery address; both then appear as distinct members and both
receive every broadcast.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from . import wire

# The supervisor's NodeReportIf receiver (HeartbeatPublisher.kSupTipc*).
SUP_TIPC_TYPE = 0x80020001
SUP_TIPC_INSTANCE = 0

_SUP_PKG = "system.supervisor"   # codec art_package for the Pg*/Heartbeat protos


def group_for(proto_type_name: str) -> int:
    """The PG group_id for a wire message type = its service_id (djb2_low16)."""
    return wire.service_id(proto_type_name)


def unique_instance() -> int:
    """A per-process-unique TIPC instance (mirrors com trace_link: getpid()&0xFFFF)
    so two probes of the same node-type bind distinct delivery addresses."""
    return os.getpid() & 0xFFFF


class PgProbe:
    """Wraps a started NodeProbe to give it PG membership + a supervisor keepalive.

    The NodeProbe owns the bound TIPC server (the delivery address) + the inbox
    machinery; PgProbe adds the supervisor-side casts + the membership cache.
    """

    def __init__(self, probe, node_name: str = "probe"):
        self._probe = probe                 # a started NodeProbe (binds rx addr)
        self._node_name = node_name
        self._pid = os.getpid()
        self._rx_type = probe.me.tipc_type
        self._rx_inst = probe.me.tipc_instance
        self._members: dict[int, list[tuple[int, int]]] = {}   # group_id → [(t,i)]
        self._members_lock = threading.Lock()
        self._seq = 0
        self._ka_stop: Optional[threading.Event] = None
        self._ka_thread: Optional[threading.Thread] = None
        # Arm reception of PgMembership pushes (the supervisor casts them to us):
        # make the non-port type decodable, then register the cache handler.
        self._probe.arm_known("system.supervisor",
                              "system_supervisor_PgMembership")
        self._probe.on_cast_known("system_supervisor_PgMembership",
                                  self._on_membership)

    # ---- supervisor casts -------------------------------------------------
    def _sup_cast(self, proto_type: str, **fields) -> None:
        """Encode a Pg*/Heartbeat message + cast it to the supervisor address."""
        self._probe.cast_addr(SUP_TIPC_TYPE, SUP_TIPC_INSTANCE,
                              _SUP_PKG, proto_type, **fields)

    def join(self, group_id: int) -> None:
        self._sup_cast("system_supervisor_PgJoin",
                       node_name=self._node_name, pid=self._pid,
                       group_id=group_id,
                       tipc_type=self._rx_type, tipc_instance=self._rx_inst)

    def watch(self, group_id: int) -> None:
        self._sup_cast("system_supervisor_PgWatch",
                       node_name=self._node_name, pid=self._pid,
                       group_id=group_id,
                       tipc_type=self._rx_type, tipc_instance=self._rx_inst)

    def leave(self, group_id: int) -> None:
        self._sup_cast("system_supervisor_PgLeave",
                       node_name=self._node_name, pid=self._pid,
                       group_id=group_id)

    # ---- keepalive (so the watchdog keeps our membership) -----------------
    def start_keepalive(self, period_s: float = 1.0) -> None:
        """Beat HeartbeatReport to the supervisor every period_s. Joining a PG
        ⇒ the supervisor monitors us; without a beat the watchdog (3s) evicts us.
        A probe is a sidecar — this is its membership-keepalive."""
        if self._ka_thread:
            return
        self._ka_stop = threading.Event()

        def _beat():
            while not self._ka_stop.is_set():
                self._seq += 1
                try:
                    self._sup_cast("system_supervisor_HeartbeatReport",
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

    # ---- membership cache -------------------------------------------------
    def _on_membership(self, d: dict) -> None:
        gid = int(d.get("group_id", 0))
        members = [(int(m["tipc_type"]), int(m["tipc_instance"]))
                   for m in d.get("members", [])]
        with self._members_lock:
            self._members[gid] = members

    def members(self, group_id: int) -> list[tuple[int, int]]:
        with self._members_lock:
            return list(self._members.get(group_id, []))

    def await_membership(self, group_id: int, want: int = 1,
                         timeout: float = 5.0) -> list[tuple[int, int]]:
        """Block until group_id has >= `want` members (or timeout). Returns the
        member list. Use to assert a join/leave/restart landed."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.members(group_id)
            if len(m) >= want:
                return m
            time.sleep(0.05)
        return self.members(group_id)
