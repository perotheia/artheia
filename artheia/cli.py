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


@main.command(
    help="Parse and print an .art file as a tree.\n\n"
         "Walks the merged model (package.art + component.art) and prints\n"
         "clusters → compositions → nodes → ports → messages, tree(1)-style.\n\n"
         "By default, empty-body forward-decls (`cluster Services { }`,\n"
         "`composition Supervisor { }`) are resolved RECURSIVELY: the parser\n"
         "scans the directory containing the input file (following symlinks)\n"
         "for the real definition and substitutes it in. Unresolved\n"
         "forward-decls are an error.\n\n"
         "Flags mirror the unix `tree` command:\n"
         "  -L <depth>     cap recursion depth (1 = top-level elements only)\n"
         "  -d             only show 'container' nodes — clusters, compositions, nodes\n"
         "  -f <FQN>       show only the subtree rooted at FQN\n"
         "  --no-recurse   leave forward-decls unresolved (single-file view)"
)
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-L", "depth", type=int, default=None,
              help="Max tree depth (1 = top-level only). Default: unlimited.")
@click.option("-d", "only_containers", is_flag=True, default=False,
              help="Container-only mode: hide leaf details (ports, fields, "
                   "connects, operations).")
@click.option("-f", "fqn_filter", type=str, default=None,
              help="Filter: print only the subtree rooted at this FQN. "
                   "Match is against `<package>.<element-name>`.")
@click.option("--no-recurse", "no_recurse", is_flag=True, default=False,
              help="Leave forward-decls unresolved (single-file view).")
def parse(art_file: str, depth, only_containers: bool, fqn_filter,
          no_recurse: bool) -> None:
    model = _parse(art_file)
    resolved = {} if no_recurse else _resolve_forward_decls(art_file, model)
    _print_tree(model, max_depth=depth, only_containers=only_containers,
                fqn_filter=fqn_filter, resolved=resolved)


# ---------------------------------------------------------------------------
# Forward-decl resolver: scan the workspace for real definitions.
# ---------------------------------------------------------------------------


def _workspace_root_for(entry: "Path", model) -> "Path":
    """Derive the workspace root from the entry file's location and
    declared package.

    The directory layout mirrors the package FQN: a file declaring
    ``package system.demo`` lives in a directory named ``demo/``
    inside a parent named ``system/``. Walk up from the entry file's
    directory, popping FQN segments as we go; the first ancestor that
    DOESN'T match the next-up segment is the root.

    Examples:
      entry = platform/system/system.art, package = system
        dir = platform/system/  (name 'system' = pkg segment 'system')
        walk up → platform/  (no more segments) → root
      entry = services/system/log/package.art, package = system.services.log
        dir = services/system/log/ (name 'log' = pkg seg 'log')
        up → services/system/      (name 'system' = pkg seg 'services'? NO)
        actually: dir names walked = [log, system, services] but pkg
        segments in tail order are [log, services, system]. The
        platform tree uses `services/system/` not `services/`, so the
        cleanest rule for our convention is: root = first ancestor
        whose name is NOT a package segment.
    """
    from pathlib import Path

    pkg_name = getattr(model, "name", None) or ""
    segments = pkg_name.split(".") if pkg_name else []

    # Walk up from entry's dir; pop segments from the FQN tail until
    # mismatch.
    d = entry.parent
    tail = list(reversed(segments))
    while tail and d.name == tail[0]:
        tail.pop(0)
        d = d.parent

    return d


def _package_dir(root: "Path", fqn: str) -> "Path | None":
    """Map a package FQN to its directory under *root*.

    ``system.demo`` + root=``platform`` → ``platform/system/demo``.
    """
    parts = fqn.split(".")
    p = root
    for seg in parts:
        p = p / seg
    return p


def _is_stub(el) -> bool:
    """True if *el* is an empty-body cluster or composition (= forward-decl).

    The user's convention: stubs at the top of an aggregator file
    declare names that live in some other component's component.art.
    Any non-empty body means this IS the real definition.
    """
    kind = type(el).__name__
    if kind == "ClusterDecl":
        return not list(getattr(el, "elements", []))
    if kind == "CompositionDecl":
        return not list(getattr(el, "elements", []))
    return False


def _resolve_forward_decls(art_file: str, model) -> dict:
    """Resolve every forward-decl in *model* by following the
    ``import`` statements at the top of the file (and transitively).
    Returns ``{id(stub) -> real_element}``. Strict: any unresolved
    stub raises.

    Resolution strategy — *no* workspace scan, *no* grep heuristic:

    1. The model's ``import system.x.y.*`` lines name packages we
       expect to find definitions in.
    2. Package FQN maps one-to-one to a directory under the
       *workspace root*: ``system.x.y`` → ``<root>/x/y/``.
    3. Workspace root is derived from *art_file* by walking up the
       parent chain while the directory name matches the package
       FQN's segments. For ``platform/system/system.art`` (package
       ``system``), root is ``platform/`` (the dir whose child
       ``system/`` matches the package's first segment).
    4. Each imported directory's ``package.art`` is parsed (the
       merged loader auto-includes its sibling ``component.art``).
    5. Each imported file's own ``import`` lines are followed
       transitively.

    Only files reachable from the entry file's import graph are
    parsed — irrelevant workspace neighbours (e.g. AUTOSAR vendor
    PSP catalogs) are never touched.
    """
    from pathlib import Path

    entry = Path(art_file).resolve()
    root = _workspace_root_for(entry, model)

    by_name: dict[str, list[tuple[Path, object]]] = {}
    visited_packages: set[str] = set()

    def _absorb(other_model, other_path: Path) -> None:
        """Add other_model's non-stub clusters/compositions to the
        index, then follow ITS imports too."""
        for e in getattr(other_model, "elements", []):
            kind = type(e).__name__
            if kind not in ("ClusterDecl", "CompositionDecl"):
                continue
            if _is_stub(e):
                continue
            by_name.setdefault(e.name, []).append((other_path, e))
        for imp in getattr(other_model, "imports", []) or []:
            _follow(imp.name)

    def _follow(import_fqn: str) -> None:
        """Resolve an ``import <FQN>`` to a file and absorb it."""
        # Drop the trailing `.*` wildcard if present — the FQN
        # always names a package (= directory) for us.
        pkg = import_fqn[:-2] if import_fqn.endswith(".*") else import_fqn
        if pkg in visited_packages:
            return
        visited_packages.add(pkg)

        pkg_dir = _package_dir(root, pkg)
        if pkg_dir is None or not pkg_dir.is_dir():
            click.secho(
                f"error: import {import_fqn!r}: no directory "
                f"{root}/{pkg.replace('.', '/')}",
                fg="red", err=True,
            )
            sys.exit(2)

        # File-name priority within the package dir:
        #   - system.art       (aggregator: top-of-subtree files like
        #                       platform/system/services/system.art)
        #   - package.art      (schema layer; loader auto-merges
        #                       sibling component.art)
        #   - component.art    (wiring layer; loader auto-merges
        #                       sibling package.art)
        candidate = None
        for fname in ("system.art", "package.art", "component.art"):
            if (pkg_dir / fname).exists():
                candidate = pkg_dir / fname
                break
        if candidate is None:
            click.secho(
                f"error: import {import_fqn!r}: no system.art / "
                f"package.art / component.art under {pkg_dir}",
                fg="red", err=True,
            )
            sys.exit(2)

        if candidate.resolve() == entry:
            return  # already loaded — that's the entry model itself
        try:
            other = _parse(str(candidate))
        except SystemExit:
            return  # diagnostic already printed
        _absorb(other, candidate)

    # Seed the recursion with the entry file's imports.
    for imp in getattr(model, "imports", []) or []:
        _follow(imp.name)

    # 4. Walk the input model and resolve every stub against the index.
    resolved: dict = {}
    unresolved: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, list[str]]] = []

    def _resolve(stub):
        if not _is_stub(stub):
            return
        candidates = by_name.get(stub.name, [])
        if not candidates:
            unresolved.append((type(stub).__name__, stub.name))
            return
        if len(candidates) > 1:
            ambiguous.append(
                (stub.name, [str(p) for p, _ in candidates])
            )
            return
        _path, real = candidates[0]
        resolved[id(stub)] = real

    # Top-level forward-decls.
    for e in model.elements:
        _resolve(e)

    # Cluster members may reference stub compositions. The
    # ClusterMember.type is the cross-ref to a CompositionDecl.
    # Walk transitively: substituting a cluster reveals new
    # ClusterMembers whose .type refs may themselves be stubs in some
    # other file (e.g. services/system/system.art's `composition Com
    # { }` stubs that `cluster Services` references).
    visited_clusters: set[int] = set()
    queue: list = []
    for e in model.elements:
        if type(e).__name__ == "ClusterDecl":
            queue.append(e)

    while queue:
        cluster = queue.pop()
        # Use the resolved version if we already substituted it.
        cluster = resolved.get(id(cluster), cluster)
        if id(cluster) in visited_clusters:
            continue
        visited_clusters.add(id(cluster))
        for member in getattr(cluster, "elements", []):
            if type(member).__name__ != "ClusterMember":
                continue
            comp = member.type
            _resolve(comp)
            # The composition we just resolved (or the in-tree one)
            # may itself contain CompositionRefDecls or nested
            # clusters in future grammars — not today, but the queue
            # is here for that. For now, just descend into nested
            # ClusterDecls inside the real composition (defensive).
            real = resolved.get(id(comp), comp)
            for sub in getattr(real, "elements", []):
                if type(sub).__name__ == "ClusterDecl":
                    queue.append(sub)

    if ambiguous:
        msg = ["ambiguous forward-decls (multiple real definitions found):"]
        for name, paths in ambiguous:
            msg.append(f"  {name}:")
            for p in paths:
                msg.append(f"    {p}")
        click.secho("\n".join(msg), fg="red", err=True)
        sys.exit(2)

    if unresolved:
        msg = ["unresolved forward-decls (no real definition found):"]
        for kind, name in unresolved:
            tag = "cluster" if kind == "ClusterDecl" else "composition"
            msg.append(f"  {tag} {name}")
        msg.append(
            f"\nsearched under: {root}\n"
            f"hint: add the missing component to the workspace, or use "
            f"`--no-recurse` to print the file standalone."
        )
        click.secho("\n".join(msg), fg="red", err=True)
        sys.exit(2)

    return resolved


# ---------------------------------------------------------------------------
# Tree printer for `artheia parse`.
# ---------------------------------------------------------------------------


# tree(1)-style box drawing.
_BRANCH = "├── "
_LAST   = "└── "
_VBAR   = "│   "
_BLANK  = "    "


def _print_tree(model, *, max_depth, only_containers: bool, fqn_filter,
                resolved: dict):
    """Walk a parsed Artheia model and print it tree-style.

    *resolved* maps ``id(stub_element) -> real_element`` so the
    printer can substitute forward-decls with their workspace-wide
    definitions. Empty dict = no substitution.

    De-duplication rule: a top-level composition/node that is reached
    via a cluster member or a prototype reference does NOT print at
    the top level — only inside its consumer. Keeps the tree a tree.
    """
    pkg = model.name or "<unnamed>"
    click.echo(pkg)

    # Filter top-level elements to those that match --fqn (if any).
    top = list(model.elements)
    if fqn_filter:
        # FQN := "<package>.<name>" — match either fully-qualified or
        # bare-name form.
        bare = fqn_filter.split(".")[-1]
        top = [
            e for e in top
            if getattr(e, "name", None) == bare
            or f"{pkg}.{getattr(e, 'name', '')}" == fqn_filter
        ]
        if not top:
            click.secho(
                f"(no element matches --fqn {fqn_filter!r})",
                fg="yellow", err=True,
            )
            return

    # Drop leaves when -d.
    if only_containers:
        top = [e for e in top if _is_container(e)]

    # Substitute top-level forward-decls with their resolved real defs.
    top = [resolved.get(id(e), e) for e in top]

    # Build the "referenced elsewhere" set — compositions reached via
    # ClusterMember.type, nodes reached via PrototypeDecl.type. Those
    # should not also print at the top level (it would make the tree
    # a DAG and double-print the same subtree).
    referenced = _collect_referenced(top, resolved)
    top = [el for el in top if id(el) not in referenced]

    # Walk.
    n = len(top)
    for i, el in enumerate(top):
        last = (i == n - 1)
        _print_element(el, prefix="", is_last=last, depth=1,
                       max_depth=max_depth, only_containers=only_containers,
                       resolved=resolved)


def _collect_referenced(top: list, resolved: dict) -> set:
    """Return the set of ``id(element)`` for every composition/node
    that is reached as a child of any cluster member or prototype in
    the tree rooted at *top*. Caller hides these at top level.
    """
    seen: set[int] = set()
    visited_clusters: set[int] = set()
    visited_comps: set[int] = set()

    def visit_cluster(cluster):
        if id(cluster) in visited_clusters:
            return
        visited_clusters.add(id(cluster))
        for sub in getattr(cluster, "elements", []):
            if type(sub).__name__ != "ClusterMember":
                continue
            # The member's referenced composition (after resolution)
            # is being absorbed into the cluster body — hide it at
            # top level. Mark both the stub (for the rare case where
            # someone references the stub) and the real one.
            real_comp = resolved.get(id(sub.type), sub.type)
            seen.add(id(real_comp))
            seen.add(id(sub.type))
            visit_composition(real_comp)

    def visit_composition(comp):
        if id(comp) in visited_comps:
            return
        visited_comps.add(id(comp))
        for sub in getattr(comp, "elements", []):
            if type(sub).__name__ == "PrototypeDecl":
                seen.add(id(sub.type))

    for el in top:
        if type(el).__name__ == "ClusterDecl":
            visit_cluster(el)
        elif type(el).__name__ == "CompositionDecl":
            visit_composition(el)

    return seen


def _is_container(el) -> bool:
    """True for cluster/composition/node — the 'directory-like' kinds."""
    return type(el).__name__ in (
        "ClusterDecl", "CompositionDecl", "NodeDecl",
    )


def _print_element(el, *, prefix: str, is_last: bool, depth: int,
                   max_depth, only_containers: bool, resolved: dict) -> None:
    """Emit one element plus its children."""
    branch = _LAST if is_last else _BRANCH
    line = _summarize(el, resolved=resolved)
    click.echo(f"{prefix}{branch}{line}")

    if max_depth is not None and depth >= max_depth:
        return

    child_prefix = prefix + (_BLANK if is_last else _VBAR)
    children = _children(el, only_containers=only_containers,
                         resolved=resolved)
    n = len(children)
    for i, c in enumerate(children):
        _print_element(c, prefix=child_prefix, is_last=(i == n - 1),
                       depth=depth + 1, max_depth=max_depth,
                       only_containers=only_containers, resolved=resolved)


def _summarize(el, *, resolved: dict | None = None) -> str:
    """One-line tree label for an element or sub-element."""
    kind = type(el).__name__
    if kind == "ClusterDecl":
        return f"cluster {el.name}"
    if kind == "CompositionDecl":
        return f"composition {el.name}"
    if kind == "NodeDecl":
        return f"node atomic {el.name}"
    if kind == "MessageDecl":
        return f"message {el.name}"
    if kind == "EnumDecl":
        return f"enum {el.name}"
    if kind == "SenderReceiverInterface":
        return f"interface senderReceiver {el.name}"
    if kind == "ClientServerInterface":
        return f"interface clientServer {el.name}"
    if kind == "BusDecl":
        return f"bus {el.name} kind={el.kind}"
    if kind == "GatewayRouteDecl":
        return f"gateway_route -> node {el.node.name}"
    # Inside-element kinds.
    if kind == "ClusterMember":
        return f"composition {_qualify(el.type)} {el.name}"
    if kind == "ClusterConnect":
        return _connect_summary(el)
    if kind == "PrototypeDecl":
        proc = f" on process {el.process}" if el.process else ""
        return f"prototype {el.type.name} {el.name}{proc}"
    if kind == "ConnectDecl":
        return _connect_summary(el)
    if kind == "TipcAddress":
        return f"tipc type={_hex(el.type)} instance={_hex(el.instance)}"
    if kind in ("SenderPort", "ReceiverPort", "ServerPort", "ClientPort"):
        return _port_summary(el)
    if kind == "MessageField":
        rep = "repeated " if el.repeated else ""
        type_str = _field_type(el.type)
        return f"{rep}{type_str} {el.name}"
    if kind == "EnumValue":
        return f"{el.name} = {el.number}"
    if kind == "DataElement":
        return f"data {el.type.name} {el.name}"
    if kind == "OperationDecl":
        params = ", ".join(
            f"{p.direction} {p.name}:{p.type.name}" for p in el.params
        )
        ret = f" returns {el.returns.name}" if el.returns else ""
        return f"operation {el.name}({params}){ret}"
    if kind == "NodeParam":
        return f"param {el.name}:{el.type} = {el.default.value}"
    return f"{kind} {getattr(el, 'name', '?')}"


def _connect_summary(el) -> str:
    s, t = el.source, el.target
    sp = s.proto.name if hasattr(s.proto, "name") else str(s.proto)
    tp = t.proto.name if hasattr(t.proto, "name") else str(t.proto)
    return f"connect {sp}.{s.port} -> {tp}.{t.port}"


def _port_summary(el) -> str:
    kind = type(el).__name__
    iface_name = getattr(el.iface, "name", "?")
    rel = ""
    if hasattr(el, "reliability") and el.reliability:
        rel = f" {el.reliability}"
    if kind == "SenderPort":
        return f"sender {el.name} provides {iface_name}{rel}"
    if kind == "ReceiverPort":
        return f"receiver {el.name} requires {iface_name}{rel}"
    if kind == "ServerPort":
        return f"server {el.name} provides {iface_name}"
    if kind == "ClientPort":
        return f"client {el.name} requires {iface_name}"
    return kind


def _field_type(ft) -> str:
    if getattr(ft, "kind", None):
        return ft.kind
    return getattr(ft.ref, "name", "?")


def _hex(v) -> str:
    """Format an int as 0x… (textX gives us the parsed string already)."""
    if isinstance(v, str):
        return v
    try:
        return f"0x{int(v):x}"
    except (TypeError, ValueError):
        return str(v)


def _qualify(comp_decl) -> str:
    """Reconstruct `<package>.<name>` from a CompositionDecl ref."""
    name = comp_decl.name
    parent = getattr(comp_decl, "parent", None)
    while parent is not None and type(parent).__name__ != "Model":
        parent = getattr(parent, "parent", None)
    if parent is None or not getattr(parent, "name", None):
        return name
    return f"{parent.name}.{name}"


def _children(el, *, only_containers: bool, resolved: dict | None = None):
    """Return the sub-elements to descend into for this element.

    *resolved* (id → real_element) lets us walk into the real
    definition of a forward-decl: a ClusterMember pointing at a stub
    composition descends into the real composition's body instead.
    """
    resolved = resolved or {}
    kind = type(el).__name__
    out: list = []
    if kind == "ClusterDecl":
        # Cluster's body: ClusterMembers + ClusterConnects (mixed in
        # source order, accessible via .elements).
        for sub in getattr(el, "elements", []):
            t = type(sub).__name__
            if only_containers and t == "ClusterConnect":
                continue
            out.append(sub)
    elif kind == "ClusterMember":
        # Descend into the referenced composition's body — using the
        # resolved (real) composition if the in-tree one is a stub.
        comp = resolved.get(id(el.type), el.type)
        for sub in getattr(comp, "elements", []):
            t = type(sub).__name__
            if only_containers and t in ("ConnectDecl", "CompositionRefDecl"):
                continue
            out.append(sub)
    elif kind == "PrototypeDecl":
        # A prototype IS a node — descend into the node's body so the
        # tree reaches ports/tipc/params at deep -L levels.
        out.append(el.type)
    elif kind == "CompositionDecl":
        for sub in getattr(el, "elements", []):
            t = type(sub).__name__
            if only_containers and t in ("ConnectDecl", "CompositionRefDecl"):
                continue
            out.append(sub)
    elif kind == "NodeDecl":
        if not only_containers and getattr(el, "tipc", None):
            out.append(el.tipc)
        if not only_containers:
            for p in getattr(el, "ports", []) or []:
                out.append(p)
            for p in getattr(el, "params", []) or []:
                out.append(p)
    elif kind == "MessageDecl":
        if not only_containers:
            for f in getattr(el, "fields", []) or []:
                out.append(f)
    elif kind == "EnumDecl":
        if not only_containers:
            for v in getattr(el, "values", []) or []:
                out.append(v)
    elif kind in ("SenderReceiverInterface",):
        if not only_containers:
            for d in getattr(el, "data", []) or []:
                out.append(d)
    elif kind == "ClientServerInterface":
        if not only_containers:
            for op in getattr(el, "operations", []) or []:
                out.append(op)
    return out


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
    "gen-trace-decoder-subset",
    help="Emit the per-rig trace-decoder subset .cc. Walks the cluster "
    "netgraph + PSP netgraph for the union of message types this rig "
    "actually uses; emits a registration table that drives "
    "libtrace_decoder.so. PSP has ~4500 messages but a real rig uses ~100 — "
    "this slims the .so accordingly. Consumed by supdbg + supervisor-gui's "
    "trace panel; the rf-theia Robot trace adapter uses py-proto reflection "
    "directly and does NOT need this output.",
)
@click.option(
    "--cluster-netgraph",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Cluster netgraph JSON (output of `artheia gen-netgraph`).",
)
@click.option(
    "--psp-netgraph",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="PSP / gateway netgraph JSON (output of `artheia gen-psp-netgraph`).",
)
@click.option(
    "--out",
    "out_file",
    required=True,
    type=click.Path(dir_okay=False),
    help="Where to write the generated .cc (e.g. "
    "platform/runtime/trace_decoder/generated/decoders.cc).",
)
def gen_trace_decoder_subset(
    cluster_netgraph: str | None,
    psp_netgraph: str | None,
    out_file: str,
) -> None:
    from .generators.trace_decoder_subset import generate as _gen
    p = _gen(
        cluster_netgraph=cluster_netgraph,
        psp_netgraph=psp_netgraph,
        out_file=out_file,
    )
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
    "gen-psp-netgraph",
    help="Emit a per-bus PSP netgraph (PDU -> bus address LUT) from "
    "an AUTOSAR catalog.json. The GATEWAY daemon consumes this at "
    "startup — it's the authority on CAN/FlexRay routing (translates "
    "TIPC ↔ bus wire). Loaded as JSON, not compiled in, so partial "
    "orchestration ships a new netgraph.json without reinstalling "
    "the gateway binary.\n\n"
    "Two netgraphs total in the system:\n"
    "  - PSP netgraph (this command) → gateway daemon (active routing)\n"
    "  - Cluster netgraph (`gen-netgraph`) → supervisor (passive, GUI/stats)\n\n"
    "Previously called `gen-gateway-netgraph` / `gen-netgraph-partition` — "
    "renamed to reflect the format (PSP = bus-side address table) rather "
    "than the consumer.",
)
@click.option("--catalog", "catalog_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Per-bus catalog.json (output of import-dbc / import-fibex).")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
              help="Output netgraph.json (typically alongside the catalog).")
def gen_psp_netgraph(catalog_path: str, out_path: str) -> None:
    from .generators.psp_netgraph import generate
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
    help="Generate a C++ application scaffold. Two modes:\n\n"
    "  --kind psp (default): vendor PSP / signal-routing app (the "
    "existing three-slice cpp_app generator). Targets vendor-root "
    "trees like vendor/odd_path_client/system/components/.\n\n"
    "  --kind fc: single-file Adaptive Functional Cluster. Targets a "
    "single services/system/<fc>/package.art. Emits the lib / main / "
    "impl slices for a GenServer-derived daemon plus manifest.py + "
    "executor.py + the .proto under platform/proto/.",
)
@click.option("--kind", type=click.Choice(["psp", "fc"]), default="psp",
              help="Generator mode (default: psp).")
# --- shared --
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Output dir. For psp mode: applications/<vendor>/. "
              "For fc mode: services/system/<fc>/.")
# --- psp-mode flags --
@click.option("--vendor-root", default=None,
              type=click.Path(exists=True, file_okay=False),
              help="(psp mode) Vendor system root, e.g. vendor/odd_path_client. "
              "Must contain system/components/*.art.")
@click.option("--namespace", default="",
              help="(psp mode) C++ namespace (default: vendor dir name).")
@click.option("--project", "project_name", default="",
              help="(psp mode) CMake project name (default: vendor dir name).")
@click.option("--netgraph", "netgraph_paths", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="(psp mode) netgraph.json per bus. Joins receiver port "
              "interfaces to their bus addresses.")
@click.option("--psp-proto-root", "psp_proto_root", default=None,
              type=click.Path(exists=True, file_okay=False),
              help="(psp mode) Pre-existing PSP .proto tree to resolve "
              "PDU packages against.")
# --- fc-mode flags --
@click.argument("art_file", required=False,
                type=click.Path(exists=True, dir_okay=False))
@click.option("--manifest-out", "manifest_out", default=None,
              type=click.Path(file_okay=False),
              help="(fc mode) Where manifest.py + executor.py land "
              "(typically manifest/services/<fc>/).")
@click.option("--proto-out", "proto_out", default=None,
              type=click.Path(file_okay=False),
              help="(fc mode) Where the generated .proto lands "
              "(typically platform/proto/). The .proto goes under "
              "<proto-out>/<art-pkg-as-path>/<leaf>.proto. After this "
              "step, run nanopb_generator on the .proto to emit .pb.{c,h}.")
@click.option("--force", is_flag=True, default=False,
              help="(fc mode) Overwrite write-once slices (impl + "
              "executor.py).")
@click.option("--ns", "cxx_namespace", default=None,
              help="(fc mode) C++ namespace for the generated daemon, "
              "accepts nested colon-colon segments. Examples: "
              "`--ns ara::sm` for AUTOSAR Adaptive FC conformity, "
              "`--ns vendor::myapp` for vendor scaffolding. Default: "
              "the .art package as one underscore-flat identifier "
              "(e.g. `system_services_sm`).")
def gen_app(kind: str,
            out_dir: str,
            vendor_root: str | None,
            namespace: str,
            project_name: str,
            netgraph_paths: tuple[str, ...],
            psp_proto_root: str | None,
            art_file: str | None,
            manifest_out: str | None,
            proto_out: str | None,
            force: bool,
            cxx_namespace: str | None) -> None:
    if kind == "psp":
        if not vendor_root:
            click.secho("error: --vendor-root is required for --kind psp",
                        fg="red", err=True)
            sys.exit(2)
        from .generators.cpp_app import generate
        results = generate(vendor_root, out_dir,
                           namespace=namespace, project_name=project_name,
                           netgraph_paths=netgraph_paths,
                           psp_proto_root=psp_proto_root)
    else:  # fc
        if not art_file:
            click.secho(
                "error: --kind fc requires an .art file as positional arg",
                fg="red", err=True)
            sys.exit(2)
        from .generators.fc_app import generate_fc
        results = generate_fc(art_file, out_dir,
                              manifest_out=manifest_out,
                              proto_out=proto_out,
                              cxx_namespace=cxx_namespace,
                              force=force)
    for path in results.get("wrote", []):
        click.echo(f"  wrote:      {path}")
    for path in results.get("overwrote", []):
        click.echo(f"  overwrote:  {path}")
    for path in results.get("skipped-exists", []):
        click.echo(f"  skipped:    {path}  (exists; --force to overwrite)")


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
    "audit-manifest",
    help="Left-join an .art system tree against a vendor rig.py and "
    "report manifest gaps. Walks every cluster/composition declared "
    "in ART_FILE (transitively via `import` statements) and checks "
    "that the rig.py module declares matching SwComponent/Application/"
    "Process entries.\n\n"
    "RIG_TARGET is a dotted module path, like `demo.manifest.rig`.",
)
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("rig_target")
@click.option(
    "--rig",
    "rig_attr",
    default=None,
    help="Name of the Rig / SoftwareSpecification attribute in the module.",
)
def audit_manifest_cmd(art_file: str, rig_target: str,
                       rig_attr: str | None) -> None:
    """Report what the rig.py is missing relative to .art declarations.

    Three checks, in left-join shape (.art → rig.py):

    1. **Cluster member → ApplicationManifest**: every
       ``composition <CompFQN> <ident>`` line inside a cluster expects
       an ``ApplicationManifest(name=<ident>)`` in the rig — or at
       minimum a ``SwComponent`` whose ``art_node`` points at the
       composition. Otherwise the cluster's .ipk has no manifest
       counterpart and Puppet won't know where to deploy it.

    2. **Composition → SwComponent**: every non-stub
       ``composition X { ... }`` should have at least one
       ``SwComponent`` whose ``art_node`` ends with ``/X`` — that
       SwComponent's bazel_target is what gets built into the binary.

    3. **Prototype-with-process → Process**: every
       ``prototype NodeT name on process P#`` annotation should have
       a ``Process(name=<name>)`` in the rig (or its supervisor
       tree). Missing here means the supervisor won't spawn it.

    Exit code: 0 if all present; 1 if gaps found.
    """
    model = _parse(art_file)
    resolved = _resolve_forward_decls(art_file, model)

    # Collect what the .art tree declares.
    clusters: list = []                      # list of ClusterDecl
    compositions_by_name: dict[str, object] = {}   # name → CompositionDecl
    prototypes_with_process: list[tuple[str, str, str]] = []
    # (composition_name, prototype_ident, process_id)

    def _walk(el, seen: set):
        if id(el) in seen:
            return
        seen.add(id(el))
        kind = type(el).__name__
        # Substitute stubs with resolved real defs.
        real = resolved.get(id(el), el)
        if id(real) != id(el) and id(real) not in seen:
            _walk(real, seen)
            return
        if kind == "ClusterDecl":
            clusters.append(el)
            for sub in getattr(el, "elements", []):
                if type(sub).__name__ == "ClusterMember":
                    _walk(sub.type, seen)  # the composition it refers to
        elif kind == "CompositionDecl":
            if el.name and getattr(el, "elements", []):
                compositions_by_name.setdefault(el.name, el)
            for sub in getattr(el, "elements", []):
                if type(sub).__name__ == "PrototypeDecl":
                    proc = getattr(sub, "process", None)
                    if proc:
                        prototypes_with_process.append(
                            (el.name, sub.name, proc),
                        )

    for el in model.elements:
        _walk(el, set())

    # Load the rig.
    try:
        rig = _resolve_rig(rig_target, rig_attr)
    except SystemExit:
        return

    sw_components = [c for app in rig.applications for c in app.components]
    apps_by_name = {a.name: a for a in rig.applications}
    processes_by_name = {p.name: p for p in rig.execution_manifests}

    def _has_sw_for_composition(comp_name: str) -> bool:
        """True if any SwComponent's `art_node` matches this composition
        — either directly (``.../<CompName>``) or via one of the node
        types prototyped inside it (``.../<NodeType>``).

        The looser match handles the convention where SwComponents
        point at the daemon NODE (e.g. ``services.com/ComDaemon``)
        even though they implement the COMPOSITION (``Com``).
        """
        comp = compositions_by_name.get(comp_name)
        prototype_types: set[str] = set()
        if comp is not None:
            for sub in getattr(comp, "elements", []):
                if type(sub).__name__ == "PrototypeDecl":
                    t = getattr(sub.type, "name", None)
                    if t:
                        prototype_types.add(t)
        for c in sw_components:
            art_node = getattr(c, "art_node", "") or ""
            leaf = art_node.rsplit("/", 1)[-1]
            if leaf == comp_name or leaf in prototype_types:
                return True
        return False

    # Run the three checks and collect gaps.
    gaps: dict[str, list[str]] = {
        "cluster_member_without_application_or_swcomponent": [],
        "composition_without_swcomponent": [],
        "prototype_process_without_process_entry": [],
    }

    for cluster in clusters:
        for sub in getattr(cluster, "elements", []):
            if type(sub).__name__ != "ClusterMember":
                continue
            ident = sub.name
            comp = resolved.get(id(sub.type), sub.type)
            comp_name = getattr(comp, "name", "?")
            has_app = ident in apps_by_name
            has_sw = _has_sw_for_composition(comp_name)
            if not has_app and not has_sw:
                gaps["cluster_member_without_application_or_swcomponent"].append(
                    f"cluster {cluster.name}.{ident}  (composition {comp_name})",
                )

    for comp_name in sorted(compositions_by_name):
        if not _has_sw_for_composition(comp_name):
            gaps["composition_without_swcomponent"].append(
                f"composition {comp_name}",
            )

    # Dedupe (composition, process-id) — one rig Process per process-id,
    # not per prototype. Multiple prototypes share one Process when they
    # are pinned `on process P1`.
    process_groups: dict[tuple[str, str], list[str]] = {}
    for comp_name, proto_name, proc in prototypes_with_process:
        process_groups.setdefault((comp_name, proc), []).append(proto_name)
    for (comp_name, proc), protos in sorted(process_groups.items()):
        # Look for a Process whose name ends with the process-id token,
        # case-insensitive (rig names look like `demo_p1`, art proc is `P1`).
        proc_token = proc.lower()
        if not any(
            p.name.lower().endswith(f"_{proc_token}") or p.name.lower() == proc_token
            for p in rig.execution_manifests
        ):
            gaps["prototype_process_without_process_entry"].append(
                f"process {proc} in {comp_name}  (prototypes: {', '.join(protos)})",
            )

    # Report.
    total = sum(len(v) for v in gaps.values())
    click.echo(f"art: {art_file}")
    click.echo(f"rig: {rig_target} -> {rig.vehicle.name!r}")
    click.echo("")
    click.echo(
        f"clusters: {len(clusters)}  "
        f"compositions: {len(compositions_by_name)}  "
        f"prototypes-with-process: {len(prototypes_with_process)}",
    )
    click.echo(
        f"rig: applications={len(rig.applications)}  "
        f"sw_components={len(sw_components)}  "
        f"processes={len(rig.execution_manifests)}",
    )
    click.echo("")
    if total == 0:
        click.secho("✓ no gaps — rig is aligned with art", fg="green")
        return
    click.secho(f"✗ {total} gaps:", fg="yellow")
    for category, items in gaps.items():
        if not items:
            continue
        click.echo(f"\n  {category}:")
        for it in items:
            click.echo(f"    - {it}")
    sys.exit(1)


@main.command(
    "generate-manifest",
    help="Emit the per-machine deploy manifest set for a vehicle rig. "
    "TARGET is a dotted import path to a module exporting a Rig or "
    "SoftwareSpecification (e.g. vendor.vehicles.tornado.arsyscomp).\n\n"
    "Writes <out>/<machine>/{machine,application,service,execution}.yaml "
    "plus an <out>/index.yaml. Each ECU's Puppet flow reads its own "
    "directory.\n\n"
    "Use --flat to emit the legacy single-YAML view to stdout (or to "
    "--out FILE) for inspection / debugging.",
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
    "out_path",
    type=click.Path(),
    default="dist/manifest",
    show_default=True,
    help="Output directory (per-machine mode) or file (--flat mode).",
)
@click.option(
    "--flat",
    "flat",
    is_flag=True,
    default=False,
    help="Emit the legacy single-YAML view of the whole rig instead of "
    "per-machine directories. Goes to stdout when --out is the "
    "default directory.",
)
def generate_manifest_cmd(
    target: str,
    rig_attr: str | None,
    out_path: str,
    flat: bool,
) -> None:
    """Run a vendor rig module and emit the deploy manifest set."""
    rig = _resolve_rig(target, rig_attr)

    if flat:
        import dataclasses
        from enum import Enum
        from ipaddress import IPv4Address, IPv6Address

        import yaml

        def _serialize(v):
            if dataclasses.is_dataclass(v) and not isinstance(v, type):
                return {f.name: _serialize(getattr(v, f.name))
                        for f in dataclasses.fields(v)}
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
        # If --out was left at the default dir, write to stdout in --flat.
        if out_path == "dist/manifest":
            click.echo(yaml_text, nl=False)
        else:
            Path(out_path).write_text(yaml_text)
            click.echo(out_path)
        return

    # Per-machine mode (default).
    from .generators.dist_manifest import emit_dist_manifest

    written = emit_dist_manifest(rig, Path(out_path))
    for p in written:
        click.echo(str(p))


@main.group("executor", help="Erlang-style executor commands.")
def executor() -> None:
    pass


@executor.command(
    "emit",
    help="Emit the supervisor manifest (executor.yaml) for a vehicle rig. "
    "TARGET is a dotted import path to a module exporting a Rig "
    "(e.g. vendor.vehicles.tornado.arsyscomp).\n\n"
    "Without --machine, emits the whole-rig tree (single-machine deploys, "
    "or for inspection). With --machine, emits only the sub-tree relevant "
    "to that machine — Process leaves and pinned SupervisorNodes whose "
    "host doesn't match are dropped, and empty sub-supervisors are pruned.",
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
@click.option(
    "--machine",
    "machine",
    default=None,
    help="Machine to emit the sliced supervisor tree for "
    "(matches Machine.name in the rig). Without this flag, emits "
    "the whole-rig tree.",
)
def executor_emit(
    target: str,
    rig_attr: str | None,
    out_file: str | None,
    machine: str | None,
) -> None:
    import yaml

    from artheia.manifest.supervisor import build_supervisor_tree

    rig = _resolve_rig(target, rig_attr)
    tree = build_supervisor_tree(rig, machine=machine)

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
            # Per-node metadata for the supervisor's node_sup
            # synthesis (#364) + trace push routing (#361). Empty
            # for non-FC children (vendor apps without .art decl).
            nodes = getattr(node, "nodes", None) or []
            if nodes:
                d["nodes"] = [
                    {
                        "name": ni.name,
                        "reporting": ni.reporting,
                        "tipc_type": ni.tipc_type,
                        "tipc_instance": ni.tipc_instance,
                    }
                    for ni in nodes
                ]
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
        # HOST machines (admin consoles) don't run a supervisor —
        # the GUI is what's running on THEM. Skip them so the
        # machines.yaml only lists targets to observe.
        if getattr(m, "kind", "target") == "host":
            continue
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


# -----------------------------------------------------------------------------
# rig-deps — Bazel-facing rig structure dump
# -----------------------------------------------------------------------------


@main.command(
    "rig-deps",
    help="Emit the rig's component structure as JSON. Consumed by the "
    "Bazel rig() module extension to wire SwComponent.bazel_target refs "
    "into per-machine deploy bundles.",
)
@click.argument("target")
@click.option(
    "--rig",
    "rig_attr",
    default=None,
    help="Name of the Rig/SoftwareSpecification attribute. "
    "Defaults to *Software / *Rig / Rig.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write the JSON. Defaults to stdout.",
)
def rig_deps(target: str, rig_attr: str | None, out_file: str | None) -> None:
    """Emit a JSON describing the rig:

      {
        "vehicle": {"name": "demo", "make": "theia", "model": "..."},
        "machines": [
          {
            "name": "demo_host",
            "applications": [
              {
                "name": "platform_app",
                "components": [
                  {"name": "demo_p1", "bazel_target": "//demo:p1_main",
                   "owner": "platform", "art_node": "system.demo/DemoP1Composition"},
                  ...
                ]
              }
            ]
          }
        ],
        "executor_yaml_components": [
          # Same components, flat — for the Bazel rule that builds the
          # opkg payload (so it doesn't have to walk the machine list).
          {"name": "demo_p1", "bazel_target": "//demo:p1_main", "machine": "demo_host"},
          ...
        ]
      }

    The Bazel module extension reads this at module-load time and
    generates one synthetic repo per rig with per-machine targets.
    """
    import json

    rig = _resolve_rig(target, rig_attr)

    # Convert AUTOSAR CpuArchitecture → the dpkg-style token Bazel +
    # downstream packaging want ("amd64" / "arm64" / "armhf").
    # Kept local to this function — only consumers of rig.json need it,
    # and the CpuArchitecture enum has its own canonical names ("x86_64",
    # "aarch64") which dpkg renames.
    _DPKG_ARCH = {
        "x86_64":  "amd64",
        "aarch64": "arm64",
        "armv7":   "armhf",
        "riscv64": "riscv64",
    }

    def _arch_token(m) -> str:
        arch_str = ""
        try:
            arch_str = str(m.hardware.cpu.architecture.value)
        except AttributeError:
            try:
                arch_str = str(m.hardware.cpu.architecture)
            except AttributeError:
                pass
        return _DPKG_ARCH.get(arch_str, "amd64")

    # Build a per-machine grouping of components. Each ApplicationManifest's
    # host_machine field binds it to a specific machine; default to the
    # first machine if no binding is set (single-machine rigs).
    machines_by_name = {m.name: m for m in rig.machines}
    apps_by_machine: dict[str, list] = {m: [] for m in machines_by_name}

    for app in rig.applications:
        host = app.host_machine or (
            next(iter(machines_by_name)) if machines_by_name else ""
        )
        if host not in apps_by_machine:
            apps_by_machine[host] = []
        apps_by_machine[host].append(app)

    def _component_dict(c) -> dict:
        return {
            "name": c.name,
            "bazel_target": c.bazel_target,
            "owner": c.owner,
            "art_node": c.art_node,
            "bazel_buildable": getattr(c, "bazel_buildable", False),
        }

    machines_json = []
    for m in rig.machines:
        apps_json = []
        for app in apps_by_machine.get(m.name, []):
            apps_json.append({
                "name": app.name,
                "components": [_component_dict(c) for c in app.components],
            })
        machines_json.append({
            "name": m.name,
            "kind": getattr(m, "kind", "target"),
            "arch": _arch_token(m),
            "applications": apps_json,
        })

    # Flat list for convenience: every component the rig declares, with
    # its machine binding.
    flat_components = []
    for m in rig.machines:
        for app in apps_by_machine.get(m.name, []):
            for c in app.components:
                flat_components.append({
                    "name": c.name,
                    "bazel_target": c.bazel_target,
                    "machine": m.name,
                    "owner": c.owner,
                    "bazel_buildable": getattr(c, "bazel_buildable", False),
                })

    doc = {
        "vehicle": {
            "name": rig.vehicle.name,
            "make": rig.vehicle.make,
            "model": rig.vehicle.model,
        },
        "machines": machines_json,
        "flat_components": flat_components,
    }

    text = json.dumps(doc, indent=2)
    if out_file is None:
        click.echo(text)
    else:
        Path(out_file).write_text(text + "\n")
        click.echo(out_file)


if __name__ == "__main__":
    main()
