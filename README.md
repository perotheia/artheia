# Artheia

Host-side DSL for modeling Adaptive-AUTOSAR-style components on the host that
talk to Classical AUTOSAR through the Theia CAN/FlexRay gateway over TIPC.

Inspired by ARText (BMW Car IT, hibernating since ~2011) but **not**
AUTOSAR-tool-compatible — Artheia is its own thing, borrowing the syntax
aesthetic and the structural concepts (atomic components, ports, prototypes,
compositions, connectors).

## Two parts of the language

1. **Message layout** — proto3-equivalent messages. `artheia gen-proto` emits
   `.proto` files compatible with the nanopb codec pipeline already in
   `~/repo/theia/gateway/pero_cmp_lnx/tools/`.
2. **Node definition + composition** — atomic nodes with explicit TIPC
   addresses, sender/receiver/client/server ports referencing interfaces,
   compositions wiring node prototypes together. `artheia gen-netgraph` emits
   the runtime netgraph (who sends what to whom, on which TIPC instance).

Config parameters and release migrations are **out of scope** — they live in
a separate Puppet-style deployment model that consumes an extract from
Artheia's parsed AST.

## Quick start

```bash
pip install -e '.[dev]'

artheia parse        examples/demo.art
artheia gen-proto    examples/demo.art --out generated/proto
artheia gen-netgraph examples/demo.art --out generated/netgraph.json
```

## Layout

```
artheia/
  grammar/artheia.tx     # textX grammar (single file, v0)
  model/                 # name-resolution + validators
  generators/
    proto.py             # message → .proto
    netgraph.py          # nodes+compositions → JSON
    templates/           # Jinja2
  cli.py
examples/demo.art
tests/
```
