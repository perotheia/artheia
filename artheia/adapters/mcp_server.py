"""MCP server: expose the artheia DSL/generator CLI as MCP tools.

Mirrors the shape of ``rf_theia/adapters/mcp_server.py`` (FastMCP over
stdio), retargeted at the artheia generator surface so Claude Code can drive
``gen-fc`` / ``gen-manifest`` / ``gen-proto`` / ``parse`` / … directly as an
API instead of shelling out.

Run with::

    python -m artheia.adapters.mcp_server      # or the `artheia-mcp` script

Or via the wrapper ``artheia/run_mcp.sh``, which sources the workspace venv
before exec'ing this module. Pointed at by the workspace-root ``.mcp.json``
so Claude Code discovers it automatically.

Design
------
Every command runs **in-process** through Click's :class:`CliRunner` against
the live ``artheia.cli.main`` group — no per-call subprocess fork, exit code
and combined output captured cleanly. The tool list is **introspection
driven** (``list_generators`` / ``describe`` read the live Click group), so
it never drifts as commands are added. A handful of high-traffic generators
get first-class typed tools; the generic :func:`gen` is the escape hatch for
any command with raw argv.

Tools exposed:

  - ``list_generators``  — the live command catalog (name + one-line help)
  - ``describe``         — full ``--help`` for one command
  - ``parse``            — resolve/validate an .art tree (the smoke test)
  - ``check_addresses``  — assert TIPC (type,instance) uniqueness
  - ``gen_fc``/``gen_fc_lib`` — C++ FC app / node lib (lib/main/impl + proto)
  - ``gen_manifest``     — Functional-Cluster manifest module
  - ``gen_proto``        — .proto per message
  - ``gen_schema``       — combined config-schema JSON
  - ``gen_netgraph``     — nodes+compositions JSON netgraph
  - ``gen``              — invoke ANY artheia command by name + argv
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

import click
from click.testing import CliRunner
from fastmcp import FastMCP

from artheia.cli import main as _cli

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("artheia")
except Exception:  # pragma: no cover — source tree without dist metadata
    __version__ = "0.0.0"

mcp = FastMCP(
    "artheia",
    instructions=(
        "Artheia DSL + generator CLI as an API. Resolve/validate .art system "
        "trees and run the gen-* code generators (gen-fc, gen-manifest, "
        "gen-proto, gen-schema, gen-netgraph, …) directly. Paths are resolved "
        "against the workspace root (the dir Claude Code runs in). Use "
        "list_generators to discover commands and describe for one command's "
        "full options."
    ),
)

# The workspace root the user invoked us from. setup.sh / the theia launcher
# export THEIA_INVOCATION_CWD; fall back to cwd. Relative .art paths in tool
# args resolve against this so the model can pass repo-relative paths.
_WORKSPACE = Path(
    os.environ.get("THEIA_INVOCATION_CWD") or os.getcwd()
).resolve()


def _run(argv: list[str]) -> str:
    """Invoke the artheia CLI in-process with ``argv``, from the workspace
    root, capturing combined stdout+stderr and the exit code."""
    runner = CliRunner()
    # Run with cwd = workspace so relative paths the model passes resolve the
    # same way they would in a terminal there.
    prev = os.getcwd()
    try:
        os.chdir(_WORKSPACE)
        result = runner.invoke(
            _cli, argv, catch_exceptions=True, prog_name="artheia"
        )
    finally:
        os.chdir(prev)
    out = result.output or ""               # stdout
    # Click >=8.2 keeps stderr separate; older merges it into .output.
    err = ""
    try:
        err = result.stderr if result.stderr_bytes is not None else ""
    except (ValueError, AttributeError):
        err = ""
    rc = result.exit_code
    head = f"$ artheia {' '.join(shlex.quote(a) for a in argv)}\n"
    # Append stderr only when it adds something stdout doesn't already carry
    # (CliRunner can echo usage/errors to both).
    add_err = err and err.strip() and err.strip() not in out
    body = out + (("\n" + err) if add_err else "")
    if result.exception is not None and rc != 0:
        # CliRunner swallows the traceback into result.exception; surface it.
        body += f"\n[exception] {type(result.exception).__name__}: {result.exception}"
    return f"{head}[exit {rc}]\n{body}".rstrip() + "\n"


def _commands() -> dict[str, click.Command]:
    """Flat catalog of the live CLI: top-level commands plus one level of
    nested groups exposed as ``group/sub``."""
    out: dict[str, click.Command] = {}
    for name, cmd in _cli.commands.items():
        if isinstance(cmd, click.Group):
            for sub, subcmd in cmd.commands.items():
                out[f"{name}/{sub}"] = subcmd
        else:
            out[name] = cmd
    return out


# ── introspection ──────────────────────────────────────────────────────────

@mcp.tool()
def list_generators() -> str:
    """List every artheia command (name + one-line help).

    This reads the LIVE Click group, so it always reflects the installed
    artheia — nothing is hardcoded. Nested groups appear as ``group/sub``
    (e.g. ``executor/emit``). Use ``describe`` for a command's full options,
    or ``gen`` to invoke any of them.
    """
    rows = []
    for name, cmd in sorted(_commands().items()):
        short = (cmd.get_short_help_str(limit=100) or "").strip()
        rows.append(f"  {name:24} {short}")
    return f"artheia {__version__} — commands:\n" + "\n".join(rows)


@mcp.tool()
def describe(command: str) -> str:
    """Show the full ``--help`` (usage, arguments, options) for one command.

    Args:
        command: A name from ``list_generators`` (e.g. ``gen-fc`` or a
                 nested ``executor/emit``).
    """
    argv = command.split("/") + ["--help"]
    return _run(argv)


# ── high-traffic typed tools ───────────────────────────────────────────────

@mcp.tool()
def parse(art_file: str, depth: int | None = None) -> str:
    """Parse and resolve an .art file as a tree — the standard validation
    smoke test (cross-refs, imports, TIPC bindings). Non-zero exit = a model
    error; the output points at the offending node.

    Args:
        art_file: Path to the .art (relative to the workspace root, e.g.
                  ``system/system.art``).
        depth:    Optional ``-L`` tree depth limit.
    """
    argv = ["parse", art_file]
    if depth is not None:
        argv += ["-L", str(depth)]
    return _run(argv)


@mcp.tool()
def check_addresses(art_file: str) -> str:
    """Assert every node's TIPC ``(type, instance)`` is unique across the
    whole system tree (the manifest/install gate). Reports any collision.

    Args:
        art_file: The aggregating .art (typically ``system/system.art``).
    """
    return _run(["check-addresses", art_file])


@mcp.tool()
def gen_fc(
    art_file: str,
    out: str,
    composition: str = "",
    proto_out: str = "",
    force: bool = False,
) -> str:
    """Generate a C++ FC / composition app (lib/ main/ impl/ + the .proto).

    The head of the gen-fc family. The write-once ``impl/<Node>_handlers.cc`` and
    ``impl/<Node>_state.hh`` are NOT overwritten unless ``force`` is set.

    Args:
        art_file:    The component/package .art for the FC (or app).
        out:         Output dir (the impl-layer dir, e.g. ``apps``).
        composition: Restrict generation to one composition by name.
        proto_out:   Where the generated .proto lands.
        force:       Overwrite the write-once impl slices.
    """
    argv = ["gen-fc", "--out", out]
    if composition:
        argv += ["--composition", composition]
    if proto_out:
        argv += ["--proto-out", proto_out]
    if force:
        argv += ["--force"]
    argv.append(art_file)
    return _run(argv)


@mcp.tool()
def gen_fc_lib(
    art_file: str,
    out: str,
    proto_out: str = "",
    vendored: bool = False,
    force: bool = False,
) -> str:
    """Generate a NO-MAIN C++ node library (the linkable form of gen-fc).

    Default: a Bazel node cc_library (the old ``--kind package``). ``vendored``:
    a self-contained slice with a vendored runtime that builds standalone (the
    old ``--kind lib``, now Bazel).

    Args:
        art_file:  The package .art for the node lib.
        out:       Output dir for the lib/impl slices (e.g. ``src``).
        proto_out: Where the generated .proto lands.
        vendored:  Emit the self-contained vendored-runtime layout.
        force:     Overwrite the write-once impl slices.
    """
    argv = ["gen-fc-lib", "--out", out]
    if vendored:
        argv += ["--vendored"]
    if proto_out:
        argv += ["--proto-out", proto_out]
    if force:
        argv += ["--force"]
    argv.append(art_file)
    return _run(argv)


@mcp.tool()
def gen_manifest(art_file: str, out_file: str) -> str:
    """Generate the Functional-Cluster manifest module from a component .art.

    Args:
        art_file: The component .art (e.g. ``system/app/component.art``).
        out_file: The manifest module to write (e.g. ``apps/manifest/app.py``).
    """
    return _run(["gen-manifest", art_file, out_file])


@mcp.tool()
def gen_proto(art_file: str, out: str = "") -> str:
    """Emit ``.proto`` files (one per message) from an .art.

    Args:
        art_file: The .art declaring the messages.
        out:      Output dir (``--out``); default per the command.
    """
    argv = ["gen-proto", art_file]
    if out:
        argv += ["--out", out]
    return _run(argv)


@mcp.tool()
def gen_schema(art_file: str, out: str) -> str:
    """Emit ONE combined config-schema JSON (digest + shape) for all the FC
    ``config <Msg>`` nodes — the spine of the migration tooling.

    Args:
        art_file: The system/component .art.
        out:      Output JSON path (``--out``).
    """
    return _run(["gen-schema", art_file, "--out", out])


@mcp.tool()
def gen_netgraph(art_file: str, out: str = "") -> str:
    """Emit a JSON netgraph (nodes + compositions + wiring) for an .art.

    Args:
        art_file: The system/component .art.
        out:      Output JSON path (``--out``); default per the command.
    """
    argv = ["gen-netgraph", art_file]
    if out:
        argv += ["--out", out]
    return _run(argv)


# ── generic escape hatch ───────────────────────────────────────────────────

@mcp.tool()
def gen(command: str, args: list[str] | None = None) -> str:
    """Invoke ANY artheia command by name with a raw argv list. Covers the
    long tail not given a typed tool (gen-can-codec, gen-fibex-codec,
    gen-etcd, gen-config-defaults, import-dbc, audit-manifest, …).

    Args:
        command: A command name from ``list_generators`` (``gen-fc``); a
                 nested ``group/sub`` is split on ``/`` (``executor/emit``).
        args:    Arguments/options exactly as on the CLI, already tokenized
                 (e.g. ``["system/system.art", "--out", "build/schema.json"]``).
    """
    argv = command.split("/") + list(args or [])
    return _run(argv)


def main() -> None:
    # Keep stdout pure JSON-RPC for the stdio transport — no startup banner.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
