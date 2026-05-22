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


@main.command("gen-proto-package",
              help="Emit ONE .proto file per .art package at "
                   "<out>/<pkg-path>/<leaf>.proto (mirrors the .art "
                   "package hierarchy; matches the platform/proto/ layout "
                   "used by libgw and apps).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_root", required=True, type=click.Path(file_okay=False))
def gen_proto_package(art_file: str, out_root: str) -> None:
    from .generators.proto_package import generate_package_proto
    path = generate_package_proto(art_file, out_root)
    click.echo(str(path))


@main.command("gen-routing",
              help="Emit per-process routing headers for a composition. "
                   "Each header declares LocalRef<T> for prototypes owned "
                   "by that process and RemoteRef<T, tipc_type, instance> "
                   "for prototypes owned elsewhere. User code calls "
                   "cast/call identically regardless of local vs remote; "
                   "overload resolution picks the path.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--composition", required=True,
              help="Name of the composition to generate routing for.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def gen_routing(art_file: str, composition: str, out_dir: str) -> None:
    from .generators.routing import generate_routing
    paths = generate_routing(art_file, composition, out_dir)
    for p in paths:
        click.echo(str(p))


@main.command("gen-app-composition",
              help="Emit one CMake project per `on process P` partition of a "
                   "composition. Each project boots TimerService + TipcMux + "
                   "local nodes, connects RemoteRefs, registers inbound "
                   "dispatch entries, and runs until SIGINT / DEMO_RUN_MS. "
                   "Node implementations are NOT generated — they come from "
                   "the existing demo_runtime; this generator only emits "
                   "main.cc + CMakeLists per process.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--composition", required=True,
              help="Name of the composition to materialize.")
@click.option("--out", "out_root", required=True, type=click.Path(file_okay=False))
@click.option("--runtime-dir", default="../../demo",
              help="Path to the demo runtime, used by each generated "
                   "CMakeLists as an add_subdirectory target.")
def gen_app_composition(art_file: str, composition: str,
                         out_root: str, runtime_dir: str) -> None:
    from .generators.app_composition import generate_composition
    paths = generate_composition(art_file, composition, out_root,
                                  runtime_dir=runtime_dir)
    for p in paths:
        click.echo(str(p))


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
    "gen-rig",
    help="Bootstrap a vendor rig.py from a top-level .art composition. "
    "Walks `prototype <Node> name on process <P>` lines, groups by "
    "process, and emits SwComponent + Executable + Process factories "
    "plus a SoftwareSpecification delta layer composed against "
    "FcSoftware. Deployment-specific decisions (machine endpoint, "
    "CPU affinity, vehicle identity) are emitted as TODO markers.",
)
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--composition", "-c",
    required=True,
    help="Top-level composition name in the .art file (e.g. Demo3Way).",
)
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Where to write the rig.py.",
)
@click.option(
    "--vehicle-name",
    default=None,
    help="VehicleIdentity.name (default: derive from --out parent dir, "
    "e.g. demo/manifest/ → 'demo').",
)
@click.option(
    "--machine-name",
    default=None,
    help="Default host machine name (default: '<vehicle>_host').",
)
@click.option(
    "--bazel-package",
    default=None,
    help="Bazel package prefix for SwComponent targets (default: '//' "
    "+ vehicle name).",
)
@click.option(
    "--grpc-port",
    type=int,
    default=7700,
    help="Default services/com gRPC port (default: 7700).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing non-empty out path.",
)
def gen_rig(
    art_file: str,
    composition: str,
    out_path: str,
    vehicle_name: str | None,
    machine_name: str | None,
    bazel_package: str | None,
    grpc_port: int,
    force: bool,
) -> None:
    from .generators.rig import write_rig_py

    out = Path(out_path)
    # Default vehicle name from out_path's parent dir name (e.g.
    # demo/manifest/rig.py → "demo").
    if vehicle_name is None:
        parents = list(out.parents)
        # parents[0] is the directory containing rig.py (e.g. manifest/);
        # parents[1] is the rig root (e.g. demo/).
        if len(parents) >= 2 and parents[1].name:
            vehicle_name = parents[1].name
        else:
            click.secho(
                "error: cannot infer --vehicle-name from --out; pass it explicitly",
                fg="red", err=True,
            )
            sys.exit(2)

    if machine_name is None:
        machine_name = f"{vehicle_name}_host"

    if bazel_package is None:
        bazel_package = f"//{vehicle_name}"

    try:
        write_rig_py(
            art_path=Path(art_file),
            composition_name=composition,
            out_path=out,
            vehicle_name=vehicle_name,
            machine_name=machine_name,
            bazel_package=bazel_package,
            grpc_port=grpc_port,
            force=force,
        )
    except FileExistsError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(2)
    except ValueError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(2)

    click.echo(str(out))


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
@click.option("--netgraph", "netgraph_paths", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="netgraph.json (per bus). Joins each receiver port's "
              "interface name (<Pdu>_Iface) with its can_id or slot_id so the "
              "generated dispatch loop can route incoming TIPC frames. "
              "Repeat for multiple buses.")
@click.option("--psp-proto-root", "psp_proto_root", default=None,
              type=click.Path(exists=True, file_okay=False),
              help="Directory containing the PSP's .proto tree "
              "(shared/, flexray/, can/<bus>/). Used to resolve each "
              "PDU's package (e.g. shared_ACC_07 vs mlbevo_gen2_EML_01) "
              "so includes and struct types match nanopb output.")
def gen_app(vendor_root: str, out_dir: str, namespace: str, project_name: str,
            netgraph_paths: tuple[str, ...], psp_proto_root: str | None) -> None:
    from .generators.cpp_app import generate
    results = generate(vendor_root, out_dir,
                       namespace=namespace, project_name=project_name,
                       netgraph_paths=netgraph_paths,
                       psp_proto_root=psp_proto_root)
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


def _resolve_rig(target: str, rig_attr: str | None):
    """Import ``target`` and return its Rig export, materializing
    :class:`SoftwareSpecification` via :meth:`SoftwareSpecification.to_rig`
    when needed.

    Accepts:
      - A direct :class:`Rig` export (legacy path).
      - A :class:`SoftwareSpecification` export (new structured-DSL path) —
        auto-converted via ``.to_rig()``.

    Search order when ``rig_attr`` is None: prefer attributes whose name
    ends in ``*Software`` over ``*Rig`` over ``Rig`` (structured-DSL
    preferred since it's the going-forward shape).
    """
    import importlib

    from artheia.manifest.rig import Rig, SoftwareSpecification

    module = importlib.import_module(target)

    if rig_attr is not None:
        if not hasattr(module, rig_attr):
            click.secho(
                f"error: {target} has no attribute '{rig_attr}'",
                fg="red", err=True,
            )
            sys.exit(2)
        candidate = getattr(module, rig_attr)
    else:
        names = [
            n for n in vars(module)
            if isinstance(getattr(module, n), (Rig, SoftwareSpecification))
        ]
        # Prefer *Software (new shape) > *Rig > bare "Rig" — emit the
        # structured-DSL export when present.
        def _rank(name: str) -> tuple[int, str]:
            if name.endswith("Software"):
                return (0, name)
            if name.endswith("Rig") and name != "Rig":
                return (1, name)
            return (2, name)

        names.sort(key=_rank)
        if not names:
            click.secho(
                f"error: {target} exports no Rig or SoftwareSpecification "
                f"(pass --rig <name>)",
                fg="red", err=True,
            )
            sys.exit(2)
        candidate = getattr(module, names[0])

    if isinstance(candidate, SoftwareSpecification):
        return candidate.to_rig()
    return candidate


@main.command(
    "generate-manifest",
    help="Generate the full deploy manifest YAML for a vehicle rig. TARGET "
    "is a dotted import path to a module exporting a Rig "
    "or SoftwareSpecification "
    "(e.g. vendor.vehicles.tornado.arsyscomp). Distinct from "
    "`executor emit`, which only emits the supervisor tree.",
)
@click.argument("target")
@click.option(
    "--rig",
    "rig_attr",
    default=None,
    help="Name of the Rig / SoftwareSpecification attribute. "
    "Defaults to *Software, then *Rig, then Rig.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the YAML manifest here. Defaults to stdout.",
)
def generate_manifest_cmd(
    target: str,
    rig_attr: str | None,
    out_file: str | None,
) -> None:
    """Run a vendor rig module and emit the full deploy manifest as YAML."""
    import dataclasses
    from enum import Enum
    from ipaddress import IPv4Address, IPv6Address

    import yaml

    rig = _resolve_rig(target, rig_attr)

    # Recursive dataclass→dict that also unwraps Enums and IPvN to strings.
    # asdict() can't be used directly: it doesn't know how to render Enum or
    # IPv4Address into YAML-safe scalars.
    def _serialize(v):
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return {f.name: _serialize(getattr(v, f.name)) for f in dataclasses.fields(v)}
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, (IPv4Address, IPv6Address)):
            return str(v)
        if isinstance(v, (list, tuple)):
            return [_serialize(x) for x in v]
        if isinstance(v, dict):
            return {k: _serialize(x) for k, x in v.items()}
        return v

    doc = _serialize(rig)
    yaml_text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    if out_file is None:
        click.echo(yaml_text, nl=False)
    else:
        Path(out_file).write_text(yaml_text)
        click.echo(out_file)


@main.group("executor", help="Erlang-style executor commands.")
def executor() -> None:
    pass


@executor.command(
    "emit",
    help="Emit the supervisor manifest (executor.yaml) for a vehicle rig. "
    "TARGET is a dotted import path to a module exporting a Rig "
    "(e.g. vendor.vehicles.tornado.arsyscomp).",
)
@click.argument("target")
@click.option(
    "--rig",
    "rig_attr",
    default=None,
    help="Name of the Rig attribute in the module. Defaults to *Rig / Rig.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write the YAML. Defaults to stdout.",
)
def executor_emit(target: str, rig_attr: str | None, out_file: str | None) -> None:
    import yaml

    from artheia.manifest.supervisor import build_supervisor_tree

    rig = _resolve_rig(target, rig_attr)
    tree = build_supervisor_tree(rig)

    def _to_dict(node) -> dict:
        d = {"name": node.name}
        if hasattr(node, "children"):
            d["strategy"] = node.strategy.value
            d["max_restarts"] = node.max_restarts
            d["max_seconds"] = node.max_seconds
            if getattr(node, "tombstone_dir", ""):
                d["tombstone_dir"] = node.tombstone_dir
            d["children"] = [_to_dict(c) for c in node.children]
        else:
            d["start_cmd"] = list(node.start_cmd)
            d["restart"] = node.restart.value
            d["shutdown"] = node.shutdown
            d["type"] = node.type.value
            if node.modules:
                d["modules"] = list(node.modules)
            if node.env:
                d["env"] = dict(node.env)
            if node.working_dir:
                d["working_dir"] = node.working_dir
            if node.shall_run_on:
                d["shall_run_on"] = list(node.shall_run_on)
            if node.shall_not_run_on:
                d["shall_not_run_on"] = list(node.shall_not_run_on)
        return d

    out = yaml.safe_dump(_to_dict(tree), sort_keys=False, default_flow_style=False)
    if out_file is None:
        click.echo(out, nl=False)
    else:
        Path(out_file).write_text(out)
        click.echo(out_file)


# -----------------------------------------------------------------------------
# gui — GUI-side manifests (small endpoint list, one machine per row)
# -----------------------------------------------------------------------------


@main.group("gui", help="Supervisor-GUI manifest commands.")
def gui() -> None:
    pass


@gui.command(
    "emit",
    help="Emit the GUI manifest (machines.yaml) for a vehicle rig. "
    "TARGET is a dotted import path to a module exporting a Rig. "
    "Output lists each Machine's services/com gRPC endpoint — the GUI "
    "opens one gRPC channel per row.",
)
@click.argument("target")
@click.option(
    "--rig",
    "rig_attr",
    default=None,
    help="Name of the Rig attribute in the module. Defaults to *Rig / Rig.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write the YAML. Defaults to stdout.",
)
def gui_emit(target: str, rig_attr: str | None, out_file: str | None) -> None:
    import yaml

    rig = _resolve_rig(target, rig_attr)

    rows: list[dict] = []
    for m in rig.machines:
        ep = getattr(m, "com_endpoint", None)
        if ep is None:
            continue
        rows.append({
            "name": m.name,
            "address": str(ep.address) if ep.address is not None else "127.0.0.1",
            "port": int(ep.port) if ep.port else 7700,
        })

    doc = {"machines": rows}
    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    if out_file is None:
        click.echo(text, nl=False)
    else:
        Path(out_file).write_text(text)
        click.echo(out_file)


if __name__ == "__main__":
    main()
