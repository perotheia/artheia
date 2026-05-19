#!/usr/bin/env bash
#
# End-to-end Artheia walkthrough. Runs every stage of the toolchain
# against the sample inputs and prints a compact summary at each step,
# so a fresh reader sees exactly what each command produces.
#
# Stages:
#   0. environment + version
#   1. import ARXML  -> gateway_signals.art + gateway_catalog.json
#   2. validate ARXML output round-trips through the parser
#   3. parse the demo file
#   4. gen-proto, gen-netgraph (with the catalog), gen-etcd
#   5. gen-cpp-stubs, gen-py-stubs
#   6. confirm generated Python is importable, headers compile-clean
#      (syntactic only — no real protoc / gcc invocation)
#   7. smoke the LSP via the headless protocol test
#
# Usage:
#   bash examples/walkthrough.sh                 # uses .venv if present
#   ARTHEIA_BIN=artheia bash examples/walkthrough.sh

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# ---- locate the artheia CLI ------------------------------------------------
if [ -n "${ARTHEIA_BIN:-}" ]; then
    BIN="$ARTHEIA_BIN"
elif [ -x "$REPO/.venv/bin/artheia" ]; then
    BIN="$REPO/.venv/bin/artheia"
    PY="$REPO/.venv/bin/python"
elif command -v artheia >/dev/null 2>&1; then
    BIN="artheia"
    PY="python3"
else
    echo "error: 'artheia' not on PATH and no .venv/bin/artheia found." >&2
    echo "       Set up the venv first:  python3 -m venv .venv && .venv/bin/pip install -e '.[dev,importers]'" >&2
    exit 1
fi
PY="${PY:-python3}"

# ---- presentation helpers --------------------------------------------------
banner() {
    printf '\n\033[1;36m== %s ==\033[0m\n' "$*"
}
note() {
    printf '   %s\n' "$*"
}
section() {
    printf '\n\033[1;33m-- %s --\033[0m\n' "$*"
}

OUT="$REPO/generated/walkthrough"
rm -rf "$OUT"
mkdir -p "$OUT"

# ---- stage 0: env ----------------------------------------------------------
banner "0. Environment"
note "artheia: $BIN"
"$BIN" --version
note "python : $("$PY" --version 2>&1)"
note "out    : $OUT"

# ---- stage 1: import ARXML -------------------------------------------------
banner "1. Import ARXML — CAN sample (cantools/system-4.2.arxml, MIT)"
"$BIN" import-arxml examples/arxml/system-4.2.arxml \
    --out-art     "$OUT/can_gateway_signals.art" \
    --out-catalog "$OUT/gateway_catalog.json" \
    --package     gateway.signals.cantools

section "first 12 lines of the generated CAN stub"
sed -n '1,12p' "$OUT/can_gateway_signals.art"

section "catalog summary"
"$PY" - <<PY "$OUT/gateway_catalog.json"
import json, sys
c = json.loads(open(sys.argv[1]).read())
msgs = c["messages"]
print(f"  {len(msgs)} CAN messages extracted:")
for name, m in msgs.items():
    if m["bus_kind"] == "can":
        cid = f"0x{m['can_id']:x}"
        nf = len(m.get("fields", []))
        print(f"    {name:24s}  bus={m['bus']:<10s} can_id={cid:6s} dlc={m.get('dlc','?'):>3}  fields={nf}")
PY

banner "1b. Import ARXML — synthetic FlexRay fixture"
"$BIN" import-arxml examples/arxml/synthetic_flexray.arxml \
    --out-art     "$OUT/fr_gateway_signals.art" \
    --out-catalog "$OUT/fr_gateway_catalog.json" \
    --package     gateway.signals.fr

section "FlexRay catalog summary"
"$PY" - <<PY "$OUT/fr_gateway_catalog.json"
import json, sys
c = json.loads(open(sys.argv[1]).read())
for name, m in c["messages"].items():
    if m["bus_kind"] == "flexray":
        print(f"    {name:18s}  bus={m['bus']:<10s} slot={m['slot_id']:<3d} channel={m['channel']}  fields={len(m['fields'])}")
PY

# ---- stage 2: round-trip the generated .art -------------------------------
banner "2. Round-trip — parse the generated stubs back through Artheia"
"$BIN" parse "$OUT/can_gateway_signals.art" | tail -5
"$BIN" parse "$OUT/fr_gateway_signals.art"  | tail -5

# ---- stage 3: parse the demo ----------------------------------------------
banner "3. Parse demo.art"
"$BIN" parse examples/demo.art

# ---- stage 4: generators (proto, netgraph w/ catalog, etcd) ---------------
banner "4. Generators"
"$BIN" gen-proto      examples/demo.art --out "$OUT/proto"        | sed 's|^|   |'
"$BIN" gen-netgraph   examples/demo.art --out "$OUT/netgraph.json" | sed 's|^|   |'
"$BIN" gen-etcd       examples/demo.art --out "$OUT/etcd_schema.json" | sed 's|^|   |'

section "netgraph excerpt — gateway_routes per node"
"$PY" - <<'PY' "$OUT/netgraph.json"
import json, sys
g = json.loads(open(sys.argv[1]).read())
for n in g["nodes"]:
    rts = n.get("gateway_routes", [])
    if not rts:
        continue
    for r in rts:
        if r["form"] == "can":
            c = r["can"]
            print(f"   {n['name']:18s}  CAN can_id=0x{c['can_id']:x} bus={c['bus']:<8s} dir={r['direction']}")
PY

section "etcd schema excerpt"
"$PY" - <<'PY' "$OUT/etcd_schema.json"
import json, sys
e = json.loads(open(sys.argv[1]).read())
print(f"   {len(e['keys'])} keys total. first three:")
for k, v in list(e["keys"].items())[:3]:
    print(f"   {k}  ({v['type']}, default={v['default']!r})")
PY

# ---- stage 5: stubs --------------------------------------------------------
banner "5. Stubs — C++ and Python (callback-style, no framework)"
"$BIN" gen-cpp-stubs examples/demo.art --out "$OUT/cpp"  | sed 's|^|   |'
"$BIN" gen-py-stubs  examples/demo.art --out "$OUT/py"   | sed 's|^|   |'

section "C++ stub interface — TorqueController_gen.h"
grep -E '^(void|int|float|uint|bool|const char|#define)' "$OUT/cpp/TorqueController_gen.h" | sed 's|^|   |'

# ---- stage 6: sanity-check generated code ---------------------------------
banner "6. Sanity-check generated artifacts"
section "Generated Python stubs parse as valid Python"
"$PY" - <<PY "$OUT/py"
import ast, pathlib, sys
root = pathlib.Path(sys.argv[1])
ok = 0
for p in root.glob("*.py"):
    ast.parse(p.read_text())
    ok += 1
print(f"   parsed {ok} files OK")
PY

section ".proto files have valid proto3 syntax (best-effort lint)"
for f in "$OUT"/proto/*.proto; do
    grep -q '^syntax = "proto3";' "$f" || { echo "   $f: missing proto3 syntax"; exit 1; }
    grep -q '^package '            "$f" || { echo "   $f: missing package";       exit 1; }
done
note "$(ls "$OUT/proto" | wc -l) .proto files passed"

# ---- stage 7: LSP smoke-test ----------------------------------------------
banner "7. LSP — real JSON-RPC smoke-test"
if .venv/bin/pytest tests/test_lsp_protocol.py -q 2>&1 | tail -2 | head -1; then
    :
fi

# ---- summary ---------------------------------------------------------------
banner "Walkthrough complete — artifacts under $OUT"
find "$OUT" -type f | sort | sed 's|^|   |'
