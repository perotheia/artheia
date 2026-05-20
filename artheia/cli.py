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
    help="Import a DBC file. Emits package.art (message per CAN frame "
    "with scalar signal fields + companion enum decls for value tables) "
    "and catalog.json (bus, can_id, dlc, per-signal layout incl. values).",
)
@click.option("--dbc", "dbc_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bus", "bus_name", required=True, help="Bus name, e.g. kcan, hcan.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output directory: vendor/autosar/<bus>/")
@click.option("--csv", "signal_csv", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Optional filter CSV (signal_name,message_name); restricts emission.")
@click.option("--package", "package_prefix", default="vendor.autosar",
              help="Package prefix for the emitted .art (default: vendor.autosar; "
              "use vendor.<v>.system.autosar when the output lives under a vendor tree).")
@click.option("--validate/--no-validate", default=True,
              help="Round-trip parse the emitted .art (default on). Skip on big "
              "FIBEX outputs — the parse can take minutes.")
def import_dbc_cmd(
    dbc_path: str, bus_name: str, out_dir: str, signal_csv: str | None,
    package_prefix: str, validate: bool,
) -> None:
    from .importers import import_dbc
    res = import_dbc(dbc_path, bus_name, out_dir, signal_csv=signal_csv,
                     package_prefix=package_prefix)
    if validate:
        _parse(str(res.art))
    click.echo(f"art:     {res.art}  ({res.frame_count} frames)")
    click.echo(f"catalog: {res.catalog}")


@main.command(
    "import-fibex",
    help="Import a FIBEX cluster file. Emits package.art (message per "
    "FlexRay frame with scalar signal fields + companion enum decls for "
    "value tables) and catalog.json (slot, cycle, channel, per-signal layout).",
)
@click.option("--fibex", "fibex_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bus", "bus_name", required=True, help="Bus name, e.g. mlbevo_gen2_a.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output directory: vendor/autosar/<bus>/")
@click.option("--csv", "signal_csv", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Optional filter CSV (signal_name,message_name); restricts emission.")
@click.option("--package", "package_prefix", default="vendor.autosar",
              help="Package prefix for the emitted .art (default: vendor.autosar; "
              "use vendor.<v>.system.autosar when the output lives under a vendor tree).")
@click.option("--validate/--no-validate", default=True,
              help="Round-trip parse the emitted .art (default on). Skip on big "
              "FIBEX outputs — the parse can take minutes.")
def import_fibex_cmd(
    fibex_path: str, bus_name: str, out_dir: str, signal_csv: str | None,
    package_prefix: str, validate: bool,
) -> None:
    from .importers import import_fibex
    res = import_fibex(fibex_path, bus_name, out_dir, signal_csv=signal_csv,
                       package_prefix=package_prefix)
    if validate:
        _parse(str(res.art))
    click.echo(f"art:     {res.art}  ({res.frame_count} frames)")
    click.echo(f"catalog: {res.catalog}")


@main.command(
    "gen-codec-dispatch",
    help="Generate dispatch_local.c for libpsp_local.so from a PSP root. "
    "When linked with libcodec.a the linker dead-strips unreferenced encode/decode "
    "symbols. Output is byte-identical to the legacy gen_codec_dispatch.py.",
)
@click.option("--psp-root", required=True, type=click.Path(exists=True, file_okay=False),
              help="Platform support package root (e.g. ../MLBevo_Gen2_cmp_psp).")
@click.option("--csv", "csv_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Signal selection CSV (pdu_name/message_name column). "
              "Omit to generate full dispatch (all messages).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output directory for dispatch_local.c.")
@click.option("--encode-only", is_flag=True,
              help="Generate encode function pointers only (decode=NULL). For capture-only apps.")
@click.option("--decode-only", is_flag=True,
              help="Generate decode function pointers only (encode=NULL). For TX-injection-only apps.")
@click.option("--namespaces", multiple=True, default=None,
              help="Only include these namespaces. Repeat the flag for multiple values.")
def gen_codec_dispatch(
    psp_root: str,
    csv_path: str | None,
    out_dir: str,
    encode_only: bool,
    decode_only: bool,
    namespaces: tuple[str, ...],
) -> None:
    from .generators.codec_dispatch import generate
    try:
        generate(
            psp_root,
            csv_path,
            out_dir,
            encode_only=encode_only,
            decode_only=decode_only,
            namespaces=list(namespaces) if namespaces else None,
        )
    except ValueError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(2)


@main.command(
    "gen-netgraph-partition",
    help="Emit a per-bus netgraph partition (PDU -> bus address LUT) from "
    "an AUTOSAR catalog.json. Output is the routing layer the gateway "
    "runtime joins with symbolic port names to fill the transport header.",
)
@click.option("--catalog", "catalog_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Per-bus catalog.json (output of import-dbc / import-fibex).")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
              help="Output netgraph.json (typically alongside the catalog).")
def gen_netgraph_partition(catalog_path: str, out_path: str) -> None:
    from .generators.netgraph_partition import generate
    generate(catalog_path, out_path)


@main.command(
    "gen-autosar-system",
    help="Emit autosar/<psp>/system/system.art with one mega-node per bus, "
    "each carrying a sender port per PDU. Forward-declares the PDU interfaces "
    "locally so the file parses standalone.",
)
@click.option("--catalog", "catalog_paths", multiple=True, required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Per-bus catalog.json. Repeat for multiple buses (FIBEX + DBC).")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
              help="Output system.art (typically autosar/<psp>/system/system.art).")
@click.option("--package", "package_name", required=True,
              help="Package name for the emitted .art (e.g. autosar.mlbevo_gen2_cmp_psp.system).")
def gen_autosar_system(
    catalog_paths: tuple[str, ...], out_path: str, package_name: str,
) -> None:
    from .generators.autosar_system import generate
    generate(list(catalog_paths), out_path, package_name)


@main.command(
    "gen-host-netgraph",
    help="Walk a platform .art composition, find every tipc-addressed node, "
    "and emit a host_netgraph.json mapping symbolic_port_name -> TIPC address "
    "and port shape. Consumed by the host transport layer (pero_cmp_lnx).",
)
@click.option("--art", "art_paths", multiple=True, required=True,
              type=click.Path(exists=True, dir_okay=False),
              help=".art files declaring TIPC nodes (platform/system/system.art "
              "plus any imported fragments). Repeat per file; nodes are merged.")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
              help="Output host_netgraph.json.")
def gen_host_netgraph(art_paths: tuple[str, ...], out_path: str) -> None:
    from .generators.host_netgraph import generate
    generate(list(art_paths), out_path)


@main.command(
    "gen-app",
    help="Generate a C++14 application scaffold from a vendor system fragment "
    "(three-slice layout: core/, app/, app/impl/, plus CMakeLists.txt). "
    "Re-runs are safe — slice 3 (handlers.cc) is write-once.",
)
@click.option("--vendor-root", required=True,
              type=click.Path(exists=True, file_okay=False),
              help="Vendor system root, e.g. vendor/odd_path_client. "
              "Must contain system/components/*.art with at least one NodeDecl.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output dir, e.g. applications/odd_path_client.")
@click.option("--namespace", default="",
              help="C++ namespace (default: vendor dir name with '-' -> '_').")
@click.option("--project", "project_name", default="",
              help="CMake project name (default: vendor dir name).")
def gen_app(vendor_root: str, out_dir: str, namespace: str, project_name: str) -> None:
    from .generators.cpp_app import generate
    results = generate(vendor_root, out_dir,
                       namespace=namespace, project_name=project_name)
    for path in results.get("wrote", []):
        click.echo(f"  wrote:   {path}")
    for path in results.get("skipped-exists", []):
        click.echo(f"  skipped: {path}  (exists; would overwrite user impl)")


@main.command(
    "gen-signal-filter",
    help="Walk a vendor system tree for gateway_route signal references, "
    "cross-reference against the AUTOSAR catalog, and emit "
    "signal_filter.csv (signal_name,pdu_name) consumed by the gateway codegen.",
)
@click.option("--vendor-root", required=True, type=click.Path(exists=True, file_okay=False),
              help="Vendor root, e.g. vendor/tornado.")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
              help="Output CSV path, e.g. vendor/tornado/config/signal_filter.csv.")
def gen_signal_filter(vendor_root: str, out_path: str) -> None:
    from .generators.signal_filter_csv import generate
    generate(vendor_root, out_path)


@main.command(
    "signal-filter",
    help="Interactive REPL for searching platform signals and building "
    "a signal_filter.csv (formerly tools/psp_signal_filter.py).",
)
@click.option("--config", "config_dir", type=click.Path(exists=True, file_okay=False), default=None,
              help="Auto-discover FIBEX + DBC files in this directory.")
@click.option("--fibex", "fibex_paths", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="FIBEX XML file. Repeat for multiple.")
@click.option("--dbc", "dbc_specs", multiple=True, metavar="PATH:BUS",
              help="DBC file with bus name, e.g. KCAN.dbc:kcan. Repeat for multiple.")
def signal_filter(
    config_dir: str | None, fibex_paths: tuple[str, ...], dbc_specs: tuple[str, ...],
) -> None:
    from .generators.signal_filter import run
    try:
        run(
            config_dir=config_dir,
            fibex_paths=list(fibex_paths),
            dbc_specs=list(dbc_specs),
        )
    except ValueError as e:
        raise click.UsageError(str(e))


@main.command(
    "gen-platform-protos",
    help="Unified FlexRay+CAN codec generator with cross-bus layout deduplication "
    "(formerly tools/gen_platform_protos.py). One pass over FIBEX+DBC produces "
    "shared codec fns + per-bus dispatch tables + proto files.",
)
@click.option("--fibex", type=click.Path(exists=True, dir_okay=False), default=None,
              help="FIBEX XML (FlexRay). Omit for CAN-only.")
@click.option("--dbc", "dbc_specs_raw", multiple=True, metavar="PATH:BUSNAME",
              help="DBC file with bus name, e.g. KCAN.dbc:kcan. Repeat for multiple.")
@click.option("--namespace-fr", default="mlbevo_gen2",
              help="FlexRay proto package namespace (default: mlbevo_gen2).")
@click.option("--out-src", required=True, type=click.Path(file_okay=False))
@click.option("--out-proto", required=True, type=click.Path(file_okay=False))
@click.option("--all-signals", is_flag=True, help="Generate for ALL PDUs/messages (skip CSV).")
@click.option("--csv", "csv_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Signal selection CSV (signal_name,message_name/pdu_name).")
@click.option("--encode-only", is_flag=True)
@click.option("--decode-only", is_flag=True)
def gen_platform_protos(
    fibex: str | None, dbc_specs_raw: tuple[str, ...], namespace_fr: str,
    out_src: str, out_proto: str, all_signals: bool, csv_path: str | None,
    encode_only: bool, decode_only: bool,
) -> None:
    if not fibex and not dbc_specs_raw:
        raise click.UsageError("At least one of --fibex or --dbc must be provided")
    if encode_only and decode_only:
        raise click.UsageError("--encode-only and --decode-only are mutually exclusive")
    if not all_signals and not csv_path:
        click.echo("INFO: No --csv and no --all-signals — defaulting to --all-signals", err=True)
        all_signals = True

    from .generators.platform_protos import generate, _parse_dbc_spec
    dbc_specs = [_parse_dbc_spec(s) for s in dbc_specs_raw]
    generate(
        fibex_path=fibex,
        dbc_specs=dbc_specs,
        namespace_fr=namespace_fr,
        out_src=out_src,
        out_proto=out_proto,
        all_signals=all_signals,
        csv_path=csv_path,
        encode_only=encode_only,
        decode_only=decode_only,
    )


@main.command(
    "gen-fibex-codec",
    help="Generate proto3 + FlexRay decoder/dispatch from a FIBEX + signal CSV "
    "(formerly tools/fibex_to_nanopb.py).",
)
@click.option("--fibex", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--csv", "csv_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Signal selection CSV. Omit with --all-signals.")
@click.option("--namespace", required=True, help="Namespace / library name (e.g. mlbevo_gen2).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
@click.option("--proto-out", "proto_out", type=click.Path(file_okay=False), default=None,
              help="Output dir for .proto files (default: same as --out).")
@click.option("--all-signals", is_flag=True, help="Generate for ALL APPLICATION PDUs (skip CSV).")
def gen_fibex_codec(
    fibex: str, csv_path: str | None, namespace: str, out_dir: str,
    proto_out: str | None, all_signals: bool,
) -> None:
    if not all_signals and not csv_path:
        raise click.UsageError("--csv is required unless --all-signals is given")
    from .generators.fibex_to_nanopb import generate
    generate(
        fibex_path=fibex,
        csv_path=csv_path,
        namespace=namespace,
        out_dir=out_dir,
        proto_out=proto_out or out_dir,
        all_signals=all_signals,
    )


@main.command(
    "gen-can-codec",
    help="Generate proto3 + CAN encoder/decoder from a DBC + signal CSV "
    "(formerly tools/can_to_nanopb.py).",
)
@click.option("--dbc", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--csv", "csv_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Signal selection CSV. Omit with --all-signals.")
@click.option("--namespace", required=True, help="Proto package namespace (e.g. can_kcan).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
@click.option("--proto-out", "proto_out", type=click.Path(file_okay=False), default=None,
              help="Output dir for .proto files (default: same as --out).")
@click.option("--all-signals", is_flag=True, help="Generate for ALL messages (skip CSV).")
@click.option("--include", "include_dir", type=click.Path(exists=True, file_okay=False), default=None,
              help="pero_cmp_lnx lib/include path (for cmp_plugin.h).")
def gen_can_codec(
    dbc: str, csv_path: str | None, namespace: str, out_dir: str,
    proto_out: str | None, all_signals: bool, include_dir: str | None,
) -> None:
    if not all_signals and not csv_path:
        raise click.UsageError("--csv is required unless --all-signals is given")
    from .generators.can_to_nanopb import generate
    generate(dbc, csv_path, namespace, out_dir, all_signals,
             proto_out or out_dir, plugin_include_dir=include_dir)


@main.command(
    "gen-app-dispatch",
    help="Generate per-application dispatch glue (dispatch_table.{c,h}, "
    "hercules_filter.h, ns_wrapper.h) from PSP manifests + signal CSV.",
)
@click.option("--psp-root", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--csv", "csv_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="App signal CSV (signal_name,pdu_name or signal_name,message_name).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def gen_app_dispatch(psp_root: str, csv_path: str, out_dir: str) -> None:
    from .generators.app_dispatch import generate
    generate(psp_root, csv_path, out_dir)


@main.command(
    "gen-gw-types",
    help="Generate gw_bus_types.h from PSP manifests (stable GwBusId enum + helpers).",
)
@click.option("--psp-root", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def gen_gw_types(psp_root: str, out_dir: str) -> None:
    from .generators.gw_types import generate
    generate(psp_root, out_dir)


@main.command(
    "gen-psp-registry",
    help="Generate psp_can_registry.{c,h} that aggregates CAN namespaces into "
    "a single psp_can_lookup() entry point for libpsp.so.",
)
@click.option("--can-namespaces", required=True, multiple=True,
              help="CAN namespace, e.g. can_kcan. Repeat for multiple.")
@click.option("--include", "include_dir", required=True,
              type=click.Path(exists=True, file_okay=False),
              help="pero_cmp_lnx lib/include path (for cmp_plugin.h).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def gen_psp_registry(
    can_namespaces: tuple[str, ...], include_dir: str, out_dir: str
) -> None:
    from .generators.psp_registry import generate
    generate(list(can_namespaces), include_dir, out_dir)


if __name__ == "__main__":
    main()
