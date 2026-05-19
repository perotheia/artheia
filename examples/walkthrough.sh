#!/usr/bin/env bash
#
# End-to-end Artheia walkthrough. Runs every stage of the toolchain
# against the demo .art file and prints a compact summary at each step,
# so a fresh reader sees exactly what each command produces.
#
# Stages:
#   0. environment + version
#   1. parse the demo file
#   2. gen-proto, gen-netgraph, gen-etcd, gen-cpp-stubs
#   3. confirm generated artifacts are well-formed
#   4. (optional) import-dbc / import-fibex against caller-supplied paths
#   5. smoke the LSP via the headless protocol test
#
# Usage:
#   bash examples/walkthrough.sh                 # uses .venv if present
#   ARTHEIA_BIN=artheia bash examples/walkthrough.sh
#   ARTHEIA_DBC=/path/MLBevo.dbc ARTHEIA_DBC_BUS=kcan bash examples/walkthrough.sh
#   ARTHEIA_FIBEX=/path/cluster.xml ARTHEIA_FIBEX_BUS=mlbevo_gen2_a bash examples/walkthrough.sh

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
    echo "       Set up the venv first:  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
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

# ---- stage 1: parse the demo ----------------------------------------------
banner "1. Parse demo.art"
"$BIN" parse examples/demo.art

# ---- stage 2: generators (proto, netgraph, etcd) --------------------------
banner "2. Generators"
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

# ---- stage 3: stubs --------------------------------------------------------
banner "3. C++ stubs (callback-style, no framework)"
"$BIN" gen-cpp-stubs examples/demo.art --out "$OUT/cpp"  | sed 's|^|   |'

section "C++ stub interface — TorqueController_gen.h"
grep -E '^(void|int|float|uint|bool|const char|#define)' "$OUT/cpp/TorqueController_gen.h" | sed 's|^|   |'

# ---- stage 4: sanity-check generated code ---------------------------------
banner "4. Sanity-check generated artifacts"
section ".proto files have valid proto3 syntax (best-effort lint)"
for f in "$OUT"/proto/*.proto; do
    grep -q '^syntax = "proto3";' "$f" || { echo "   $f: missing proto3 syntax"; exit 1; }
    grep -q '^package '            "$f" || { echo "   $f: missing package";       exit 1; }
done
note "$(ls "$OUT/proto" | wc -l) .proto files passed"

# ---- stage 5 (optional): DBC + FIBEX import -------------------------------
banner "5. AUTOSAR import (optional)"
if [ -n "${ARTHEIA_DBC:-}" ]; then
    BUS="${ARTHEIA_DBC_BUS:-bus}"
    section "import-dbc — $ARTHEIA_DBC (bus=$BUS)"
    "$BIN" import-dbc --dbc "$ARTHEIA_DBC" --bus "$BUS" --out "$OUT/autosar/$BUS"
else
    note "set ARTHEIA_DBC=/path/to/file.dbc (and ARTHEIA_DBC_BUS) to exercise import-dbc"
fi
if [ -n "${ARTHEIA_FIBEX:-}" ]; then
    BUS="${ARTHEIA_FIBEX_BUS:-fr}"
    section "import-fibex — $ARTHEIA_FIBEX (bus=$BUS)"
    "$BIN" import-fibex --fibex "$ARTHEIA_FIBEX" --bus "$BUS" --out "$OUT/autosar/$BUS"
else
    note "set ARTHEIA_FIBEX=/path/to/cluster.xml (and ARTHEIA_FIBEX_BUS) to exercise import-fibex"
fi

# ---- stage 6: LSP smoke-test ----------------------------------------------
banner "6. LSP — real JSON-RPC smoke-test"
if .venv/bin/pytest tests/test_lsp_protocol.py -q 2>&1 | tail -2 | head -1; then
    :
fi

# ---- summary ---------------------------------------------------------------
banner "Walkthrough complete — artifacts under $OUT"
find "$OUT" -type f | sort | sed 's|^|   |'
