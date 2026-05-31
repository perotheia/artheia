"""artheia.observer — subscribe to the log[trace] firehose, yield decoded records.

The OTP dbg:tracer shape for Theia. See TraceObserver. All internal TIPC, no
gRPC; decode via libprotobuf (the probe Codec) + JSON. Collector addresses are
resolved from the parsed log .art (generic).
"""
from .observer import TraceObserver, TraceRec, SUBSCRIBER_TYPE

__all__ = ["TraceObserver", "TraceRec", "SUBSCRIBER_TYPE"]
