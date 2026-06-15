"""artheia.observer — subscribe to the log[trace] firehose, yield decoded records.

The OTP dbg:tracer shape for Theia. See TraceObserver. All internal TIPC, no
gRPC; decode via libprotobuf (the probe Codec) + JSON. Collector addresses are
resolved from the parsed log .art (generic).
"""
from .observer import TraceObserver, TraceRec, SUBSCRIBER_TYPE
from .log_observer import LogObserver, LogRec, LOG_SUBSCRIBER_TYPE, LEVEL_CODES

__all__ = [
    "TraceObserver", "TraceRec", "SUBSCRIBER_TYPE",
    "LogObserver", "LogRec", "LOG_SUBSCRIBER_TYPE", "LEVEL_CODES",
]
