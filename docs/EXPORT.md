# Importing into Artheia

How external network and component descriptions become Artheia `.art` and
`catalog.json` files. There are three independent flows; you wire them
together at netgraph time.

```
   DBC files     ─┐
   FIBEX files   ─┼──►  artheia import-dbc / import-fibex   ──►  vendor/autosar/<bus>/{package.art, catalog.json}
                  │
   Tornado tree  ─┴──►  artheia.importers.fusee             ──►  vendor/tornado/system/{signals,components,system.art}

   demo.art      ─►  artheia gen-netgraph --catalog ...     ──►  netgraph.json
```

The two AUTOSAR importers (DBC + FIBEX) feed the *gateway* side: which
CAN/FlexRay frames terminate on the host. Fusée feeds the *host* side:
which DDS topics the host components publish and consume. Both produce
file layouts the LSP indexes for goto-definition and completion.

---

## 1. DBC import — CAN frames

```sh
artheia import-dbc \
    --dbc MLBevo_KCAN.dbc \
    --bus kcan \
    --out vendor/autosar/kcan/
```

### What goes in

A single DBC file. Anywhere on disk. We use the same parser
(`asam_cmp_parser.DbcDb`) the gateway uses, vendored verbatim at
`artheia/importers/_asam_cmp_parser.py`. If your DBC parses in theia's
codec generator, it parses here.

The `--bus` flag names the bus this file describes (`kcan`, `hcan`,
`dcan`, etc.). The same name appears in every catalog entry and is what
your host-side `gateway_route` blocks reference via `bus=kcan`.

Optional `--csv path/to/filter.csv` with theia's two-column format
restricts which frames are emitted:

```
signal_name,message_name
ACC_07_CRC,ACC_07
ACC_07_BZ,ACC_07
Brake_01_Pressure,Brake_01
```

Only frames listed under `message_name` survive. Omit the CSV to emit
every frame in the DBC.

### What comes out

Two files under `vendor/autosar/<bus>/`:

**`package.art`** — Artheia forward declarations, one per CAN frame:

```artheia
package vendor.autosar.kcan

message ACC_07 { }
message Brake_01 { }
message GearShifter_01 { }
…
```

The bodies are intentionally empty. Bit layout, signal types, factor /
offset, units — none of that lives in the `.art`. The frame name is a
**symbol** the rest of Artheia can refer to (in `gateway_route signal=`,
in the LSP completion list), nothing more.

**`catalog.json`** — netgraph metadata. The shape `gen-netgraph
--catalog` already understands:

```json
{
  "bus": "kcan",
  "bus_kind": "can",
  "messages": {
    "ACC_07": {
      "bus": "kcan",
      "bus_kind": "can",
      "can_id": 302,
      "extended_id": false,
      "dlc": 8,
      "fields": [
        { "name": "ACC_07_CRC", "bit_position": 0, "bit_length": 8,
          "proto_type": "uint32", "is_signed": false,
          "motorola_byte_order": false,
          "factor": 1.0, "offset": 0.0, "unit": "" },
        …
      ]
    },
    …
  }
}
```

The per-signal layout (`fields[]`) is the decoder side — downstream code
generators (theia's nanopb wire encoder, etc.) read it to emit byte-level
codecs. Artheia itself never decodes; the catalog is just data it
forwards.

---

## 2. FIBEX import — FlexRay frames

```sh
artheia import-fibex \
    --fibex MLBevo_FR_Cluster.xml \
    --bus mlbevo_gen2_a \
    --out vendor/autosar/mlbevo_gen2_a/
```

Mechanically identical to DBC import; the parser is
`asam_cmp_parser.FibexDb`. The differences are in the catalog shape:

```json
{
  "bus": "mlbevo_gen2_a",
  "bus_kind": "flexray",
  "messages": {
    "BrakeFrame_A": {
      "bus": "mlbevo_gen2_a",
      "bus_kind": "flexray",
      "slot_id": 5,
      "cycle": 0,
      "cycle_repetition": 1,
      "channel": "channel_a",
      "channel_idx": 0,
      "byte_length": 8,
      "fields": [ … ]
    }
  }
}
```

`can_id` / `extended_id` / `dlc` are absent; in their place are
`slot_id`, `cycle`, `cycle_repetition`, `channel`, `channel_idx`,
`byte_length`. The `extra_channels` field appears when the same frame
triggers on both channels (A and B).

### Channel names

The channel name comes from the FIBEX `CHANNEL`'s element `ID`, not its
`SHORT-NAME`. Real cluster files use IDs like `channel_5971626`; if you
want human-readable bus suffixes, encode them in the `--bus` value
(`mlbevo_gen2_a` vs `mlbevo_gen2_b`) and ship one import per channel.

---

## 3. Fusée — Tornado (DDS) host side

```sh
.venv/bin/python -c "
from pathlib import Path
from artheia.importers.fusee import import_all
import_all(Path('up/tornado'), Path('vendor/tornado'))
"
```

Fusée is a library, not a CLI subcommand. It reads a tornado source tree
(`up/tornado/`) and emits a fully-typed Artheia model of the host system.

### What goes in

A tornado checkout under `up/tornado/`:

```
up/tornado/
  signals/
    body/
      *.proto        ← proto2 message definitions
      BUILD          ← vehicle_os_signal(...) topic declarations
    cabin/
    chassis/
    …
  app/
    onboard/
      brake_manager/BUILD          ← vehicle_os_signal_junction(...)
      door_manager/BUILD
      drive_mode_manager/BUILD
      …
    vehicle_registration/BUILD     ← also reads app_config_proto
```

`up/` is in `.gitignore` — vendor your tornado checkout however you like.

### What comes out

Everything under `vendor/tornado/system/`, also gitignored — it's
regenerable build output:

```
vendor/tornado/system/
  signals/
    body/package.art          ← messages, enums, senderReceiver interfaces
    cabin/package.art
    chassis/package.art
    common_types/package.art
    powertrain/package.art
    thermal_management/package.art
    transmission/package.art
    vehicle/package.art
  components/
    brake_manager.art         ← node atomic per component, with ports
    door_manager.art
    drive_mode_manager.art
    gear_shift_manager.art
    mock_charge_provider.art
    torque_path_manager.art
    vehicle_registration.art  ← + inline config schema
  system.art                  ← composition TornadoSystem wiring pairs
```

Three categories of translation:

1. **proto2 → Artheia messages.** One Artheia `message` per proto message.
   `required` / `optional` labels drop (Artheia has proto3 semantics).
   Field numbers drop (the proto generator re-assigns them). Nested
   messages are hoisted to top level with `<Outer><Inner>` names. `oneof`
   is flattened: each branch becomes an ordinary field. `enum` declarations
   become real Artheia enums; the redundant `<ENUM_NAME>_` value prefix
   is stripped where it parses (digit-safe — `GPS_FIX_ENUM_2D_FIX` stays
   unchanged because `2D_FIX` isn't a valid identifier).
2. **`vehicle_os_signal(...)` → `interface senderReceiver`.** Each topic
   becomes an Artheia interface named after the bazel target with
   `_signal` stripped. The interface body is a single `data <Message>
   payload`.
3. **`vehicle_os_signal_junction(...)` → `node atomic`.** Each component
   becomes a node with synthetic TIPC, one receiver port per
   `input_signals` entry, one sender port per `output_signals` entry. The
   QoS string (`RELIABLE` / `BEST_EFFORT`) maps to the port's
   `reliable | best_effort` modifier.

The composition (`vendor/tornado/system/system.art`) emits one
`connect <pub>.<port> to <sub>.<port>` for every pair of translated
components that share a topic. N×M tornado pub/sub becomes over-specified
1:1 connects.

### When a component declares `app_config_proto`

Some components — currently only `vehicle_registration` — declare a
runtime config schema via `mosaic_cc_application(..., app_config_proto =
":foo_proto", app_config_proto_name = "moz_cfg_X")` in the BUILD. When
present:

- Fusée parses the matching `.proto` in the component directory,
- Emits the config messages **inline** above the node in the component's
  `.art`,
- Adds a `config <RootMessageName>` line to the node decl. The
  cross-reference resolves to the local message; the grammar requires it
  be a `MessageDecl`.

### Cross-category forward declarations

textX's name resolution is single-file. A message used in `body/`
that's declared in `common_types/` cannot be resolved across files. Fusée
emits a local empty stub (`message X { }` or `enum X { }`) for every
cross-category reference, annotated with `// origin:
vendor.tornado.system.signals.<cat>`. The LSP's `_is_stub` heuristic
recognises these and prefers non-stub declarations when you press F12.

---

## 4. Wiring the catalogs into gen-netgraph

A host-side `.art` references a CAN frame symbolically:

```artheia
gateway_route SpeedFromCar {
    signal = ACC_07
    direction = in
}
```

To resolve `ACC_07` to its bus + can_id at netgraph time, pass the
catalog:

```sh
artheia gen-netgraph examples/demo.art \
    --catalog vendor/autosar/kcan/catalog.json \
    --out generated/netgraph.json
```

For multiple buses you currently merge by hand — `--catalog` takes one
file. The merge is `dict.update`-shaped: union of `messages` dictionaries.

The LSP picks up catalogs automatically from anywhere matching:

- `gateway_catalog*.json` (legacy name)
- `*.gateway-catalog.json` (legacy name)
- `vendor/autosar/*/catalog.json` (new layout)

Completion of `signal = …` references draws from the union of all
matched files.

---

## 5. Refreshing the vendored parser

`artheia/importers/_asam_cmp_parser.py` is a verbatim copy of
`theia/gateway/pero_cmp_lnx/tools/asam_cmp_parser.py`. When theia's
parser changes (new bus types, FIBEX 4.x support, etc.), re-vendor:

```sh
cp ../theia/gateway/pero_cmp_lnx/tools/asam_cmp_parser.py \
   artheia/importers/_asam_cmp_parser.py
```

Then restore the provenance header at the top of the file (commit hash
points back at the theia commit you copied from). Run `pytest tests/` to
confirm nothing in the autosar test fixtures regressed.

Do **not** edit the file locally. If you find a bug, fix it upstream in
theia and re-vendor.

---

## 6. What never makes it across

- **ARXML.** We don't read it. The gateway generates its netgraph from
  DBC/FIBEX directly, so reading the higher-level ARXML system view on
  top would be redundant. ARXML round-trip is out of scope forever.
- **proto2 defaults.** Dropped at import time; Artheia messages have
  proto3 semantics. Per-field `(nanopb).*` annotations *do* survive —
  they pass through verbatim into the `.art` and back out into generated
  `.proto` files so nanopb's static sizing still works.
- **proto2 `oneof` semantics.** The wire-format guarantee that at most
  one branch is set is lost; you get a flat set of fields with `// oneof
  <name>` annotations as a reading aid only.
- **Tornado's broadcast topic semantics.** N publishers × M subscribers
  on a single DDS topic becomes N×M individual `connect` lines. Topology
  is over-specified — there's no `topic Foo { … }` keyword yet.
- **DDS QoS beyond reliability.** Only `RELIABLE` / `BEST_EFFORT` round-
  trip into the grammar's `reliable | best_effort` modifier. Durability,
  history depth, deadline — none of those have grammar yet.

---

## 7. Tests

The autosar importer has minimal-fixture tests at `tests/test_autosar.py`:
a hand-written DBC string with two frames + three signals, a hand-written
FIBEX 3.1 XML with one frame + one PDU + one signal, and a CSV-filter
case. Fusée has round-trip tests at `tests/test_fusee.py` covering enum
prefix stripping and the digit-safe fallback. All of them assert on
`parse_file` round-trip — the .art is generator output, but it must also
be a valid Artheia program.
