"""Artheia command-line interface."""
from __future__ import annotations

import sys
from pathlib import Path  # noqa: F401  (used by --catalog branch)

import click
from textx import TextXError, TextXSemanticError, TextXSyntaxError

from . import __version__
from .generators import (
    generate_cpp_stubs,
    generate_etcd_schema,
    generate_netgraph,
    generate_proto,
)
from .model import parse_file


def _parse(art_file: str):
    try:
        return parse_file(art_file)
    except (TextXSyntaxError, TextXSemanticError, TextXError) as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(2)


@click.group(help="Artheia DSL CLI — host-side DSL for Adaptive-AUTOSAR-style nodes.")
@click.version_option(__version__)
def main() -> None:
    pass


@main.command(help="Parse and validate an .art file. Prints a short summary.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
def parse(art_file: str) -> None:
    model = _parse(art_file)
    click.echo(f"package: {model.name or '<unnamed>'}")
    click.echo(f"elements ({len(model.elements)}):")
    for e in model.elements:
        kind = e.__class__.__name__
        if kind == "GatewayRouteDecl":
            label = f"-> node {e.node.name}"
        else:
            label = getattr(e, "name", "?")
        click.echo(f"  - {kind}: {label}")


@main.command("gen-proto", help="Emit .proto files (one per message).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def gen_proto(art_file: str, out_dir: str) -> None:
    model = _parse(art_file)
    paths = generate_proto(model, out_dir, source_file=art_file)
    for p in paths:
        click.echo(p)


@main.command("gen-netgraph", help="Emit a JSON netgraph describing nodes + compositions.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
@click.option(
    "--catalog",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Gateway catalog JSON (produced by `artheia import-dbc` / "
    "`artheia import-fibex`). When "
    "supplied, gateway_route signal=Foo refs are resolved to bus + addresses.",
)
def gen_netgraph(art_file: str, out_file: str, catalog: str | None) -> None:
    import json as _json
    model = _parse(art_file)
    cat = _json.loads(Path(catalog).read_text()) if catalog else None
    path = generate_netgraph(model, out_file, catalog=cat)
    click.echo(str(path))


@main.command("gen-etcd", help="Emit the etcd seed schema for all node params.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
def gen_etcd(art_file: str, out_file: str) -> None:
    model = _parse(art_file)
    path = generate_etcd_schema(model, out_file)
    click.echo(str(path))


@main.command("gen-cpp-stubs", help="Emit C++ callback-style header stubs (one per node).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def gen_cpp_stubs(art_file: str, out_dir: str) -> None:
    model = _parse(art_file)
    for p in generate_cpp_stubs(model, out_dir, source_file=art_file):
        click.echo(str(p))


@main.command(
    "import-dbc",
    help="Import a DBC file. Emits package.art (one opaque message per "
    "CAN frame) and catalog.json (bus, can_id, dlc, signal layout).",
)
@click.option("--dbc", "dbc_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bus", "bus_name", required=True, help="Bus name, e.g. kcan, hcan.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output directory: vendor/autosar/<bus>/")
@click.option("--csv", "signal_csv", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Optional filter CSV (signal_name,message_name); restricts emission.")
def import_dbc_cmd(dbc_path: str, bus_name: str, out_dir: str, signal_csv: str | None) -> None:
    from .importers import import_dbc
    res = import_dbc(dbc_path, bus_name, out_dir, signal_csv=signal_csv)
    _parse(str(res.art))
    click.echo(f"art:     {res.art}  ({res.frame_count} frames)")
    click.echo(f"catalog: {res.catalog}")


@main.command(
    "import-fibex",
    help="Import a FIBEX cluster file. Emits package.art (one opaque "
    "message per FlexRay frame) and catalog.json (slot, cycle, channel, signal layout).",
)
@click.option("--fibex", "fibex_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bus", "bus_name", required=True, help="Bus name, e.g. mlbevo_gen2_a.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output directory: vendor/autosar/<bus>/")
@click.option("--csv", "signal_csv", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Optional filter CSV (signal_name,message_name); restricts emission.")
def import_fibex_cmd(fibex_path: str, bus_name: str, out_dir: str, signal_csv: str | None) -> None:
    from .importers import import_fibex
    res = import_fibex(fibex_path, bus_name, out_dir, signal_csv=signal_csv)
    _parse(str(res.art))
    click.echo(f"art:     {res.art}  ({res.frame_count} frames)")
    click.echo(f"catalog: {res.catalog}")


if __name__ == "__main__":
    main()
