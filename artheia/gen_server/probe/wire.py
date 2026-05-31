"""TheiaMsgHeader pack/unpack + service_id hash — the on-wire contract.

Mirrors platform/runtime/include/TheiaMsgHeader.hh (24-byte packed, LE) and
RemoteCodec.hh's djb2_low16. A probe and a C++ FC meet on exactly these bytes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ---- constants (TheiaMsgHeader.hh) ----------------------------------------
BUS_TYPE_RPC = 0x02
MSG_GEN_CAST = 0x20
MSG_GEN_CALL = 0x21
MSG_GEN_CALL_REPLY = 0x22

# bus_type, msg_type, proto_len, timestamp_ns, service_id, method_id,
# correlation_id, seq_num, reserved[2] — packed, little-endian.
_HDR = struct.Struct("<BBHQHHIH2s")
HEADER_SIZE = _HDR.size  # 24
assert HEADER_SIZE == 24, f"header must be 24 bytes, got {HEADER_SIZE}"

# TipcClient::send_frame caps payload at 256 bytes today (NodeRef.cc).
MAX_PAYLOAD = 256


def service_id(proto_type_name: str) -> int:
    """djb2_low16 of the nanopb type name (e.g. 'system_demo_Inc').

    Matches RemoteCodec.hh hash_msg_type_ exactly: sender and receiver MUST
    agree on this for the FC's dispatch table to find the handler.
    """
    h = 5381
    for b in proto_type_name.encode("utf-8"):
        h = (h * 33 + b) & 0xFFFFFFFF
    return h & 0xFFFF


@dataclass
class Header:
    msg_type: int
    proto_len: int
    service_id: int
    correlation_id: int = 0
    method_id: int = 0
    timestamp_ns: int = 0
    seq_num: int = 0

    def pack(self) -> bytes:
        return _HDR.pack(
            BUS_TYPE_RPC,
            self.msg_type,
            self.proto_len,
            self.timestamp_ns,
            self.service_id,
            self.method_id,
            self.correlation_id,
            self.seq_num,
            b"\x00\x00",
        )

    @classmethod
    def unpack(cls, buf: bytes) -> "Header":
        (bus, msg_type, proto_len, ts, svc, method, corr, seq, _resv) = \
            _HDR.unpack(buf[:HEADER_SIZE])
        if bus != BUS_TYPE_RPC:
            raise ValueError(f"not an RPC frame (bus_type={bus:#x})")
        return cls(
            msg_type=msg_type,
            proto_len=proto_len,
            service_id=svc,
            correlation_id=corr,
            method_id=method,
            timestamp_ns=ts,
            seq_num=seq,
        )


def frame(hdr: Header, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"payload {len(payload)}B exceeds runtime cap {MAX_PAYLOAD}B "
            "(TipcClient::send_frame)"
        )
    hdr.proto_len = len(payload)
    return hdr.pack() + payload
