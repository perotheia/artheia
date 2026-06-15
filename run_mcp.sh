#!/usr/bin/env bash
# artheia MCP launcher — workspace-relative, no hardcoded paths.
#
# Uses the single workspace .venv (artheia is editable-installed there via
# `pip install -e artheia/`). Pointed at by workspace-root .mcp.json so Claude
# Code discovers it automatically. Mirrors testing/run_mcp.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # artheia/
WORKSPACE="$(cd "${SCRIPT_DIR}/.." && pwd)"                       # repo / ws root

VENV_PY="${WORKSPACE}/.venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
    echo "artheia: ${VENV_PY} missing — run:" >&2
    echo "  python3 -m venv .venv && ./.venv/bin/pip install -e artheia/" >&2
    exit 1
fi

# Resolve relative .art paths the model passes against the dir the user is
# working in (the workspace root), not artheia/.
export THEIA_INVOCATION_CWD="${THEIA_INVOCATION_CWD:-$WORKSPACE}"

# Run from a neutral cwd so the INSTALLED artheia package wins import — the
# repo root contains a sibling `artheia/` source dir that would otherwise
# shadow it (namespace-package collision). Path resolution still happens
# against THEIA_INVOCATION_CWD (the server chdir's there per call).
cd /
exec "${VENV_PY}" -m artheia.adapters.mcp_server "$@"
