"""artheia.gen_server.probe — mock a Theia node over TIPC to test FCs in isolation.

Given a parsed .art, an ArtheiaContext exposes every node as a named RemoteRef;
a NodeProbe impersonates one node (binds its TIPC address) and supports every
gen_server operation (cast / call active; on_cast / on_call / expect_cast
passive) on the real wire. Several probes surround one FC to exercise it.

    from artheia.gen_server.probe import ArtheiaContext
    ctx = ArtheiaContext("system/app/component.art", proto_root="platform/proto")
    other_node = ctx.probe("OtherNode").start()
    other_node.cast("MyNode", "MyMsg", n=5)
    assert other_node.call("MyNode", "MyReply")["value"] == 5
"""
from .context import ArtheiaContext, RemoteRef, MsgRef, OpRef, PortRef
from .node import NodeProbe
from . import wire

__all__ = [
    "ArtheiaContext", "RemoteRef", "MsgRef", "OpRef", "PortRef",
    "NodeProbe", "wire",
]
