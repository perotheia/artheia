"""Artheia command-line interface."""
from __future__ import annotations

import sys
from pathlib import Path  # noqa: F401  (used by --catalog branch)

import click
from textx import TextXError, TextXSemanticError, TextXSyntaxError

from . import __version__
from .generators import (
    generate_etcd_schema,
    generate_netgraph,
    generate_proto,
)
from .model import (
    parse_file,
    parse_file_standalone,
    parse_bus_component_nodes_only,
)


def _parse(art_file: str):
    try:
        return parse_file(art_file)
    except (TextXSyntaxError, TextXSemanticError, TextXError) as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(2)


def _parse_standalone(art_file: str):
    """Like _parse but WITHOUT the package.art/component.art sibling merge —
    for the catalog stop (parse the small bus-node component.art, not the PDU
    monolith)."""
    try:
        return parse_file_standalone(art_file)
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
         "By default, `extern` forward-decls (`extern cluster Services { }`,\n"
         "`extern composition Supervisor { }`) are resolved RECURSIVELY: the parser\n"
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
@click.option("--force", "force", is_flag=True, default=False,
              help="Push INSIDE catalog-optimized bus packages: resolve a bus "
                   "mega-node's full per-PDU ifaces + ports (and their "
                   "messages) instead of the cheap node-only view. Accepts the "
                   "O(N²) parse cost (seconds for a 1000-PDU bus).")
def parse(art_file: str, depth, only_containers: bool, fqn_filter,
          no_recurse: bool, force: bool) -> None:
    # If the ENTRY file is itself a catalog-optimized bus package (its dir has a
    # catalog.json), the default view is the cheap node-only projection of its
    # bus mega-node(s) — parsing the merged package.art+component.art (messages +
    # N ifaces + N ports) is O(N²) (seconds-to-minutes for a 1000-PDU bus).
    # --force pushes inside (full merged parse, accepts the N²).
    from pathlib import Path as _P
    entry_dir = _P(art_file).absolute().parent
    if (not force) and (entry_dir / "catalog.json").exists():
        model = parse_bus_component_nodes_only(art_file)
        if model is None:
            model = _parse(art_file)
        _print_tree(model, max_depth=depth, only_containers=only_containers,
                    fqn_filter=fqn_filter, resolved={})
        return
    model = _parse(art_file)
    resolved = ({} if no_recurse
                else _resolve_forward_decls(art_file, model, force=force))
    _print_tree(model, max_depth=depth, only_containers=only_containers,
                fqn_filter=fqn_filter, resolved=resolved)


# ---------------------------------------------------------------------------
# Forward-decl resolver: scan the workspace for real definitions.
# ---------------------------------------------------------------------------


def _import_dir(entry: "Path", entry_pkg: str, import_pkg: str) -> "Path | None":
    """Resolve an ``import <import_pkg>`` to a directory, RELATIVE to the
    entry file's own package + location. Layout-independent: it never
    reconstructs a global workspace root, so it works whether the entry
    was reached via the canonical symlinked path or via the symlink
    target (the real file).

    The rule: the entry's directory IS its own package. An import that
    extends the entry's package maps to a subdirectory of the entry's
    directory, dropping the shared prefix.

      entry  platform/system/system.art   (package system)
      import system.services.com          → drop "system" → services/com
                                          → platform/system/services/com
                                            (the `services` symlink)
      entry  services/system/system.art   (package system.services)
      import system.services.com          → drop "system.services"
                                          → com
                                          → services/system/com         ✓

    When the import does NOT extend the entry's package (a sibling or
    unrelated tree), fall back to treating the entry's directory as the
    mirror of ``entry_pkg`` and mapping the import's full FQN from the
    common root upward — i.e. strip the longest shared package prefix,
    ascend that many levels, then descend the import remainder.
    """
    entry_dir = entry.parent
    e_parts = entry_pkg.split(".") if entry_pkg else []
    i_parts = import_pkg.split(".") if import_pkg else []

    # Longest common package prefix.
    common = 0
    for a, b in zip(e_parts, i_parts):
        if a != b:
            break
        common += 1

    # Ascend out of the entry's package down to the common root: the
    # entry's directory mirrors e_parts, so its base (common root dir)
    # is entry_dir with (len(e_parts) - common) trailing segments
    # stripped.
    up = len(e_parts) - common
    base = entry_dir
    for _ in range(up):
        base = base.parent

    remainder = i_parts[common:]

    # Layout-robustness: the naive ascent assumes the entry's directory
    # mirrors its package FQN 1:1. That holds via the canonical `system/`
    # symlink tree, but the PHYSICAL file (e.g. services/per/system/per for
    # package system.services.per) carries an extra `system/<fc>` suffix that
    # is NOT in the FQN — so the ascent undershoots the real root. If the
    # computed base doesn't actually contain the import's head segment, keep
    # walking up until it does (bounded by the filesystem root). This makes
    # resolution work from either path — the layout-independence the docstring
    # promises. (Catches the recurring `platform.runtime.*` / ChildControlIf
    # break when gen-app is handed the physical FC path.)
    if remainder:
        head = remainder[0]
        probe = base
        while not (probe / head).exists() and probe.parent != probe:
            probe = probe.parent
            if (probe / head).exists():
                base = probe
                break

    # Descend the import's remainder past the common prefix.
    p = base
    for seg in remainder:
        p = p / seg
    return p


def _kind_family(el) -> str | None:
    """Group decl classes into families for forward-decl matching.

    Returns the family name (one of: cluster, composition, node,
    interface) or None if *el* isn't a forward-decl-able element.
    SenderReceiverInterface and ClientServerInterface share the
    'interface' family because the stub form `interface X { }`
    doesn't carry the flavor — the real decl on the other side
    supplies it.
    """
    kind = type(el).__name__
    if kind == "ClusterDecl":
        return "cluster"
    if kind == "CompositionDecl":
        return "composition"
    if kind == "NodeDecl":
        return "node"
    if kind in ("SenderReceiverInterface", "ClientServerInterface"):
        return "interface"
    return None


def _is_stub(el) -> bool:
    """True if *el* is an `extern` forward-declaration.

    Forward-decls are now marked EXPLICITLY with the `extern` keyword
    (cluster / composition / node / interface). The old empty-body
    heuristic is retired: an empty body is no longer magic — say
    `extern` when you mean a forward declaration. This keeps the
    senderReceiver case honest (an empty `interface X { }` is a real,
    payload-less interface; only `extern interface X { }` is a stub).
    """
    return bool(getattr(el, "extern", False))


# Entry-file priority for an imported package directory. component.art is LAST
# so a hand-written package (schema in package.art) is preferred — except when
# the catalog optimization kicks in (below).
_PKG_ENTRY_PRIORITY = ("system.art", "cluster.art", "package.art", "component.art")


def _pkg_entry_file(pkg_dir: "Path") -> "tuple[Path | None, bool]":
    """Pick the .art entry to parse for an imported package directory, and
    report whether the catalog optimization was detected.

    Returns ``(entry_path, catalog_stop)``:

      - If the directory carries a ``catalog.json`` (a generated, read-only bus
        package — kcan/flexray), the catalog optimization is DETECTED: parsing
        the ``package.art`` PDU monolith is O(N²) (measured ~3.4s for flexray's
        1025 PDUs) and unnecessary — the per-PDU message/interface types resolve
        LAZILY from catalog.json. So we STOP at the small ``component.art`` (the
        bus mega-node only) and return ``catalog_stop=True``; the message/iface
        chain is left to lazy resolution. No catalog.json → normal priority,
        ``catalog_stop=False``.

    Shared by the CLI parse path AND the LSP (both walk the import graph and must
    avoid dragging a thousand-PDU package into scope). The forward-declaration
    chain node→iface→message is what makes the stop safe: the bus NODE is the
    only thing that must be eagerly present; its sender ports' ifaces (and their
    messages) are forward-resolved on demand from the catalog.
    """
    if (pkg_dir / "catalog.json").exists():
        comp = pkg_dir / "component.art"
        return (comp if comp.exists() else None, True)
    for fname in _PKG_ENTRY_PRIORITY:
        if (pkg_dir / fname).exists():
            return (pkg_dir / fname, False)
    return (None, False)


def _collect_imported_models(art_file: str, model, force: bool = False) -> list:
    """Follow the entry model's ``import system.x.*`` graph and return
    a list of additional ``(Path, model)`` tuples — every reachable
    file's parsed model, NOT including the entry model itself.

    Uses the same package→directory rules and file-name priority as
    :func:`_resolve_forward_decls`. Errors out the same way on a
    missing import target so a recursive walk fails loudly rather
    than silently skipping nodes.
    """
    from pathlib import Path

    # absolute(), NOT resolve(): the workspace layout uses symlinks
    # (e.g. system/gateway → ../gateway/system) to mirror the package
    # FQN onto a directory. Resolving the symlink would dump us in the
    # physical path (gateway/system/...) where the package↔dir mirror
    # no longer holds, and _import_dir would compute wrong import paths.
    entry = Path(art_file).absolute()

    visited_packages: set[str] = set()
    visited_files: set[Path] = {entry}
    out: list = []

    def _follow(import_fqn: str, from_path: Path, from_pkg: str) -> None:
        # Resolve relative to the IMPORTING file's package + location,
        # so each transitively-imported file maps its own imports
        # correctly regardless of how the tree was entered.
        pkg = import_fqn[:-2] if import_fqn.endswith(".*") else import_fqn
        if pkg in visited_packages:
            return
        visited_packages.add(pkg)

        pkg_dir = _import_dir(from_path, from_pkg, pkg)
        if pkg_dir is None or not pkg_dir.is_dir():
            click.secho(
                f"error: import {import_fqn!r}: no directory {pkg_dir}",
                fg="red", err=True,
            )
            sys.exit(2)

        candidate, catalog_stop = _pkg_entry_file(pkg_dir)
        if candidate is None:
            click.secho(
                f"error: import {import_fqn!r}: no system.art / cluster.art / "
                f"package.art / component.art under {pkg_dir}",
                fg="red", err=True,
            )
            sys.exit(2)

        resolved_path = candidate.resolve()
        if resolved_path in visited_files:
            return
        visited_files.add(resolved_path)
        try:
            # Catalog stop (default) → cheap node-only projection (bus nodes,
            # ports stripped). --force → full component.art (node+ifaces+ports,
            # the accepted N²). No stop → normal merged parse.
            if catalog_stop and not force:
                other = parse_bus_component_nodes_only(str(candidate))
            elif catalog_stop:
                other = _parse_standalone(str(candidate))
            else:
                other = _parse(str(candidate))
            if other is None:
                return
        except SystemExit:
            return
        out.append((resolved_path, other))
        other_pkg = getattr(other, "name", "") or ""
        # Catalog stop: a bus package's component.art (node-only) carries no
        # imports worth following, and we must NOT descend into the PDU
        # monolith — the per-PDU types resolve lazily from catalog.json.
        if catalog_stop:
            return
        for imp in getattr(other, "imports", []) or []:
            _follow(imp.name, candidate, other_pkg)

    entry_pkg = getattr(model, "name", "") or ""
    for imp in getattr(model, "imports", []) or []:
        _follow(imp.name, entry, entry_pkg)

    return out


def _resolve_forward_decls(art_file: str, model, force: bool = False) -> dict:
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

    # absolute(), not resolve() — see _collect_imported_models for why
    # the entry path must NOT follow workspace symlinks.
    entry = Path(art_file).absolute()
    entry_pkg = getattr(model, "name", "") or ""

    # Key by (family, name) so a NodeDecl Foo doesn't collide with a
    # CompositionDecl Foo (real example: platform/supervisor/system has
    # both a `node Supervisor` and a `composition Supervisor`).
    by_name: dict[tuple[str, str], list[tuple[Path, object]]] = {}
    visited_packages: set[str] = set()

    def _absorb(other_model, other_path: Path, follow_imports: bool = True) -> None:
        """Add other_model's non-stub clusters/compositions/nodes/
        interfaces to the index, then (unless follow_imports=False) follow ITS
        imports too. follow_imports=False is the catalog stop: a bus package's
        node-only component.art is indexed, but we don't descend into the PDU
        monolith its sibling package.art would pull in."""
        for e in getattr(other_model, "elements", []):
            fam = _kind_family(e)
            if fam is None:
                continue
            if _is_stub(e):
                continue
            by_name.setdefault((fam, e.name), []).append((other_path, e))
        if not follow_imports:
            return
        other_pkg = getattr(other_model, "name", "") or ""
        for imp in getattr(other_model, "imports", []) or []:
            _follow(imp.name, other_path, other_pkg)

    def _follow(import_fqn: str, from_path: Path, from_pkg: str) -> None:
        """Resolve an ``import <FQN>`` to a file and absorb it.

        Resolution is relative to the IMPORTING file (``from_path`` +
        ``from_pkg``), so the same import maps correctly whether the
        tree was entered via the canonical symlinked path or via the
        symlink target (the real file)."""
        # Drop the trailing `.*` wildcard if present — the FQN
        # always names a package (= directory) for us.
        pkg = import_fqn[:-2] if import_fqn.endswith(".*") else import_fqn
        if pkg in visited_packages:
            return
        visited_packages.add(pkg)

        pkg_dir = _import_dir(from_path, from_pkg, pkg)
        if pkg_dir is None or not pkg_dir.is_dir():
            click.secho(
                f"error: import {import_fqn!r}: no directory {pkg_dir}",
                fg="red", err=True,
            )
            sys.exit(2)

        # Entry file for the package dir. The catalog optimization (a bus
        # package with catalog.json) STOPS at the node-only component.art —
        # parsing the PDU monolith (package.art) would be O(N²) and the per-PDU
        # types resolve lazily from the catalog instead.
        candidate, catalog_stop = _pkg_entry_file(pkg_dir)
        if candidate is None:
            click.secho(
                f"error: import {import_fqn!r}: no system.art / cluster.art / "
                f"package.art / component.art under {pkg_dir}",
                fg="red", err=True,
            )
            sys.exit(2)

        if candidate.resolve() == entry:
            return  # already loaded — that's the entry model itself
        try:
            if catalog_stop and not force:
                # Default: cheap node-only projection (bus nodes, ports stripped)
                # — never parse the N ifaces/ports/messages (O(N²)).
                other = parse_bus_component_nodes_only(str(candidate))
            elif catalog_stop:
                # --force: parse the full component.art (node + ifaces + ports),
                # then below we'll also let the package.art messages resolve —
                # the accepted N² "push inside" view.
                other = _parse_standalone(str(candidate))
            else:
                other = _parse(str(candidate))
            if other is None:
                return
        except SystemExit:
            return  # diagnostic already printed
        # On a catalog stop, index the bus node but do NOT follow the package's
        # imports (would re-enter the monolith).
        _absorb(other, candidate, follow_imports=not catalog_stop)

    # Seed the recursion with the entry file's imports.
    for imp in getattr(model, "imports", []) or []:
        _follow(imp.name, entry, entry_pkg)

    # 4. Walk the input model and resolve every stub against the index.
    resolved: dict = {}
    unresolved: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, list[str]]] = []

    # Entry's own catalog (if it's a bus dir): under --force, the bus's extern
    # `<Pdu>_Iface` fwd-decls resolve here (the catalog is authoritative for
    # message/interface). Lazy-built once.
    _own_catalog = {"idx": None, "built": False}

    def _own_catalog_lookup(fam: str, name: str):
        if not force:
            return None
        if not _own_catalog["built"]:
            _own_catalog["built"] = True
            cat = entry.parent / "catalog.json"
            if cat.exists():
                from .model.scope import _CatalogIndex  # type: ignore
                _own_catalog["idx"] = _CatalogIndex(cat, entry_pkg)
        idx = _own_catalog["idx"]
        if idx is None:
            return None
        cfam = "interface" if fam == "interface" else (
            "message_or_enum" if fam == "message_or_enum" else None)
        if cfam is None:
            return None
        try:
            return idx.lookup(cfam, name)
        except Exception:
            return None

    def _resolve(stub):
        if not _is_stub(stub):
            return
        fam = _kind_family(stub)
        if fam is None:
            return
        candidates = by_name.get((fam, stub.name), [])
        if not candidates:
            own = _own_catalog_lookup(fam, stub.name)
            if own is not None:
                resolved[id(stub)] = own
                return
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
        _kind_tags = {
            "ClusterDecl": "cluster",
            "CompositionDecl": "composition",
            "NodeDecl": "node",
            "SenderReceiverInterface": "interface senderReceiver",
            "ClientServerInterface": "interface clientServer",
        }
        msg = ["unresolved forward-decls (no real definition found):"]
        for kind, name in unresolved:
            msg.append(f"  {_kind_tags.get(kind, kind)} {name}")
        searched = ", ".join(sorted(visited_packages)) or "(no imports)"
        msg.append(
            f"\nsearched packages: {searched}\n"
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
        _dv = el.default.value
        _dv = _dv.s if _dv.__class__.__name__ == "StrLit" else _dv
        return f"param {el.name}:{el.type} = {_dv}"
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


@main.group("proto", help="Protobuf wire schema (.proto) from .art messages.")
def proto() -> None:
    """The .proto wire-schema verbs. Kept distinct from the deployment verbs:

      proto emit         — .proto files from .art messages (wire schema)
      gen-manifest       — a DeploymentLayer from an .art subtree
      serialize-manifest — per-machine JSON from a DeploymentLayer
    """
    pass


@proto.command("emit", help="Emit .proto files (one per message) from an .art.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
def proto_emit(art_file: str, out_dir: str) -> None:
    model = _parse(art_file)
    paths = generate_proto(model, out_dir, source_file=art_file)
    for p in paths:
        click.echo(p)


@main.command("gen-proto", hidden=True,
              help="Deprecated alias for `proto emit` (kept for back-compat).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False))
@click.pass_context
def gen_proto(ctx: click.Context, art_file: str, out_dir: str) -> None:
    ctx.invoke(proto_emit, art_file=art_file, out_dir=out_dir)


# (gen-proto-package retired as a standalone command — `gen-app --kind fc`
# emits the per-package .proto internally via generate_package_proto (still
# in generators/proto_package.py). The committed platform/proto/*.proto were
# produced by it; regenerate them through gen-app, not a bare command.)


@main.command("gen-manifest",
              help=".art subtree → manifest.py (DeploymentLayer) + "
                   "executor.py (write-once unless --force).\n\n"
                   "Emits a base DeploymentLayer on the orthogonal-ARA engine: "
                   "one EXECUTION-axis process per cluster member, best-effort "
                   "SERVICE-axis instances from provided interfaces, machines "
                   "left open (the deploy variant binds them). The supervisor "
                   "tree is sidecared in executor.py — written once, then "
                   "hand-editable (regenerate only with --force).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("out_file", type=click.Path(dir_okay=False))
@click.option("--force", "-f", "force", is_flag=True, default=False,
              help="Clobber an existing executor.py sidecar (default: keep it).")
def gen_manifest(art_file: str, out_file: str, force: bool) -> None:
    from .generators.manifest_gen import generate_manifest
    path = generate_manifest(art_file, out_file, force=force)
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


# (gen-app-composition retired — superseded by `gen-app --kind fc
# --composition <Name>`, which emits a buildable per-composition app
# (lib + impl + main) instead of just main.cc + CMakeLists over a
# hand-written node runtime.)


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
@click.option(
    "--recursive", "-R", is_flag=True, default=False,
    help="Follow `import system.x.*` statements and union nodes + "
    "compositions from every reachable file. Use for aggregator files "
    "like platform/system/system.art that declare no nodes themselves.",
)
def gen_netgraph(
    art_file: str, out_file: str, catalog: str | None, recursive: bool,
) -> None:
    import json as _json

    from .generators.netgraph import DuplicateTipcAddress
    model = _parse(art_file)
    cat = _json.loads(Path(catalog).read_text()) if catalog else None
    extras = None
    if recursive:
        extras = [m for _p, m in _collect_imported_models(art_file, model)]
    try:
        path = generate_netgraph(model, out_file, catalog=cat,
                                 extra_models=extras)
    except DuplicateTipcAddress as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(1)
    click.echo(str(path))


@main.command(
    "check-addresses",
    help="Assert every node's TIPC (type, instance) is unique across the "
    "system. Follows `import` recursively (like -R gen-netgraph), so it "
    "catches CROSS-FC collisions (e.g. two FCs both claiming 0x80010008). "
    "Exits non-zero on a clash. Run before manifest/dist generation.",
)
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--catalog",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Gateway catalog JSON (so bus mega-nodes resolve their addresses).",
)
def check_addresses(art_file: str, catalog: str | None) -> None:
    """Pure address-collision gate. Unions every reachable model (recursive
    import-follow), then runs the netgraph's system-wide TIPC uniqueness check
    — without writing any output. The right pre-flight before generate-manifest
    / theia dist: a duplicate (type, instance) silently mis-wires the runtime
    (TIPC routes purely by address), so we fail the build instead.

    Point it at the WIDEST aggregator that imports the FCs you deploy —
    e.g. system/services/cluster.art (imports com + per + every service)."""
    import json as _json

    from .generators.netgraph import build_netgraph, DuplicateTipcAddress
    model = _parse(art_file)
    cat = _json.loads(Path(catalog).read_text()) if catalog else None
    extras = [m for _p, m in _collect_imported_models(art_file, model)]
    try:
        ng = build_netgraph(model, catalog=cat, extra_models=extras)
    except DuplicateTipcAddress as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(1)
    n = len(ng.get("nodes", []))
    click.secho(
        f"check-addresses: OK — {n} node(s), all TIPC addresses distinct",
        fg="green",
    )


@main.command("gen-etcd", help="Emit the etcd seed schema for all node params.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
def gen_etcd(art_file: str, out_file: str) -> None:
    model = _parse(art_file)
    path = generate_etcd_schema(model, out_file)
    click.echo(str(path))


@main.command("gen-params",
              help="Emit the per-FC static params JSON (one section per node, "
                   "keyed by prototype name = kNodeName). Read once at boot by "
                   "the runtime config singleton; stage at "
                   "<ROOT>/<machine>/config/<fc>.json.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
def gen_params(art_file: str, out_file: str) -> None:
    from .generators.params_config import generate_params_config
    model = _parse(art_file)
    path = generate_params_config(model, out_file)
    click.echo(str(path))


@main.command("gen-schema",
              help="Emit ONE combined config-schema JSON for all FC `config "
                   "<Msg>` bindings in a system .art: per config_type, the "
                   "stable shape DIGEST + proto type + field shape + bound "
                   "nodes. The spine of the migration tooling (snapshot decode, "
                   "transform validation/codegen).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
def gen_schema(art_file: str, out_file: str) -> None:
    from .generators.config_schema import generate_config_schema
    model = _parse(art_file)
    path = generate_config_schema(model, out_file)
    click.echo(str(path))


@main.command("gen-config-defaults",
              help="Emit the DECLARED config-field defaults (the .art `field = "
                   "value` on a `config <Msg>` message), keyed by node prototype "
                   "+ config_type + digest. The first-boot seed source: a node "
                   "whose config has no stored value in etcd starts at these "
                   "instead of proto3 zeros. Parallel to gen-params (the static "
                   "side); same declare-once source feeds the migration `add` "
                   "rule (via gen-schema) AND this seed.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
def gen_config_defaults(art_file: str, out_file: str) -> None:
    from .generators.config_defaults import generate_config_defaults
    model = _parse(art_file)
    path = generate_config_defaults(model, out_file)
    click.echo(str(path))


@main.command("gen-transform",
              help="Generate a migration-plugin .cc (+ a write-once custom "
                   "sidecar) from a transform.json rule-set. The plugin is "
                   "dlopen'd by per (MigrateBulk) and works on the nanopb "
                   "STRUCT — no JSON, no libprotobuf at runtime (JSON lives "
                   "only in tools/migrate/migrate.py, the design bench). Needs "
                   "--schema (gen-schema output) for the struct field shapes. "
                   "Build the .cc(+_custom.cc) as a cc_binary .so (linkshared), "
                   "like services/per/migrations/example.")
@click.argument("transform_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False))
@click.option("--schema", "schema_file", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="gen-schema output (config_type -> proto_type + field shape).")
def gen_transform(transform_json: str, out_file: str, schema_file: str) -> None:
    from .generators.transform_codegen import generate_transform_from_file
    path = generate_transform_from_file(transform_json, out_file, schema_file)
    click.echo(str(path))


@main.command("gen-migration",
              help="Diff two gen-schema outputs (OLD vs NEW) and SCAFFOLD the "
                   "per-node migration transforms: one <node>_v1_to_v2.json per "
                   "config whose shape digest changed, pre-filled with the "
                   "from/to digests + the auto-derivable rules (add/remove, a "
                   "same-tag RENAME heuristic, and a custom-hook stub for type "
                   "changes — each guess flagged in a `_review` note to confirm). "
                   "Also regenerates the migration BUILD plugin entries (the "
                   "managed block). Then run `gen-transform` on each .json to "
                   "emit the plugin .cc. Configs with an unchanged digest emit "
                   "nothing; a config only in NEW is a fresh binding (skipped).")
@click.option("--from", "from_schema", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="gen-schema output for the OLD (v1) shapes.")
@click.option("--to", "to_schema", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="gen-schema output for the NEW (v2) shapes.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False),
              help="Directory for the <node>_v1_to_v2.json files (+ BUILD).")
@click.option("--no-build", "no_build", is_flag=True, default=False,
              help="Don't (re)write the BUILD.bazel plugin entries.")
def gen_migration(from_schema: str, to_schema: str, out_dir: str,
                  no_build: bool) -> None:
    from .generators.migration_diff import generate_migrations
    written = generate_migrations(from_schema, to_schema, out_dir,
                                  emit_build=not no_build)
    if not written:
        click.echo("no config-shape changes between the two schemas "
                   "(nothing to migrate)")
        return
    for ct, path in written.items():
        click.echo(f"{ct} -> {path}")


# (gen-cpp-stubs retired — conflicted with gen-app, which emits the
# GenServer/GenStateM daemon (incl. the statem StateMBase) directly from
# the same .art. There is one C++-from-.art path now: `gen-app --kind fc`.)

# (gen-trace-decoder-subset retired — unused. The trace decoder is built as
# a dependency, not generated per-rig from a netgraph here.)


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
@click.option("--bus", "bus_name", required=True, help="Bus name, e.g. vehicle_gen2_a.")
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
              help="Platform support package root (e.g. ../Vehicle_Gen2_cmp_psp).")
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
              help="Package name for the emitted .art "
                   "(e.g. system.autosar.vehicle_gen2). The output path's directory "
                   "should match the package, so the file lands at "
                   "autosar/<psp>/system/vehicle_gen2/system.art.")
def gen_autosar_system(
    catalog_paths: tuple[str, ...], out_path: str, package_name: str,
) -> None:
    from .generators.autosar_system import generate
    generate(list(catalog_paths), out_path, package_name)


# (gen-host-netgraph retired — superseded by gen-netgraph, the single
# JSON netgraph emitter for nodes + compositions.)


@main.command(
    "gen-app",
    context_settings={"ignore_unknown_options": True},
    help="DEPRECATED — split into the gen-fc family. Use `gen-fc` (was --kind fc), "
    "`gen-fc-lib` (was --kind package), or `gen-fc-lib --vendored` (was --kind "
    "lib). This shim hard-errors and points to the replacement.",
)
@click.option("--kind", default="fc")
@click.argument("rest", nargs=-1, type=click.UNPROCESSED)
def gen_app(kind: str, rest: tuple) -> None:
    """Removed. gen-app was overloaded (--kind fc|lib|package); it is now the
    named gen-fc family. Hard-errors with the exact replacement command."""
    repl = {
        "fc": "gen-fc <component.art> --out <dir> [--proto-out <dir>] "
              "[--composition <name>] [--ns <ns>] [--force]",
        "package": "gen-fc-lib <package.art> --out <dir> [--proto-out <dir>] "
                   "[--ns <ns>] [--force]",
        "lib": "gen-fc-lib --vendored <package.art> --out <dir> [--proto-out <dir>] "
               "[--ns <ns>] [--force]",
    }.get(kind, "gen-fc / gen-fc-lib")
    click.secho(
        "error: `gen-app` was removed — it split into the gen-fc family.\n"
        f"  --kind {kind} → artheia {repl}\n"
        "  (gen-fc = FC/composition app with main; gen-fc-lib = no-main node lib; "
        "--vendored = self-contained standalone artifact.)",
        fg="red", err=True)
    sys.exit(2)


# Shared result printer for the gen-fc family (gen-fc / gen-fc-lib), so the new
# verbs render identically to the (deprecated) gen-app.
def _echo_gen_results(results: dict) -> None:
    for path in results.get("wrote", []):
        click.echo(f"  wrote:      {path}")
    for path in results.get("overwrote", []):
        click.echo(f"  overwrote:  {path}")
    for path in results.get("skipped-exists", []):
        click.echo(f"  skipped:    {path}  (exists; --force to overwrite)")


@main.command(
    "gen-fc",
    help="Generate a C++ Adaptive Functional Cluster / composition app from an "
    ".art (lib + impl + main, Bazel). Per-COMPOSITION / per-FC codegen — the "
    "head of the gen-fc family (gen-fc-lib is the no-main variant; gen-param + "
    "gen-config-defaults are its per-FC config sub-generators). Emits lib/impl/"
    "main into <out> and the .proto under <proto-out>. In a `theia init` "
    "workspace `--out apps --proto-out proto` are the defaults, so the bare "
    "`artheia gen-fc system/<app>/component.art` just works.")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_dir", default="apps", type=click.Path(file_okay=False),
              help="Output dir for the lib/main/impl slices (default: apps — the "
              "workspace convention).")
@click.option("--proto-out", "proto_out", default="proto",
              type=click.Path(file_okay=False),
              help="Where the generated .proto lands (default: proto — the "
              "workspace tree). Pass '' / --proto-out= to skip proto emission.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite the write-once impl slices.")
@click.option("--ns", "cxx_namespace", default=None,
              help="C++ namespace (e.g. `ara::sm`). Default: the .art package as "
              "one underscore-flat identifier.")
@click.option("--composition", "composition", default=None,
              help="Emit ONE app for a SINGLE composition (per-process layout). "
              "Default: auto-iterate every composition the .art declares.")
def gen_fc(art_file: str, out_dir: str, proto_out: str | None, force: bool,
           cxx_namespace: str | None, composition: str | None) -> None:
    from .generators.fc_app import generate_fc
    proto_out = proto_out or None   # explicit --proto-out= skips proto emission
    try:
        results = generate_fc(art_file, out_dir, proto_out=proto_out,
                              cxx_namespace=cxx_namespace, composition=composition,
                              force=force, package_mode=False)
    except ValueError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(2)
    _echo_gen_results(results)
    _warn_cross_fc_address_collision(art_file)


@main.command(
    "gen-fc-lib",
    help="Generate a NO-MAIN C++ node library from an .art — the linkable form of "
    "gen-fc (a sub-generator: same node emission, no executable). Bazel-only. Two "
    "layouts:\n\n"
    "  default: a ROS-style PACKAGE — lib + impl + proto built ONCE as a Bazel "
    "cc_library; a COMPOSITION in a workspace imports it, owns the main, links "
    "this lib against the WORKSPACE runtime (= the old `gen-app --kind package`).\n\n"
    "  --vendored: a SELF-CONTAINED slice — lib + impl + a VENDORED copy of "
    "platform/runtime/ + generated protos + a BUILD.bazel so it compiles as a "
    "standalone Bazel artifact with NO @pero_theia workspace dep + a main.cc."
    "example showing full runtime init (= the old `gen-app --kind lib`, now Bazel "
    "not CMake).")
@click.argument("art_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_dir", default=None, type=click.Path(file_okay=False),
              help="Output dir for the lib/impl slices. Default: src for a package, "
              ". (the repo root) with --vendored.")
@click.option("--proto-out", "proto_out", default=None,
              type=click.Path(file_okay=False),
              help="Where the generated .proto lands (default: proto for a package; "
              "--vendored defaults it to <out>/generated/ self-contained).")
@click.option("--vendored", is_flag=True, default=False,
              help="Emit the self-contained vendored-runtime layout (standalone "
              "Bazel artifact) instead of the workspace-linked package cc_library.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite the write-once impl slices.")
@click.option("--ns", "cxx_namespace", default=None,
              help="C++ namespace (e.g. `ara::<pkg>`). Default: the .art package "
              "as one underscore-flat identifier.")
def gen_fc_lib(art_file: str, out_dir: str | None, proto_out: str | None,
               vendored: bool, force: bool, cxx_namespace: str | None) -> None:
    if vendored:
        from .generators.lib_app import generate_lib
        # A vendored lib is a whole repo — default --out to the repo root.
        results = generate_lib(art_file, out_dir or ".",
                               proto_out=(proto_out or None),
                               cxx_namespace=cxx_namespace, force=force)
    else:
        from .generators.fc_app import generate_fc
        # A package's proto defaults to the workspace `proto/` tree (its
        # self-contained proto BUILD needs it); explicit --proto-out= skips.
        pkg_proto = "proto" if proto_out is None else (proto_out or None)
        try:
            results = generate_fc(art_file, out_dir or "src", proto_out=pkg_proto,
                                  cxx_namespace=cxx_namespace, composition=None,
                                  force=force, package_mode=True)
        except ValueError as e:
            click.secho(f"error: {e}", fg="red", err=True)
            sys.exit(2)
    _echo_gen_results(results)


def _warn_cross_fc_address_collision(art_file: str | None) -> None:
    """Best-effort cross-FC TIPC collision warning after gen-app. Walks up from
    the .art to find the workspace root (the dir holding system/services/
    cluster.art), then runs the system-wide netgraph check over the canonical
    aggregators. Prints a WARNING (not an error — doesn't fail gen-app) naming
    the clash; the `theia manifest` gate is what actually blocks the build."""
    if not art_file:
        return
    from .generators.netgraph import build_netgraph, DuplicateTipcAddress

    # Find the workspace root by walking up for system/services/cluster.art.
    cur = Path(art_file).resolve().parent
    root = None
    for cand in [cur, *cur.parents]:
        if (cand / "system" / "services" / "cluster.art").is_file():
            root = cand
            break
    if root is None:
        return   # not in a recognizable workspace; the manifest gate covers it

    aggregators = [
        root / "system" / "services" / "cluster.art",
        root / "system" / "system.art",
    ]
    for agg in aggregators:
        if not agg.is_file():
            continue
        try:
            model = _parse(str(agg))
            extras = [m for _p, m in _collect_imported_models(str(agg), model)]
            build_netgraph(model, extra_models=extras)
        except DuplicateTipcAddress as e:
            click.secho(
                f"\nWARNING: TIPC address collision after this gen-app "
                f"(via {agg.name}):\n{e}\n"
                "Pick a distinct `tipc type=…` in the .art. "
                "`theia manifest` will refuse to build until this is fixed.",
                fg="yellow", err=True)
            return
        except Exception:
            # A broken/incomplete aggregator (mid-refactor) shouldn't make
            # gen-app noisy — the manifest gate is the authority.
            return


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
@click.option("--namespace-fr", default="vehicle_gen2",
              help="FlexRay proto package namespace (default: vehicle_gen2).")
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
@click.option("--namespace", required=True, help="Namespace / library name (e.g. vehicle_gen2).")
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



def _load_deployment(target: str, attr: str | None):
    """Import ``target`` and return ``(module, DeploymentLayer)``.

    The single resolution path shared by every command that loads a rig on
    the orthogonal-ARA engine (``serialize-manifest`` / ``rig-deps`` /
    ``gui emit``). When ``attr`` is None the export is auto-detected:
    DEPLOYMENT first, then RIG/SINGLE/DOCKER/HW/LOCAL, then any
    :class:`DeploymentLayer`-typed export. The module is returned too so
    callers can read sidecar attributes (e.g. ``SUPERVISORS``).
    """
    import importlib

    from artheia.manifest.deployment import DeploymentLayer

    try:
        module = importlib.import_module(target)
    except ModuleNotFoundError as e:
        click.secho(
            f"error: can't import '{target}' ({e}) — check it's a real "
            f"dotted module path under this workspace (e.g. "
            f"manifest/<target>/rig.py → manifest.<target>.rig) and that "
            "you're running from the workspace root.",
            fg="red", err=True)
        sys.exit(2)

    if attr is not None:
        if not hasattr(module, attr):
            click.secho(f"error: {target} has no attribute '{attr}'",
                        fg="red", err=True)
            sys.exit(2)
        dep = getattr(module, attr)
    else:
        names = [n for n in vars(module)
                 if isinstance(getattr(module, n), DeploymentLayer)]
        preferred = ["DEPLOYMENT", "RIG", "SINGLE", "DOCKER", "HW", "LOCAL"]

        def _rank(name: str) -> tuple[int, str]:
            return (preferred.index(name) if name in preferred else len(preferred),
                    name)

        names.sort(key=_rank)
        if not names:
            click.secho(
                f"error: {target} exports no DeploymentLayer (pass --attr <name>)",
                fg="red", err=True)
            sys.exit(2)
        dep = getattr(module, names[0])

    if not isinstance(dep, DeploymentLayer):
        click.secho(f"error: {target} attr is not a DeploymentLayer",
                    fg="red", err=True)
        sys.exit(2)

    return module, dep


@main.command(
    "serialize-manifest",
    help="DeploymentLayer → per-machine deploy JSON.\n\n"
    "TARGET is a dotted import path to a module exporting a DeploymentLayer "
    "on the orthogonal-ARA engine (e.g. manifest.demo.single). The layer is "
    "looked up by name: DEPLOYMENT first, then SINGLE/DOCKER/HW/LOCAL/*-named "
    "DeploymentLayer; use --attr to name it.\n\n"
    "validate() runs FIRST: any severity=error issue is printed (path: "
    "message) and the command exits non-zero (the validate-before-serialize "
    "gate). On success the layer is simplified and written as:\n\n"
    "  <out>/machines.json                         the machine list\n"
    "  <out>/<machine>/machine.json                that machine's MachineTarget\n"
    "  <out>/<machine>/execution.json              its processes\n"
    "  <out>/<machine>/service.json                its service instances\n"
    "  <out>/<machine>/application.json            its applications\n"
    "  <out>/<machine>/executor.json               the sup tree sliced per machine",
)
@click.argument("target")
@click.option(
    "--attr",
    "attr",
    default=None,
    help="Name of the DeploymentLayer attribute in the module. "
    "Defaults to DEPLOYMENT, then SINGLE/DOCKER/HW/LOCAL, then any "
    "DeploymentLayer-typed export.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(),
    default="dist/manifest",
    show_default=True,
    help="Output directory for the per-machine deploy JSON.",
)
@click.option(
    "--arch",
    "arch_override",
    default=None,
    help="Override machine arch before serializing. A single token (x86_64) sets "
    "EVERY machine; a comma-list (aarch64,aarch64) sets one PER machine in machine-"
    "name order. Lets ONE rig serialize to per-arch outputs — and a SPLIT rig to a "
    "MIXED fleet (rpi4+jetson) — without duplicate per-arch rig files.",
)
@click.option(
    "--os",
    "os_override",
    default=None,
    help="Override machine OS/distro tag (bookworm / focal). Single token sets all; "
    "comma-list sets one per machine (name order). Pairs with --arch to pick the "
    "versioned runtime artifact per board, e.g. --arch aarch64,aarch64 --os "
    "bookworm,focal for a rpi4(bookworm)+jetson(focal) split.",
)
@click.option(
    "--rig-name",
    "rig_name",
    default=None,
    help="The deployment/rig NAME (e.g. single / split) → machines.json `rig`, and "
    "the name the user Software Package / .deb is keyed from. Falls back to the "
    "DeploymentLayer's MachineSetLayer.name when that is set (the model carrier); "
    "this CLI value is the robust source `theia manifest <target>` passes (the "
    "target dir name), surviving the layer fold that can clobber a model name.",
)
@click.option(
    "--tipc-netid",
    "tipc_netid",
    type=int,
    default=None,
    help="The rig's TIPC netid (cluster id) — the per-rig ISOLATION knob so two "
    "clusters on a shared L2 / shared host netns can't cross-talk. A DECLARED "
    "deploy fact: emitted to machines.json + each machine.json, and theia-run.sh "
    "reads it from config/machines.json (the deploy env THEIA_TIPC_NETID still "
    "overrides). Omit → no netid in the manifest (the TIPC default / a colony -e "
    "override still applies). One value applies cluster-wide.",
)
def serialize_manifest_cmd(
    target: str,
    attr: str | None,
    out_path: str,
    arch_override: str | None,
    os_override: str | None,
    rig_name: str | None,
    tipc_netid: int | None,
) -> None:
    """Import a DeploymentLayer module, validate it, and write per-machine JSON."""
    import json

    from artheia.manifest.algebra import validate

    module, dep = _load_deployment(target, attr)

    # --- per-machine arch / os override: one rig → per-board output -------
    # Rebuild each MachineLayer's arch/os from --arch/--os. A single token applies
    # to every machine; a comma-list maps one token PER machine in sorted machine-
    # name order (so `--arch a,b` over machines {central, compute} → central=a,
    # compute=b). The values flow into machine.json (arch + os), so the SAME rig
    # serializes for a mixed fleet without a duplicate per-arch/os rig file.
    if arch_override or os_override:
        import dataclasses
        from artheia.manifest.algebra import Explicit
        from artheia.manifest.deployment import MachineSetLayer

        def _per_machine(spec, names):
            # single token → dict(name→token); comma-list → zipped by sorted name.
            if spec is None:
                return {}
            toks = [t.strip() for t in spec.split(",")]
            if len(toks) == 1:
                return {n: toks[0] for n in names}
            if len(toks) != len(names):
                raise click.ClickException(
                    f"--arch/--os list has {len(toks)} token(s) but there are "
                    f"{len(names)} machine(s) {names}; give one token or one per machine")
            return dict(zip(names, toks))

        names = sorted(m.name for m in dep.machines.machines)
        arch_by = _per_machine(arch_override, names)
        os_by = _per_machine(os_override, names)
        machs = []
        for m in dep.machines.machines:
            repl = {}
            if m.name in arch_by:
                repl["arch"] = Explicit(arch_by[m.name])
            if m.name in os_by:
                repl["os"] = Explicit(os_by[m.name])
            machs.append(dataclasses.replace(m, **repl) if repl else m)
        dep = dataclasses.replace(
            dep, machines=MachineSetLayer(machines=set(machs)))

    # --- the validate-before-serialize gate -------------------------------
    issues = validate(dep)
    warnings = [i for i in issues if i.severity == "warning"]
    if warnings:
        click.secho(f"⚠ {len(warnings)} warning(s):", fg="yellow", err=True)
        for i in warnings:
            click.echo(f"  {i.path}: {i.message}", err=True)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        click.secho(f"✗ {len(errors)} error(s) — refusing to serialize:",
                    fg="red", err=True)
        for i in errors:
            click.echo(f"  {i.path}: {i.message}", err=True)
        sys.exit(1)

    target_dep = dep.simplify()

    # --- per-machine slicing ----------------------------------------------
    procs = list(target_dep.execution.processes)
    # DeploymentLayer.machines is a SET — iteration order is unstable. Sort to a
    # STABLE, canonical order so the derived machine_index (and everything keyed
    # on it: the supervisor's --tipc instance shift, com's instance→name map, the
    # GUI machine tabs) is reproducible across runs. Convention: the MASTER first
    # (index 0 — the etcd/coordinator: role=="master"/"central", or etcd=True, or
    # the legacy name "central"), then the rest alphabetically. Matches the
    # master=0 / worker=1,2,… instance scheme the supervisor + shwa assume.
    def _is_master(m):
        return (str(getattr(m, "role", "") or "") in ("master", "central")
                or bool(getattr(m, "etcd", False)) or m.name == "central")
    machines = sorted(target_dep.machines.machines,
                      key=lambda m: (not _is_master(m), m.name))
    machine_index = {m.name: i for i, m in enumerate(machines)}

    # ── Machine-identity invariants (run right after the TIPC address-uniqueness
    #    gate). com correlates a discovered TIPC instance N → machine_index → the
    #    UNIQUE machine NAME (the RUNTIME identity; role and hostname are NOT unique
    #    and must never be the key). These rules GUARANTEE that correlation always
    #    resolves, so com never needs a fallback:
    #      (a) machine NAMES are unique — two boards can't share a name (a role like
    #          "zonal" used as N boards' name is the classic mixup: role is the
    #          DEPLOYMENT identity, the name is the RUNTIME identity — keep them
    #          separate, give each board a distinct name e.g. compute/frontal).
    #      (b) machine_index is SEQUENTIAL from 0 (0,1,2,…) — no gaps, so instance→
    #          index→name is total.
    #      (c) index 0 is the MASTER/central (the etcd coordinator) — the supervisor
    #          + shwa + com all assume master=instance-0.
    if machines:
        names = [m.name for m in machines]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise click.ClickException(
                "serialize-manifest: machine NAMES must be unique — the machine "
                f"name is the runtime identity com/GUI key on. Duplicated: {dupes}. "
                "Give each board a distinct name (role master/zonal is the "
                "deployment identity, NOT the name).")
        idxs = sorted(machine_index.values())
        if idxs != list(range(len(machines))):
            raise click.ClickException(
                f"serialize-manifest: machine_index must be sequential from 0 "
                f"(got {idxs}).")
        if not _is_master(machines[0]):
            raise click.ClickException(
                "serialize-manifest: machine_index 0 must be the MASTER/central "
                f"(etcd coordinator); got {machines[0].name!r}.")

    services = list(target_dep.service.instances)
    apps = list(target_dep.applications.applications)

    def _proc_machines(p) -> set:
        """The EFFECTIVE placement of a process: {machine} ∪ machines. A process
        with a set-valued `machines` fans onto SEVERAL boards (a host-monitor like
        shwa); the scalar `machine` is the ordinary single-board case. Either may
        be empty; the deployment invariants guarantee at least one resolves."""
        out = set(getattr(p, "machines", None) or ())
        if p.machine:
            out.add(p.machine)
        return out

    def _proc_on(p, mname: str) -> bool:
        return mname in _proc_machines(p)

    def _proc_dict(p, on_machine=None) -> dict:
        # `machine` in the per-machine slice is THIS machine (a fanned-out process
        # binds itself here), not the authored scalar — so execution.json on each
        # board lists the process as local. Falls back to p.machine for the
        # whole-deployment view (on_machine=None).
        return {
            "name": p.name, "executable": p.executable,
            "start_cmd": p.start_cmd, "function_group": p.function_group,
            "fg_states": sorted(p.fg_states),
            "cpu_affinity": sorted(p.cpu_affinity),
            "scheduling": p.scheduling, "priority": p.priority,
            "mem_limit_bytes": p.mem_limit_bytes,
            "machine": on_machine if on_machine is not None else p.machine,
            "depends_on": sorted(p.depends_on),
            # Static data resources baked into this FC's deb (LUTs, tables).
            # p.resources is a tuple of (src, dest) pairs (hashable in the target);
            # emit as [{src,dest}] for the dist deb-staging step. Empty for a
            # code-only FC. `theia dist` stages each to /opt/theia/share/<fc>/data/.
            "resources": [{"src": s, "dest": d}
                          for (s, d) in (getattr(p, "resources", ()) or ())],
        }

    def _svc_dict(s) -> dict:
        return {
            "name": s.name, "interface": s.interface, "version": s.version,
            "instance_id": s.instance_id, "binding": s.binding,
            "endpoint": s.endpoint, "provided_by": s.provided_by,
        }

    def _machine_dict(m) -> dict:
        return {
            "name": m.name, "arch": m.arch, "os": getattr(m, "os", "linux"),
            # The machine's STABLE cluster index (central=0, compute=1, …). The
            # supervisor adds it to each child's --tipc instance (run-supervisor.sh
            # → THEIA_MACHINE_INSTANCE), and com maps it back to the machine name
            # (machine_manifest.cc). Without it both fall back to 0/"mN" — nodes on
            # the 2nd machine collide at instance 0 and machines lose their names.
            "machine_index": machine_index[m.name],
            "cores": sorted(m.cores),
            "machine_states": sorted(m.machine_states),
            "network_interfaces": sorted(m.network_interfaces),
            "os_packages": sorted(m.os_packages), "time_base": m.time_base,
            # Whether this machine hosts the cluster etcd (one per cluster — the
            # coordinator). Provisioning reads it to install etcd on central only.
            "etcd": bool(getattr(m, "etcd", False)),
            # Deployment ROLE (central | zonal | …) — the master/zone distinction
            # colony provisions against and the GS Distribution binds role→board.
            # Unset falls back to the machine name, so a lone "central" is its own
            # master with no authoring change.
            "role": getattr(m, "role", None) or m.name,
            # The rig's TIPC netid (cluster isolation), if declared (--tipc-netid).
            # theia-run.sh applies it before the first bind; the deploy env
            # THEIA_TIPC_NETID overrides. Absent (None) → key omitted, TIPC default.
            **({"tipc_netid": tipc_netid} if tipc_netid is not None else {}),
        }

    def _app_dict(a, on_machine=None) -> dict:
        """Application slice for one machine. `on_machine` (the machine's process
        names) intersects the app's process set so each board's application.json
        lists ONLY the processes that run there — matching execution.json /
        executor.json. None → the whole-deployment view (every process)."""
        procs = sorted(a.processes if on_machine is None
                       else (set(a.processes) & on_machine))
        return {
            "name": a.name, "host_machine": a.host_machine,
            "processes": procs,
        }

    # Supervisor tree from the source module's sidecar (executor.py), if any.
    supervisors = list(getattr(module, "SUPERVISORS", []) or [])
    sup_by_name = {s.name: s for s in supervisors}

    # Per-process node/module metadata resolved from the .art at gen-manifest
    # time (PROCESS_NODES on the manifest module; empty for a placeholder FC).
    process_nodes = dict(getattr(module, "PROCESS_NODES", {}) or {})

    # Per-process static params (params{} defaults) and etcd config-defaults
    # (config{} declared values + digest), both captured at gen-manifest time.
    # serialize-manifest uses these to emit config/<fc>.json per machine and
    # config-defaults.json for the first-boot etcd seed — no .art backtrack.
    process_params = dict(getattr(module, "PROCESS_PARAMS", {}) or {})
    process_config_defaults = dict(getattr(module, "PROCESS_CONFIG_DEFAULTS", {}) or {})

    proc_by_name = {p.name: p for p in procs}

    def _worker_dict(p) -> dict:
        """A fully-populated ChildSpec worker leaf — the shape
        platform/supervisor/impl/core/spec.cpp::load_worker parses. start_cmd
        is split to argv; env carries the boot log level + logger sink + config
        dir the supervisor exports to the child; nodes/modules come from
        PROCESS_NODES."""
        meta = process_nodes.get(p.name, {})
        start = p.start_cmd or f"bin/{p.name}"
        env = {
            "THEIA_LOG_LEVEL": "info",
            "THEIA_LOGGER": f"file:/tmp/theia/{p.name}.log",
            "THEIA_CONFIG_DIR": "config",
        }
        # Per-process env from PROCESS_NODES (app-specific knobs an FC reads from
        # the environment — e.g. the gateway's THEIA_GW_CAPTURE_IFACE / _PSP_ROOT).
        # Merged on top of the boot defaults so a process can override or extend.
        if isinstance(meta.get("env"), dict):
            env.update({str(k): str(v) for k, v in meta["env"].items()})
        w = {
            "name": p.name,
            "start_cmd": start.split() if isinstance(start, str) else list(start),
            "restart": "permanent",
            "shutdown": 5000,
            "type": "worker",
            "modules": list(meta.get("modules", [])),
            "env": env,
            "nodes": list(meta.get("nodes", [])),
        }
        if p.mem_limit_bytes:
            w["mem_limit_bytes"] = p.mem_limit_bytes
        # run_on_start (default true → omitted). A process whose PROCESS_NODES
        # meta carries run_on_start=false is DEFINED in the tree but not booted by
        # the supervisor (a HW-dependent FC opted out for this deploy — e.g. nm on
        # a container rig). Deploy tooling may also patch this into executor.json
        # post-serialize for a deploy-specific opt-out.
        if meta.get("run_on_start") is False:
            w["run_on_start"] = False
        return w

    out_dir = Path(out_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _dump(path: Path, doc) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n")
        written.append(path)

    # Whole-deployment machine list + the user Software Package(s). The SWP name
    # comes from the .art CLUSTER (gen-manifest scaffolds the ApplicationLayer name
    # from it); `services` is the platform AA, never a user SWP. This is the single
    # source the .deb / SWP is NAMED from (theia dist / release-swp read it here);
    # no swp.json duplicates it.
    #
    # ROLES = the DEPLOYMENT machine list (every board the Distribution targets:
    # runtime to all, the SWP overlay to the machine(s) running its processes).
    # arity = len(roles) — a single rig is arity 1, central+compute is arity 2.
    # `on` records which of those roles actually run the SWP's processes (compute
    # for the split demo), so the per-role deploy overlays the SWP only there.
    role_names = [m.name for m in machines]    # canonical central-first order
    # name → deployment ROLE (central | zonal | …). The Distribution binds each
    # role to a board ($name) and colony provisions per role (central → etcd/wifi/
    # mender-gw + full services; zonal → ucm/shwa + mender-agent). `roles` stays a
    # NAME list for back-compat (release-swp reads it as names); `role_map` is the
    # additive name→role hint colony/GS consume. Unset role falls back to the name.
    role_map = {m.name: (getattr(m, "role", None) or m.name) for m in machines}

    def _app_on(a) -> list:
        names = {m.name for m in machines
                 if (a.host_machine == m.name
                     or (set(a.processes) & {p.name for p in procs
                                             if _proc_on(p, m.name)}))}
        # Master first, then the canonical machine order (machine_index already
        # sorts master → workers). Preserve that instead of a name=="central" test.
        return sorted(names, key=lambda n: machine_index.get(n, 1 << 30))

    # The RIG NAME — the deployment's identity (e.g. single / split). This is what
    # the user Software Package / .deb is NAMED from, so two rigs of the SAME app
    # (single vs split) become DISTINCT named SWPs instead of both showing "apps".
    # Source order: the CLI --rig-name (what `theia manifest <target>` passes — the
    # target dir name; ROBUST, survives the layer fold + the arch/os rebuild that
    # drop a model name) → else the model's MachineSetLayer.name when an author set
    # it (not the default "machine") → else the app name (back-compat).
    set_name = getattr(target_dep.machines, "name", None)
    model_name = set_name if (set_name and set_name != "machine") else None
    rig = rig_name or model_name
    user_apps = [a for a in apps if a.name != "services"]
    # SWP `app` = the rig name (the deployment identity); fall back to the
    # application name when no rig name is available.
    swps = [{"app": (rig or a.name), "rig": rig, "application": a.name,
             "roles": role_names, "arity": len(role_names),
             "on": _app_on(a)} for a in user_apps]
    # name → STABLE cluster machine_index (master=0, then canonical worker order).
    # This is the RUNTIME identity com keys on: com discovers a supervisor at TIPC
    # instance N and correlates N → machine_index → the UNIQUE machine NAME here.
    # machines.json is the ONLY manifest com references, so the index MUST live here
    # (not only in the per-machine machine.json). Names are unique by construction
    # (a rig can't declare two machines of the same name); ROLE (master/zonal) and
    # hostname are NOT unique, so neither can be the identity — the machine name is.
    machine_index_map = {m.name: machine_index[m.name] for m in machines}
    machines_doc = {"machines": role_names, "rig": rig, "apps": swps,
                    "role_map": role_map, "machine_index": machine_index_map}
    # The rig's TIPC netid (cluster isolation), rig-WIDE — theia-run.sh reads it
    # from here (config/machines.json) and applies it before the first TIPC bind.
    # Declared via --tipc-netid; omitted when unset (TIPC default / colony -e).
    if tipc_netid is not None:
        machines_doc["tipc_netid"] = tipc_netid
    # Convenience: the primary SWP at top level (single-SWP rigs, the common case)
    # so consumers don't have to index `apps`.
    if len(swps) == 1:
        machines_doc["app"] = swps[0]["app"]
        machines_doc["application"] = swps[0]["application"]
        machines_doc["roles"] = swps[0]["roles"]
        machines_doc["arity"] = swps[0]["arity"]
        machines_doc["on"] = swps[0]["on"]
    _dump(out_dir / "machines.json", machines_doc)

    # Index the procs by name so a service's provided_by resolves to the full
    # process (for its effective placement), not just the authored scalar machine.
    proc_by_pname = {p.name: p for p in procs}

    for m in machines:
        mdir = out_dir / m.name
        m_procs = [p for p in procs if _proc_on(p, m.name)]
        m_proc_names = {p.name for p in m_procs}
        m_svcs = [s for s in services
                  if (sp := proc_by_pname.get(s.provided_by)) is not None
                  and _proc_on(sp, m.name)]
        # An application appears on THIS machine if any of its processes is bound
        # here (a split app like the `services` AA spans both boards) — not only
        # where its declared host_machine sits. Without this, the non-host board
        # gets an EMPTY application.json and the host board lists processes it
        # doesn't run (e.g. central listing compute's ucm/shwa). The per-machine
        # slice is the app's processes ∩ this machine's processes (below).
        m_apps = [a for a in apps
                  if a.host_machine == m.name
                  or (set(a.processes) & m_proc_names)]

        _dump(mdir / "machine.json", _machine_dict(m))
        _dump(mdir / "execution.json",
              {"processes": [_proc_dict(p, on_machine=m.name) for p in m_procs]})
        _dump(mdir / "service.json",
              {"instances": [_svc_dict(s) for s in m_svcs]})
        _dump(mdir / "application.json",
              {"applications": [_app_dict(a, m_proc_names) for a in m_apps]})

        # executor.json: a NESTED supervisor tree rooted at `root`, sliced to
        # this machine — the shape the C++ supervisor parses (spec.cpp:
        # load_node = has 'children' → supervisor, else worker leaf). Each
        # child name resolves to a nested SupervisorNode (recurse) or a process
        # bound here (a full worker leaf); names that are neither (a process on
        # another machine) drop out. Supervisor nodes whose subtree is empty on
        # this machine are pruned.
        def _build(node):
            kids = []
            for c in node.children:
                if c in sup_by_name:
                    sub = _build(sup_by_name[c])
                    if sub is not None:
                        kids.append(sub)
                elif c in m_proc_names:
                    kids.append(_worker_dict(proc_by_name[c]))
                # else: a process on another machine — skip.
            if not kids and node.name != "root":
                return None
            d = {
                "name": node.name,
                "strategy": node.strategy.value,
                "max_restarts": node.max_restarts,
                "max_seconds": node.max_seconds,
                "children": kids,
            }
            if node.tombstone_dir:
                d["tombstone_dir"] = node.tombstone_dir
            return d

        roots = [s for s in supervisors
                 if not any(s.name in o.children for o in supervisors)]
        if len(roots) == 1:
            executor_doc = _build(roots[0])
        elif roots:
            # Multiple roots (no single 'root') — wrap under a synthetic one so
            # the manifest still has the single-supervisor top the C++ loader
            # requires.
            executor_doc = {
                "name": "root", "strategy": "one_for_all",
                "max_restarts": 3, "max_seconds": 5,
                "children": [c for c in (_build(r) for r in roots)
                             if c is not None],
            }
        else:
            executor_doc = {"name": "root", "strategy": "one_for_all",
                            "max_restarts": 3, "max_seconds": 5, "children": []}
        # Stamp THIS machine's name on the tree root so the supervisor knows its
        # own machine identity (it reads executor.json anyway) and reports it in
        # GetSystemInfo — com then labels per-machine telemetry by the REAL name
        # without a separate THEIA_MACHINE_MANIFEST lookup.
        executor_doc["machine"] = m.name
        _dump(mdir / "executor.json", executor_doc)

        # config/<fc>.json — static params + etcd config-defaults per machine.
        # Emitted for every process on THIS machine from the PROCESS_PARAMS and
        # PROCESS_CONFIG_DEFAULTS sidecars captured at gen-manifest time.
        # User override: deploy/config/<machine>/<fc>.json (partial, same shape
        # as gen-params output) is deep-merged on top so per-rig knobs (e.g.
        # tsync central=GPS-grandmaster / compute=PTP-slave) don't require a
        # separate .art. This is what Ansible's seed-config.yml copies to the
        # device as-is — it expects a ready-to-ship file, not a base+override.
        if m_procs and (process_params or process_config_defaults):
            cfg_dir = mdir / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            _dump(cfg_dir / "executor.json", executor_doc)  # collocate sup manifest
            # Workspace root: the CWD at serialize-manifest time (deploy/ lives here).
            ws_root = Path(".")
            override_dir = ws_root / "deploy" / "config" / m.name
            for proc in m_procs:
                params_data = dict(process_params.get(proc.name, {}) or {})
                if not params_data:
                    params_data = {"package": "", "nodes": {}}
                # Deep-merge deploy/config/<machine>/<fc>.json override on top.
                override_file = override_dir / f"{proc.name}.json"
                if override_file.is_file():
                    try:
                        override = json.loads(override_file.read_text())
                        # Alias twins (gen-manifest "aliases": prototype →
                        # snake'd node type, for IMPORTED package nodes whose
                        # compiled kNodeName differs from the prototype). An
                        # override keyed by EITHER name must land in BOTH
                        # sections — the composition main reads the prototype
                        # key, the imported lib reads the type key; merging
                        # only one silently desyncs the pair.
                        twin: dict = {}
                        for a, b in (params_data.get("aliases") or {}).items():
                            twin[a] = b
                            twin[b] = a
                        # nodes section: merge each node's fields individually
                        # (into the key and, when aliased, its twin).
                        for node_name, node_vals in override.get("nodes", {}).items():
                            for key in {node_name, twin.get(node_name, node_name)}:
                                params_data.setdefault("nodes", {})[key] = {
                                    **params_data.get("nodes", {}).get(key, {}),
                                    **node_vals,
                                }
                        # top-level keys other than nodes (e.g. package) override directly.
                        for k, v in override.items():
                            if k != "nodes":
                                params_data[k] = v
                    except Exception:
                        pass
                # LOUD on clobber: this file is a BUILD ARTIFACT, re-derived
                # from the .art (+ deploy/config override) on EVERY
                # manifest/install. A hand edit made directly to the staged
                # copy is silently reverted here — which reads as "my param
                # didn't take" (it took, then this overwrote it). Warn when we
                # are about to replace content that differs from what we
                # write, and name the supported override channel.
                cfg_path = cfg_dir / f"{proc.name}.json"
                try:
                    old = json.loads(cfg_path.read_text())
                except Exception:
                    old = None
                if old is not None and old != params_data:
                    click.echo(
                        f"  NOTE: {cfg_path} regenerated from the .art — its "
                        f"previous (possibly hand-edited) content is replaced. "
                        f"Persistent per-rig knobs belong in "
                        f"{override_dir / (proc.name + '.json')} "
                        f"(deep-merged, alias-mirrored, survives every restage).",
                        err=True)
                _dump(cfg_path, params_data)
            # config-defaults.json — merged across all processes on this machine.
            # seed.py reads a single per-machine file so we merge all FCs' configs.
            if process_config_defaults:
                merged_configs: dict = {}
                merged_pkg = ""
                for proc in m_procs:
                    cd = process_config_defaults.get(proc.name) or {}
                    merged_pkg = merged_pkg or cd.get("package", "")
                    merged_configs.update(cd.get("configs", {}))
                if merged_configs:
                    _dump(cfg_dir / "config-defaults.json",
                          {"package": merged_pkg, "configs": merged_configs})

    for p in written:
        click.echo(str(p))


# -----------------------------------------------------------------------------
# gui — GUI-side manifests (small endpoint list, one machine per row)
# -----------------------------------------------------------------------------


@main.group("gui", help="Supervisor-GUI manifest commands.")
def gui() -> None:
    pass


@gui.command(
    "emit",
    help="Emit the GUI manifest (machines.json) for a vehicle rig. "
    "TARGET is a dotted import path to a module exporting a DeploymentLayer "
    "on the orthogonal-ARA engine. Output lists each target machine's com "
    "gRPC endpoint — the GUI opens one gRPC channel per row.",
)
@click.argument("target")
@click.option(
    "--attr",
    "attr",
    default=None,
    help="Name of the DeploymentLayer attribute in the module. "
    "Defaults to DEPLOYMENT/RIG, then any DeploymentLayer-typed export.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write the JSON. Defaults to stdout.",
)
def gui_emit(target: str, attr: str | None, out_file: str | None) -> None:
    """Emit machines.json — one row per TARGET machine (a machine that hosts
    at least one process). HOST/admin machines (no processes — the GUI runs ON
    them) are skipped. Each row carries the machine's com gRPC endpoint: the
    MachineTarget.com_endpoint (address, port) when set, else 127.0.0.1:7700."""
    import json

    _module, dep = _load_deployment(target, attr)
    td = dep.simplify()

    # Machines that host ≥1 process are the observable targets; the rest are
    # host/admin consoles the GUI itself runs on.
    # Effective placement {machine} ∪ machines per process — a fanned-out
    # host-monitor (machines-set, no scalar) still counts its boards as targets.
    machines_with_procs = {
        mn
        for p in td.execution.processes
        for mn in ((set(getattr(p, "machines", None) or ()))
                   | ({p.machine} if p.machine else set()))
    }

    rows: list[dict] = []
    for m in td.machines.machines:
        if m.name not in machines_with_procs:
            continue
        ep = m.com_endpoint  # optional (address, port) tuple, or None
        address, port = "127.0.0.1", 7700
        if ep is not None:
            try:
                address, port = str(ep[0]), int(ep[1])
            except (TypeError, ValueError, IndexError):
                pass
        rows.append({"name": m.name, "address": address, "port": port})

    rows.sort(key=lambda r: r["name"])
    doc = {"machines": rows}
    text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
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
    "Bazel rig() module extension to wire process bazel-target refs "
    "into per-machine deploy bundles, and by rf-theia (the typed Rig in "
    "rf_theia.runtime.rig). TARGET is a dotted module exporting a "
    "DeploymentLayer on the orthogonal-ARA engine.",
)
@click.argument("target")
@click.option(
    "--attr",
    "attr",
    default=None,
    help="Name of the DeploymentLayer attribute in the module. "
    "Defaults to DEPLOYMENT/RIG, then any DeploymentLayer-typed export.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write the JSON. Defaults to stdout.",
)
def rig_deps(target: str, attr: str | None, out_file: str | None) -> None:
    """Emit a JSON describing the rig (the rf-theia / Bazel rig-deps contract):

      {
        "vehicle": {"name": "...", "make": "theia", "model": "workspace"},
        "machines": [
          {"name": "central", "kind": "target", "arch": "amd64",
           "applications": [
             {"name": "apps", "components": [
                {"name": "p1", "bazel_target": "//apps/...:apps",
                 "owner": "platform", "art_node": "", "bazel_buildable": true},
                ...]}]}
        ],
        "flat_components": [
          {"name": "p1", "bazel_target": "//apps/...:apps", "machine": "central",
           "owner": "platform", "art_node": ""},
          ...]
      }

    Mapping from the DeploymentTarget: each machine's applications are the
    ApplicationTargets whose host_machine == that machine; each application's
    components are its bundled processes (name + executable bazel-target,
    looked up in the execution axis). The flat list is every component across
    machines with its machine binding.
    """
    import json

    _module, dep = _load_deployment(target, attr)
    td = dep.simplify()

    # Convert the AUTOSAR-style arch token (x86_64 / aarch64) → the dpkg-style
    # token Bazel + downstream packaging want (amd64 / arm64 / armhf).
    _DPKG_ARCH = {
        "x86_64":  "amd64",
        "aarch64": "arm64",
        "armv7":   "armhf",
        "riscv64": "riscv64",
    }

    procs_by_name = {p.name: p for p in td.execution.processes}
    machines = list(td.machines.machines)
    apps = list(td.applications.applications)

    # Vehicle identity. name from a VEHICLE_NAME/NAME export, else the module's
    # leaf segment (or the parent segment when the leaf is the generic "rig",
    # e.g. manifest.single.rig → "single"); make/model default to workspace.
    def _name_from_target() -> str:
        parts = target.split(".")
        leaf = parts[-1]
        if leaf == "rig" and len(parts) >= 2:
            return parts[-2]
        return leaf

    veh_name = (getattr(_module, "VEHICLE_NAME", None)
                or getattr(_module, "NAME", None)
                or _name_from_target())
    vehicle = {
        "name": str(veh_name),
        "make": str(getattr(_module, "VEHICLE_MAKE", "theia")),
        "model": str(getattr(_module, "VEHICLE_MODEL", "workspace")),
    }

    # Which machines host ≥1 process → "target"; the rest are "host" consoles.
    # Effective placement {machine} ∪ machines per process — a fanned-out
    # host-monitor (machines-set, no scalar) still counts its boards as targets.
    machines_with_procs = {
        mn
        for p in td.execution.processes
        for mn in ((set(getattr(p, "machines", None) or ()))
                   | ({p.machine} if p.machine else set()))
    }

    def _component_dict(proc_name: str) -> dict:
        p = procs_by_name.get(proc_name)
        executable = p.executable if p is not None else ""
        return {
            "name": proc_name,
            "bazel_target": executable,
            "owner": "platform",
            "art_node": "",
            # A "//..."-shaped executable is a buildable Bazel label.
            "bazel_buildable": bool(executable) and executable.startswith("//"),
        }

    apps_by_machine: dict[str, list] = {m.name: [] for m in machines}
    for app in apps:
        host = app.host_machine
        if host not in apps_by_machine:
            apps_by_machine[host] = []
        apps_by_machine[host].append(app)

    machines_json = []
    for m in machines:
        apps_json = []
        for app in sorted(apps_by_machine.get(m.name, []), key=lambda a: a.name):
            apps_json.append({
                "name": app.name,
                "components": [_component_dict(pn)
                              for pn in sorted(app.processes)],
            })
        machines_json.append({
            "name": m.name,
            "kind": "target" if m.name in machines_with_procs else "host",
            "arch": _DPKG_ARCH.get(m.arch, "amd64"),
            "applications": apps_json,
        })

    # Flat list: every component across every machine, with its binding.
    flat_components = []
    for m in machines:
        for app in sorted(apps_by_machine.get(m.name, []), key=lambda a: a.name):
            for pn in sorted(app.processes):
                c = _component_dict(pn)
                flat_components.append({
                    "name": c["name"],
                    "bazel_target": c["bazel_target"],
                    "machine": m.name,
                    "owner": c["owner"],
                    "art_node": c["art_node"],
                })

    doc = {
        "vehicle": vehicle,
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
