# The Artheia Language Manual

Artheia is a small DSL for describing the host side of a vehicle compute
stack: how host components publish and consume messages, how they are
addressed on TIPC, what runtime parameters they expose to operations, and
which of them terminate signals coming from the classical-AUTOSAR bus
through the gateway.

It exists because we like writing systems in a language, not in XML, and
because every alternative we looked at either disappeared (ARText) or
expected us to live inside Eclipse. Artheia takes ARText's syntactic
aesthetic — `package`, `component atomic`, `ports { sender X provides
If }`, `composition`, `connect a.x to b.y` — and rewires it for a host
runtime that talks to the gateway over AF_TIPC and lets system engineering
push parameter values through etcd.

This manual is the single document you should need. It explains the
philosophy, walks the grammar construct by construct, describes what every
generator emits, and ends with a worked example.

---

## Table of contents

1. [Philosophy and scope](#1-philosophy-and-scope)
2. [Files, packages, imports](#2-files-packages-imports)
3. [Messages — the wire layer](#3-messages--the-wire-layer)
4. [Interfaces — what a port speaks](#4-interfaces--what-a-port-speaks)
5. [Nodes — components on the host](#5-nodes--components-on-the-host)
6. [Ports — how nodes connect](#6-ports--how-nodes-connect)
7. [Params — runtime config from etcd](#7-params--runtime-config-from-etcd)
8. [Compositions — wiring the graph](#8-compositions--wiring-the-graph)
9. [Buses and gateway routes](#9-buses-and-gateway-routes)
10. [Importing DBC + FIBEX for the gateway catalog](#10-importing-dbc--fibex-for-the-gateway-catalog)
11. [The generators](#11-the-generators)
12. [Worked example](#12-worked-example)
13. [The CLI](#13-the-cli)
14. [Editor support](#14-editor-support)
15. [Out-of-scope, on purpose](#15-out-of-scope-on-purpose)

---

## 1. Philosophy and scope

Artheia is **not** an AUTOSAR tool. It does not round-trip ARXML, does not
implement the AUTOSAR metamodel, and is not partner-portal-locked. It is
inspired by ARText (BMW Car IT, hibernating since ~2011), borrowing its
syntax and a few of its structural concepts (atomic components, ports,
prototypes, compositions, connectors). What goes in or out of the language
is decided by what the host runtime actually needs.

Two coupled jobs:

1. **Describe what the host runs.** A model with named messages, typed
   interfaces, nodes wearing TIPC addresses, ports that talk those
   interfaces, and compositions that wire them.
2. **Describe how the host meets the classical bus.** Gateway routes
   attaching CAN IDs and FlexRay slots to particular nodes, plus a
   generated stub of every gateway-side message — so when you reference
   `ACC_07` from a port, completion finds it and the netgraph fills the
   routing for you.

Once a model is written, the toolchain emits:

| Output | What it's for |
|---|---|
| `.proto` files (one per `message`) | nanopb-friendly wire definitions, drop-in compatible with the gateway's existing codec pipeline. |
| `netgraph.json` | who talks to whom over TIPC; per-node CAN/FlexRay routes; resolved against the gateway catalog. |
| `etcd_schema.json` | a flat seed document of every node's params with defaults; system engineering edits this before install. |
| `<Node>_gen.h` / `_gen.py` | callback-style stubs. You implement `on_*` callbacks; you call `send_*` helpers; runtime owns the dispatch loop. |

The toolchain is one Python package (`artheia`) plus a VS Code extension
that hosts a real LSP. Everything runs on stock CPython 3.10+; no JVM, no
Eclipse, no AUTOSAR partner login.

> **A note on names.** The DSL was named after the AUTOSAR-textual tradition
> it pays homage to. The system it targets is the *Theia* gateway. Don't
> confuse `~/repo/artheia/` (this language) with `~/repo/theia/` (the
> gateway).

---

## 2. Files, packages, imports

An Artheia source file has the extension `.art`. The skeleton is:

```artheia
package my.demo

import gateway.signals.*

// declarations follow
```

The `package` line is optional but recommended; it groups the file's
contents and is used by code generators to namespace output (the `.proto`
`package` line, for example).

The `import` form is parsed but not yet resolved — for v0.1 it is a
documentation hint, used by the LSP for completion. Treat the LSP's
workspace as the import scope: every `.art` in your workspace contributes
to the symbol pool.

Comments are C-style: `//` line, `/* … */` block.

---

## 3. Messages — the wire layer

A `message` is a proto3-equivalent value type. It declares fields with a
proto3 primitive type or a reference to another message, plus a field
number (proto3 tag).

```artheia
message SpeedSignal {
    uint32 speed_kph = 1
    uint64 ts_ns     = 2
}

message Fix {
    double lat   = 1
    double lon   = 2
    repeated string tags = 3
}
```

**Primitive types** track proto3 exactly:
`int32`, `int64`, `uint32`, `uint64`, `sint32`, `sint64`,
`fixed32`, `fixed64`, `sfixed32`, `sfixed64`,
`float`, `double`, `bool`, `string`, `bytes`.

**References:** any field can use another `message` as its type. Artheia
resolves the reference and the proto generator emits an `import
"Other.proto";` for you.

```artheia
message Composite {
    SpeedSignal speed = 1
    repeated Fix history = 2
}
```

**Repeated:** prefix the type with `repeated`. Same semantics as proto3.

**Rules:** field numbers must be positive and unique within a message.
Both are checked at parse time.

---

## 4. Interfaces — what a port speaks

An interface is a contract a port presents to the rest of the system.
There are two kinds, each mapping cleanly to AUTOSAR's sender/receiver and
client/server concepts.

### Sender/receiver

```artheia
interface senderReceiver SpeedIf {
    data SpeedSignal speed
}
```

A senderReceiver interface owns one or more `data` elements, each of which
is a `name : type` pair (the type is a reference to a `message`). A
sender port pushes one of these data elements; a receiver port observes
them.

### Client/server

```artheia
interface clientServer StatusIf {
    operation GetStatus() returns StatusReport
    operation SetMode(in mode: ModeRequest)
    operation Echo(in arg: Payload, inout state: State)
}
```

A clientServer interface declares `operation`s: a name, an optional
parameter list (each parameter annotated `in` / `out` / `inout` plus a
message type), and an optional `returns` clause naming a single message.

Servers implement operations; clients call them.

---

## 5. Nodes — components on the host

A node is an atomic host component. It has a TIPC address (always
mandatory), optional ports, and optional params.

```artheia
node atomic SpeedPublisher {
    tipc type=0x80010001 instance=0
    ports {
        sender out provides SpeedIf
        server status provides StatusIf
    }
    params {
        publish_period_ms : uint32 = 10
        enabled           : bool   = true
        source_name       : string = "front-axle"
    }
}
```

`atomic` is the only node kind in v0.1 (no composite nodes — compositions
do the wiring, see §8).

### TIPC addressing

`tipc type=<u32> instance=<u32>` directly maps to the AF_TIPC SOCK_SEQPACKET
addressing the host runtime uses. The numbers feed `GwMessageHeader.tipc`
(see `theia/.../gw_proto.h`).

Two invariants are enforced at parse time:

- Every node's `(type, instance)` is unique.
- The reserved pair `type=0x80010000, instance=0` is rejected — that's
  the gateway itself (`TIPC_GW_TYPE` / `TIPC_GW_INSTANCE` in
  `gw_proto.h`). Pick a higher `type` value for your nodes.

By convention, Artheia hosts pick TIPC types in `0x80010001` and up.

---

## 6. Ports — how nodes connect

A port has a name, a direction implied by its keyword, and an interface
reference.

| Keyword | Meaning | Family |
|---|---|---|
| `sender X provides If` | outgoing data on senderReceiver `If` | senderReceiver |
| `receiver X requires If` | incoming data on senderReceiver `If` | senderReceiver |
| `server X provides If` | implements operations of clientServer `If` | clientServer |
| `client X requires If` | calls operations of clientServer `If` | clientServer |

Compatibility rules (checked when you write a `connect` — see §8):

- A connect must wire one provider (`sender` / `server`) to one requirer
  (`receiver` / `client`). Two senders or two receivers is an error.
- Both endpoints must use the same interface kind family. You can't
  connect a sender to a client.
- Both endpoints must reference the same interface declaration —
  structurally-equivalent but distinct interfaces are not the same
  interface.

---

## 7. Params — runtime config from etcd

A node may declare a `params` block of typed runtime parameters with
defaults:

```artheia
node atomic TorqueController {
    tipc type=0x80010002 instance=0
    params {
        gain        : float  = 1.25
        max_torque  : uint32 = 250
        enabled     : bool   = true
        log_target  : string = "stdout"
    }
}
```

Allowed types: `int32`, `int64`, `uint32`, `uint64`, `float`, `double`,
`bool`, `string`. Defaults are range-checked at parse time:

- `uintN` defaults must be `0 ≤ x < 2^N`.
- `intN` defaults must be `-2^(N-1) ≤ x < 2^(N-1)`.
- `bool` defaults must be `true` or `false` (not 0/1).
- `string` defaults must be quoted strings.

Param names must be unique within a node.

### How params reach the runtime

The `gen-etcd` generator collects every node's params and emits a single
seed document keyed by `/nodes/<NodeName>/params/<name>`:

```json
{
  "/nodes/TorqueController/params/gain": {
    "type": "float", "python_type": "float", "default": 1.25
  }
}
```

Operations / system engineering edit this document *before install* to
override defaults for each deployment (think OTA: ship the schema with the
package, customer-specific values get spliced in at install time).

At runtime, the host framework watches each prefix; when a value changes,
it calls the generated `on_param_<name>(new_value)` callback in your node
stub. There are no migrations — etcd's model is "values, mutable in
place" and the DSL never describes versioned schemas.

If a node has no params, the generator emits an empty schema for it. Adding
or removing params later is a normal code change.

---

## 8. Compositions — wiring the graph

A composition is the description of *which* nodes exist in a deployment
and *how* their ports are wired.

```artheia
composition VehicleSystem {
    prototype SpeedPublisher    speed_pub
    prototype TorqueController  torque_ctrl
    prototype Actuator          actuator

    connect speed_pub.out         to torque_ctrl.speed_in
    connect torque_ctrl.torque_out to actuator.torque_in
    connect torque_ctrl.status_query to speed_pub.status
}
```

- `prototype <NodeType> <instance_name>` instantiates a node type. The
  same node type can be prototyped multiple times in one composition.
- `connect <prototype>.<port> to <prototype>.<port>` wires two ports.
  Direction and interface rules from §6 apply.

The composition is the *only* thing that makes a port connection real.
Listing ports inside a node without a connect leaves them unwired.

The netgraph generator walks compositions to fill in the runtime routing
table.

---

## 9. Buses and gateway routes

Artheia nodes may terminate signals coming off the classical-AUTOSAR
buses. The `gateway_route` construct attaches a node to a CAN ID or
FlexRay slot.

### Bus identifiers

A bus name in the DSL maps to a value of `GwBusId` (see
`theia/.../gw_bus_types.h`). The CAN buses are `diagcan`, `dcan`, `hcan`,
`ican`, `k2can`, `kcan`, `komfortcan`, `subcan`; FlexRay channels appear
as `mlbevo_gen2_a`, `mlbevo_gen2_b`. The list is parsed live from
`gw_bus_types.h` if Artheia can find it (default: `~/repo/theia/...`;
override with `$ARTHEIA_GW_BUS_TYPES_H`), and falls back to a hardcoded
snapshot otherwise.

You can also declare your own bus inline:

```artheia
bus test_can      kind=can
bus custom_fr     kind=flexray channels=[A, B]
```

A declared bus shadows the well-known one of the same name.

### CAN routes

```artheia
node atomic SpeedFromCar { tipc type=0x80010010 instance=0 }

gateway_route SpeedFromCar {
    can id=0x42 bus=kcan dlc=8
    direction=in
}
```

Field names mirror `GwCanMeta` from `gw_proto.h` so the netgraph drops
into the gateway runtime without renaming: `id`, `bus`, `channel_idx`,
`dlc`, `extended_id`, `rtr`.

### FlexRay routes

```artheia
gateway_route TorqueOut {
    flexray slot=15 bus=mlbevo_gen2_a channel=A cycle=0 pdu_offset=4
    direction=out
}
```

Field names mirror `GwFlexRayMeta`: `slot`, `bus`, `channel` (`A` or
`B`), `cycle`, `pdu_offset`.

### Signal-named routes (preferred)

When you've imported the DBC/FIBEX signal catalog (see §10), the cleanest
form is to reference the message symbol and let the importer's catalog
fill in the bus and address:

```artheia
gateway_route SpeedFromCar {
    signal = ACC_07
    direction = in
}
```

The netgraph generator looks up `ACC_07` in the catalog and produces the
same JSON as if you had written the explicit `can id=… bus=…` form. If
the catalog is missing or doesn't contain the message, the netgraph marks
the route `"unresolved": true` and the LSP flags it.

### Direction

`direction = in` means signals flow *from the gateway* into the node
(host receives). `direction = out` means the node injects onto the bus
through the gateway (host transmits). Direction is required on every
`gateway_route`.

### Cross-checks

- CAN spec on a FlexRay-kind bus is rejected, and vice versa.
- The gateway's reserved TIPC address (see §5) is rejected for any node.
- Bus identifiers that aren't well-known and aren't declared are
  rejected with a list of accepted names.

---

## 10. Importing DBC + FIBEX for the gateway catalog

The job: take a DBC (CAN) or FIBEX (FlexRay) network description, extract
every frame plus its routing metadata, and emit (a) a `package.art` with
one **opaque** `message FrameName { }` per gateway-visible frame, and
(b) a JSON catalog with per-signal layout that the netgraph generator
and the LSP read for completion.

These are the same parsers `theia/gateway/pero_cmp_lnx/tools/` uses —
vendored verbatim under `artheia/importers/_asam_cmp_parser.py` so the
two stacks agree byte-for-byte on what a frame is.

```sh
# CAN
artheia import-dbc \
    --dbc MLBevo_KCAN.dbc \
    --bus kcan \
    --out vendor/autosar/kcan/

# FlexRay
artheia import-fibex \
    --fibex MLBevo_FR_Cluster.xml \
    --bus mlbevo_gen2_a \
    --out vendor/autosar/mlbevo_gen2_a/
```

Each command writes a `package.art` (forward-decl messages, opaque
bodies) and a `catalog.json` next to it. ARXML is **not** supported —
the gateway already generates its netgraph from DBC/FIBEX, so reading
the higher-level ARXML view would be redundant.

What `package.art` contains: `package vendor.autosar.<bus>` and one
`message <FrameName> { }` per frame. Bit layout, signal types, can_id /
slot_id, dlc, channel, cycle — none of that lives in the `.art`. It all
lives in `catalog.json`, which is consumed downstream by:

- `gen-netgraph --catalog catalog.json` for `gateway_route signal=Foo`
  references in host-side `.art` files.
- The LSP for completion of catalog message names.

The `--csv` filter mirrors theia's tooling. The CSV has a
`signal_name,message_name` header; only listed frames are emitted.
Omit `--csv` to emit every frame.

The generated files are **regenerable** — never hand-edit. Re-running
the importer overwrites them.

In your application `.art`, reference the generated messages:

```artheia
package my.app

import gateway.signals.*

interface senderReceiver SpeedIfArt {
    data Message1 packed_speed
}

node atomic SpeedSink {
    tipc type=0x80010101 instance=0
    ports {
        receiver speed_in requires SpeedIfArt
    }
}

gateway_route SpeedSink {
    signal = Message1
    direction = in
}
```

---

## 11. The generators

### `artheia gen-proto`

One `.proto` file per `message`, with `import "Other.proto";` lines for
cross-message references. Field shape matches the conventions used by the
existing nanopb pipeline in `theia/.../tools/templates/proto.j2` — so the
output drops into your existing codec build.

### `artheia gen-netgraph`

A single JSON document describing the system at runtime:

```json
{
  "package": "my.app",
  "nodes": [
    { "name": "SpeedPublisher",
      "tipc": {"type": "0x80010001", "instance": "0"},
      "ports": [...],
      "gateway_routes": [
        {"form": "can", "direction": "in",
         "can": {"can_id": 66, "bus": "kcan", "dlc": 8}}
      ]
    }
  ],
  "compositions": [
    { "name": "VehicleSystem",
      "prototypes": [...],
      "connections": [...]
    }
  ]
}
```

If you pass `--catalog gateway_catalog.json`, `signal=` routes are
resolved on the spot; otherwise they appear with `"unresolved": true`.

### `artheia gen-etcd`

The seed schema described in §7. Flat dict keyed by etcd path; one entry
per param across all nodes.

### `artheia gen-cpp-stubs`

Callback-style free-function header stubs, one file per node. No classes,
no inheritance, no run loop. The runtime target for Artheia is C++; the
older Python stub generator was removed because it was never load-bearing
for any real deployment.

For a node with a receiver port `speed_in` carrying `SpeedSignal`, a
sender port `out` carrying `TorqueRequest`, a client port `status_query`
calling `GetStatus()`, and params `gain`, `max_torque`, the C++ stub
exports:

```c
// the user implements
void on_speed_in_speed(const SpeedSignal* msg);
void on_param_gain(float new_value);
void on_param_max_torque(uint32_t new_value);

// the user calls
int  send_out_torque(const TorqueRequest* msg);
int  call_status_query_GetStatus(StatusReport* response);

// runtime exposes
float    get_param_gain(void);
uint32_t get_param_max_torque(void);
```

Runtime glue — wiring TIPC reads to `on_*` callbacks, wiring `send_*` to
TIPC writes, wiring etcd watchers to `on_param_*` — lives outside the
generated stubs. Artheia generates the surface, not the host framework.

---

## 12. Worked example

Here's the demo file from `examples/demo.art`, complete:

```artheia
package theia.demo

// --- messages ---
message SpeedSignal {
    uint32 speed_kph = 1
    uint64 ts_ns     = 2
}
message TorqueRequest {
    sint32 torque_nm = 1
    uint64 ts_ns     = 2
}
message StatusReport {
    bool   healthy = 1
    string note    = 2
}

// --- interfaces ---
interface senderReceiver SpeedIf  { data SpeedSignal speed }
interface senderReceiver TorqueIf { data TorqueRequest torque }
interface clientServer StatusIf {
    operation GetStatus() returns StatusReport
}

// --- nodes ---
node atomic SpeedPublisher {
    tipc type=0x80010001 instance=0
    ports {
        sender out provides SpeedIf
        server status provides StatusIf
    }
    params {
        publish_period_ms : uint32 = 10
        enabled           : bool   = true
        source_name       : string = "front-axle"
    }
}
node atomic TorqueController {
    tipc type=0x80010002 instance=0
    ports {
        receiver speed_in requires SpeedIf
        sender   torque_out provides TorqueIf
        client   status_query requires StatusIf
    }
    params {
        gain       : float  = 1.25
        max_torque : uint32 = 250
    }
}
node atomic Actuator {
    tipc type=0x80010003 instance=0
    ports { receiver torque_in requires TorqueIf }
}

// --- composition ---
composition VehicleSystem {
    prototype SpeedPublisher    speed_pub
    prototype TorqueController  torque_ctrl
    prototype Actuator          actuator

    connect speed_pub.out         to torque_ctrl.speed_in
    connect torque_ctrl.torque_out to actuator.torque_in
    connect torque_ctrl.status_query to speed_pub.status
}

// --- gateway termination ---
gateway_route SpeedPublisher {
    can id=0x42 bus=kcan dlc=8
    direction=in
}
gateway_route TorqueController {
    can id=0x101 bus=kcan dlc=8 extended_id=false
    direction=out
}
```

Generate everything:

```sh
artheia gen-proto      examples/demo.art --out generated/proto
artheia gen-netgraph   examples/demo.art --out generated/netgraph.json
artheia gen-etcd       examples/demo.art --out generated/etcd_schema.json
artheia gen-cpp-stubs  examples/demo.art --out generated/cpp
```

You will get one `.proto` per message, a netgraph with three nodes plus
their gateway routes, an etcd schema with five keys, and three C++ header
stubs.

---

## 13. The CLI

```
artheia parse FILE                            validate + print summary
artheia gen-proto FILE --out DIR              one .proto per message
artheia gen-netgraph FILE --out FILE          composition + gateway routes
       [--catalog CATALOG.json]               resolve signal= routes
artheia gen-etcd FILE --out FILE              flat etcd seed schema
artheia gen-cpp-stubs FILE --out DIR          one <Node>_gen.h per node
artheia import-dbc                            extract CAN frames from DBC
       --dbc PATH --bus NAME --out DIR
       [--csv PATH]
artheia import-fibex                          extract FlexRay frames from FIBEX
       --fibex PATH --bus NAME --out DIR
       [--csv PATH]
```

Exit code 0 on success, 1 on errors. Errors are written to stderr.

---

## 14. Editor support

The VS Code extension lives at `vscode-extension/`. It bundles a
TextMate grammar (instant highlighting, works offline) and an LSP client
that spawns `artheia-lsp` over stdio.

What the LSP provides:

- **Diagnostics** on open/change/save — every textX semantic or syntactic
  error becomes a red squiggle with the parser's line and column.
- **Goto-definition** for any identifier that's the name of a declared
  message, interface, node, composition, bus, or prototype.
- **Completion** offering: every keyword in context, every workspace
  symbol from any `.art` file open in the editor, and every gateway
  message found in any `gateway_catalog*.json` in the workspace.

The completion source for gateway messages is the same JSON the netgraph
generator reads, so the catalog written by `artheia import-dbc` /
`artheia import-fibex` does double duty: it routes the netgraph and it
powers editor completion for those signal names.

Install:

```sh
pip install -e .            # gives you artheia + artheia-lsp on PATH
cd vscode-extension
npm install && npm run compile
npx vsce package --no-dependencies
code --install-extension artheia-0.0.1.vsix
```

Settings (in VS Code):

| Key | Default | What |
|---|---|---|
| `artheia.serverCommand` | `artheia-lsp` | how to launch the LSP |
| `artheia.serverArgs` | `[]` | extra args |

---

## 15. Out-of-scope, on purpose

Artheia deliberately does **not** include any of the following. They have
been considered and rejected. They are not on a roadmap.

- **ARXML.** Not read, not emitted. The gateway already generates its
  netgraph from DBC/FIBEX; reading the higher-level ARXML system view on
  top of that would be redundant work for no win. System engineering
  owns the ARXML.
- **Python code generation.** Artheia targets C++ runtimes; the older
  Python stub generator was removed because it was never load-bearing
  for any real deployment.
- **Migrations / versioned schemas for params.** etcd is mutable; new
  installs ship a fresh schema. There is no `version` or `migration`
  syntax and there never will be.
- **AUTOSAR-tool compatibility.** Artheia is not an AUTOSAR authoring
  tool. It reads DBC + FIBEX for the gateway catalog and that's it.
- **SWC / runnable / interface metamodel extraction.** Artheia
  interfaces and nodes describe the host runtime, not classic-AUTOSAR
  SWCs.
- **Extension-point grammar plugins** (the ARText `@ArtextExtension`
  mechanism). Adds complexity that nothing currently uses.

If a feature you want is on this list, the answer is "build it as a
post-processor on the netgraph or stub output, not in the DSL."

---

*Document status: this manual matches Artheia v0.1. The generator output
formats and grammar are stable for v0.1; backwards-incompatible changes
move us to v0.2.*
