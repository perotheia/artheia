# artheia

A host-side DSL for modeling **Adaptive-AUTOSAR-style** components — atomic
nodes with explicit TIPC addresses and typed ports, wired into compositions and
clusters — plus the code generators that turn an `.art` model into C++
scaffolds, `.proto` wire types, deploy manifests, and netgraphs for the
**Theia** runtime.

Inspired by ARText (the syntax aesthetic + the structural concepts: atomic
components, ports, prototypes, compositions, connectors), but its own thing —
not AUTOSAR-tool-compatible.

## Install

```sh
pip install -e .            # or: pip install --find-links /opt/theia/wheels artheia
```

Gives you the `artheia` CLI, the `artheia-lsp` language server (for the editor
integrations), and `artheia-mcp` (the generator surface as an MCP server).

## The grammar in one breath

Three primitives, bottom to top:

| Primitive | Is a | Owns |
| --- | --- | --- |
| **node** | thread | one TIPC `type/instance`, typed ports, a behavior class |
| **composition** | process (one executable) | node *prototypes* + in-process wiring (`connect`) |
| **cluster** | distribution bundle | compositions + inter-process wiring; the deploy unit |

Around them: `message`/`enum` (proto3-equivalent data), `interface`
(`senderReceiver` for streams, `clientServer` for request/reply), and `import`
across packages.

## Example

```art
package system.demo

// --- data ----------------------------------------------------------------
enum Mode { IDLE = 0  RUN = 1 }

message Tick   { uint32 seq }
message SetMode { Mode mode }

// --- contracts -----------------------------------------------------------
interface senderReceiver TickStream { }          // a stream of Tick
interface clientServer   ModeCtl {
    operation Set(in r:SetMode) returns Tick
}

// --- a node: one thread, one TIPC endpoint -------------------------------
node atomic Counter {
    tipc type=0x80020001 instance=0
    ports {
        receiver ticks_in requires TickStream     // inbound stream
        server   ctl      provides ModeCtl        // request/reply surface
    }
}

node atomic Driver {
    tipc type=0x80020002 instance=0
    ports {
        sender ticks_out provides TickStream
        client mode_call requires ModeCtl
    }
}

// --- a composition: the process, wiring prototypes together --------------
composition DemoProc {
    prototype Counter counter
    prototype Driver  driver

    connect driver.ticks_out to counter.ticks_in
    connect driver.mode_call to counter.ctl
}

// --- a cluster: the deploy/package unit ----------------------------------
cluster Demo {
    composition DemoProc proc
}
```

## What you do with a model

```sh
artheia parse        system/system.art                 # resolve + validate the tree
artheia check-addresses system/system.art              # assert TIPC addresses unique
artheia gen-app --kind fc <component>.art --out apps    # C++ scaffold (lib/main/impl)
artheia gen-proto    <component>.art --out platform/proto
artheia gen-netgraph <component>.art --out netgraph.json
artheia gen-manifest <component>.art manifest/app.py    # deploy manifest module
```

Run `artheia --help` for the full command set (~30 generators incl. DBC/FIBEX
import, config-schema migration, and the rig/manifest pipeline). The `gen_server`
shape follows OTP — `handle_call` / `handle_cast` / `handle_info` per node.

## Editor support

`.art` files get highlighting + LSP (diagnostics, goto-definition, completion)
in VS Code and Emacs via the `artheia-lsp` server — see the umbrella repo's
`contrib/editors/`.

## Layout

```
artheia/
  grammar/*.tx        textX grammar
  model/              name resolution + validators
  generators/         the gen-* code generators + Jinja2 templates
  lsp/                the artheia-lsp language server (pygls)
  adapters/           the artheia-mcp server (FastMCP)
  cli.py              the `artheia` CLI (Click)
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
