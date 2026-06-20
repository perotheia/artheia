"""Generate ``services/manifest/service.py`` from a system ``.art``.

The manifest module is emitted from the ``.art`` spec, not a hand-kept
Python list — the ``.art`` is the source of truth. AUTOSAR Adaptive
(ARA) splits a deployment into four manifest kinds; this generator
emits a module organised around them:

  * **Application** — one section per ``cluster`` in the ``.art``. Each
    cluster contributes its members as SwComponent + Executable +
    Process triples, under a ``<Cluster>_*`` group named LITERALLY after
    the cluster (``Services``, ``Applications``, ``Platform``, …). The
    generator assigns no meaning to a cluster name — it just emits one
    section per cluster found.
  * **Machine** — empty here. Machines are a deploy-time concern filled
    in by rig layers (``apps/manifest/rig.py``), never by the spec.
  * **Service** — the ServiceManifest instances; derived by the loader
    from the same cluster members (kept in ``platform.py``).
  * **Execution** — the Processes (one per cluster member).

A cluster member's handle (the ``ident`` in ``composition Com com``) is
the FC/app short name; ``ClusterMember.name`` carries it. Members that
aren't in any cluster (daemon-less placeholders: crypto, idsm, …) never
reach the manifest.

Everything except the per-cluster member lists is fixed boilerplate
emitted verbatim, so the generated file stays diff-stable.
"""

from __future__ import annotations

from pathlib import Path

from artheia.model import parse_file
from artheia.model.scope import _import_dir, _PKG_FILE_PRIORITY


def _extract_package(art_file: str) -> str:
    """The `package <fqn>` line of *art_file* (empty if none)."""
    for line in Path(art_file).read_text().splitlines():
        s = line.strip()
        if s.startswith("package "):
            return s[len("package "):].split("//")[0].strip()
    return ""


def _extract_imports(art_file: str) -> "list[str]":
    """The bare import FQNs of *art_file* (`import a.b.*` → `a.b`)."""
    out = []
    for line in Path(art_file).read_text().splitlines():
        s = line.strip()
        if s.startswith("import "):
            fqn = s[len("import "):].split("//")[0].strip()
            out.append(fqn[:-2] if fqn.endswith(".*") else fqn)
    return out


def _base_dir_for(defining_file: Path, workspace: "Path | None" = None) -> str:
    """The source-tree base_dir a cluster's gen-app members hang off — the
    WORKSPACE-RELATIVE TOP-LEVEL directory of the file that DEFINES the cluster
    (after symlink resolution). The bazel-target prefix is //<base_dir>/…:

      services/system/cluster.art            → "services"
      app/system/app/component.art            → "app"
      platform/supervisor/system/package.art  → "platform"

    When the resolved top is the aggregator's own `system/` dir (a real dir whose
    <pkg>/cluster.art just bundles FCs that live elsewhere — system/services/…),
    fall back to the package's LAST segment (system.services → services).
    Workspace root = the dir with MODULE.bazel/WORKSPACE above the file."""
    real = defining_file.resolve()
    root = workspace
    if root is None:
        p = real.parent
        while p != p.parent:
            if (p / "MODULE.bazel").exists() or (p / "WORKSPACE").exists() \
                    or (p / "WORKSPACE.bazel").exists():
                root = p
                break
            p = p.parent
    top = ""
    if root is not None:
        try:
            rel = real.relative_to(root)
            if rel.parts:
                top = rel.parts[0]
        except ValueError:
            pass
    if top and top != "system":
        return top
    pkg = _extract_package(str(defining_file))
    if pkg:
        return pkg.split(".")[-1]
    return top or real.parent.name


def _extract_members(model, cluster_el):
    """[(ident, composition, [nodes]), …] for one ClusterDecl element."""
    members = []
    for mem in getattr(cluster_el, "elements", []):
        if type(mem).__name__ != "ClusterMember":
            continue
        ident = getattr(mem, "name", None) or getattr(mem, "instance_name", None)
        if not ident:
            continue
        composition = getattr(getattr(mem, "type", None), "name", None) or ""
        nodes = _composition_nodes(model, composition) if composition else []
        members.append((ident, composition, nodes))
    return members


def _members_of_cluster_in(art_file: str, cluster_name: str):
    """Members of *cluster_name* declared in *art_file* (parsed standalone)."""
    try:
        model = parse_file(art_file)
    except Exception:
        return []
    for el in getattr(model, "elements", []):
        if type(el).__name__ == "ClusterDecl" and el.name == cluster_name:
            return _extract_members(model, el)
    return []


def _pkg_cluster(art_file: "str | Path") -> str:
    """The PACKAGE cluster of *art_file* — the segment after ``system.`` in its
    ``package`` decl (``system.app`` → ``app``, ``system.services.com`` →
    ``services``). This is the cluster the ``art_node`` must name so the
    supervisor resolves each node's TIPC addr from
    ``system/<cluster>/component.art`` — DISTINCT from base_dir (the source
    DIRECTORY, e.g. ``app`` for an app whose package is ``system.app``)."""
    pkg = _extract_package(str(art_file))
    parts = pkg.split(".") if pkg else []
    # system.<cluster>[.<ident>...] → <cluster>; bare <cluster> → itself.
    if len(parts) >= 2 and parts[0] == "system":
        return parts[1]
    return parts[-1] if parts else ""


def _resolve_cluster_members_via_imports(art_file: str, cluster_name: str):
    """An empty cluster STUB in an aggregator (system/system.art) is materialized
    from an `import`. Walk the imports, resolve each to its package dir (the
    symlinked `system/` layout), parse the defining file, and return that
    cluster's real (members, base_dir, pkg_cluster) — base_dir is the SOURCE-TREE
    root the members live under (bazel-target prefix); pkg_cluster is the .art
    PACKAGE cluster (the art_node cluster). Layout-independent (reuses
    scope._import_dir). Returns ([], "", "") if unresolved."""
    entry = Path(art_file)
    entry_pkg = _extract_package(art_file)
    for imp_pkg in _extract_imports(art_file):
        pkg_dir = _import_dir(entry, entry_pkg, imp_pkg)
        if pkg_dir is None or not pkg_dir.is_dir():
            continue
        for fname in _PKG_FILE_PRIORITY:
            cand = pkg_dir / fname
            if not cand.is_file():
                continue
            members = _members_of_cluster_in(str(cand), cluster_name)
            if members:
                return members, _base_dir_for(cand), _pkg_cluster(cand)
    return [], "", ""


def _model_defines_composition(model, members) -> bool:
    """True if the model carries the FULL body (prototypes) of the members'
    compositions — i.e. they're defined HERE, not forward-decl'd from imports."""
    for _ident, comp, _nodes in members:
        if comp and _composition_nodes(model, comp):
            return True
    return False


def _base_dir_for_composition(art_file: str, composition: str) -> str:
    """base_dir for a member *composition* forward-decl'd in the aggregator —
    walk imports to the file that defines it with a body, take its base_dir."""
    entry = Path(art_file)
    entry_pkg = _extract_package(art_file)
    for imp_pkg in _extract_imports(art_file):
        pkg_dir = _import_dir(entry, entry_pkg, imp_pkg)
        if pkg_dir is None or not pkg_dir.is_dir():
            continue
        for fname in _PKG_FILE_PRIORITY:
            cand = pkg_dir / fname
            if not cand.is_file():
                continue
            try:
                m = parse_file(str(cand))
            except Exception:
                continue
            if _composition_nodes(m, composition):
                return _base_dir_for(cand)
    return ""


def _pkg_cluster_for_composition(art_file: str, composition: str) -> str:
    """Package cluster (art_node) for a member *composition* forward-decl'd in
    the aggregator — walk imports to the file that DEFINES it with a body, take
    its package cluster. Sibling of :func:`_base_dir_for_composition`."""
    entry = Path(art_file)
    entry_pkg = _extract_package(art_file)
    for imp_pkg in _extract_imports(art_file):
        pkg_dir = _import_dir(entry, entry_pkg, imp_pkg)
        if pkg_dir is None or not pkg_dir.is_dir():
            continue
        for fname in _PKG_FILE_PRIORITY:
            cand = pkg_dir / fname
            if not cand.is_file():
                continue
            try:
                m = parse_file(str(cand))
            except Exception:
                continue
            if _composition_nodes(m, composition):
                return _pkg_cluster(cand)
    return ""


# One cluster member, fully derived from the .art:
#   ident       — the member handle (``p1``), == app dir / target / bin name.
#   composition — the .art composition it instantiates (member.type.name).
#   nodes       — the composition's hosted prototype names (its GenServers).
Member = "tuple[str, str, list[str]]"


def _composition_nodes(model, composition_name: str) -> "list[str]":
    """Prototype (node) names hosted by *composition_name* in *model*."""
    for el in getattr(model, "elements", []):
        if (
            type(el).__name__ == "CompositionDecl"
            and el.name == composition_name
        ):
            return [
                p.name
                for p in getattr(el, "elements", [])
                if type(p).__name__ == "PrototypeDecl"
                and getattr(p, "name", None)
            ]
    return []


def _cluster_members(art_file: str) -> "list[tuple[str, str, str, list]]":
    """Return ``[(cluster_name, base_dir, pkg_cluster,
    [(ident, composition, [nodes]), ...]), ...]`` for every ``cluster`` declared
    in *art_file*, in source order. ``base_dir`` is the SOURCE DIR (bazel-target
    prefix); ``pkg_cluster`` is the .art PACKAGE cluster (the art_node cluster) —
    they DIFFER when a cluster lives in a dir != its package (e.g. dir
    ``app``, package ``system.app``).

    A member's ``ident`` is ``ClusterMember.name``; ``composition`` is
    ``member.type.name``; ``nodes`` are its hosted prototype names — all read
    straight from the .art.

    Works on a single ``component.art`` (members inline, base_dir="" → caller's
    default) AND on the workspace AGGREGATOR (``system/system.art``), where a
    cluster is an empty forward-decl STUB materialized from an ``import``. Those
    stubs are resolved by walking the imports to the defining file, and each
    carries its own SOURCE-TREE base_dir (services vs apps vs platform) so its
    bazel targets get the right prefix — Theia derives the manifest from the
    system/ layout, not a hardcoded module name. Raises ``ValueError`` if no
    cluster is declared.
    """
    model = parse_file(art_file)
    out: list[tuple[str, str, str, list]] = []
    for el in getattr(model, "elements", []):
        if type(el).__name__ != "ClusterDecl":
            continue
        members = _extract_members(model, el)
        # Inline members defined in THIS file → derive base_dir from the file's
        # own source-tree location (the bazel-target prefix), NOT the caller's
        # output-dir heuristic. _base_dir_for handles the consuming-workspace
        # case where the .art sits at system/apps/component.art (top resolves to
        # `system`, so it falls back to the package last-segment → `apps`); the
        # heuristic that keys off the manifest OUTPUT dir (manifest/) would yield
        # '' and emit broken `///<App>` bazel targets. Overridden below for the
        # aggregator-stub + import-forward-decl cases.
        base_dir = _base_dir_for(Path(art_file)) if members else ""
        # pkg_cluster: the .art PACKAGE cluster (art_node), distinct from base_dir.
        # Inline members live in THIS file → its package; resolved later otherwise.
        pkg_cluster = _pkg_cluster(art_file)
        if not members:
            # Aggregator stub → resolve real members + their source-tree base_dir
            # + the defining file's package cluster.
            members, base_dir, pkg_cluster = _resolve_cluster_members_via_imports(
                art_file, el.name)
        elif not _model_defines_composition(model, members):
            # Inline cluster whose member compositions are forward-decl'd from
            # imports (Platform's Supervisor/GatewayBridge) → derive base_dir
            # AND the package cluster from where the first composition is defined.
            if members:
                base_dir = _base_dir_for_composition(art_file, members[0][1])
                pkg_cluster = (_pkg_cluster_for_composition(art_file, members[0][1])
                               or pkg_cluster)
        out.append((el.name, base_dir, pkg_cluster, members))
    if not out:
        raise ValueError(
            f"{art_file}: no `cluster` declaration found — nothing to "
            f"generate"
        )
    return out


def _py_ident(cluster_name: str) -> str:
    """Uppercase, identifier-safe prefix for a cluster's section vars."""
    return "".join(c if c.isalnum() else "_" for c in cluster_name).upper()


# ---------------------------------------------------------------------------
# File template. Two substitution points:
#   {source}   — provenance (the .art path)
#   {sections} — the per-cluster member-short blocks + the aggregate
#                lists, rendered by _render_sections().
# Everything else is fixed boilerplate.
# ---------------------------------------------------------------------------

_HEADER = '''\
"""Adaptive Platform manifest — GENERATED from {source}.

Do not edit by hand. Edit the ``cluster`` declarations in the source
``.art`` and regenerate:

    artheia gen-manifest-proto {source} <this file>

ARA manifest sections (see docs/autosar/manifest.md):

  * Application — one ``<Cluster>_*`` group per ``cluster`` in the .art
                  (SwComponent + Executable + Process per member).
  * Machine     — empty; rig layers (apps/manifest/rig.py) fill it.
  * Service     — ServiceManifest instances (loader-derived in
                  platform.py from the same cluster members).
  * Execution   — Processes (one per cluster member).

Upper layers patch this base by name (:class:`Override`) — see
``apps/manifest/rig.py``.
"""

from __future__ import annotations

# The per-cluster section builders live in artheia.manifest.utils so the
# generated file stays small and the build logic can evolve without
# regenerating. Imported under the leading-underscore names the sections
# below call.
from artheia.manifest.layer import Layer
from artheia.manifest.utils import (
    app_component_for,
    app_process_for,
    component_for as _component_for,
    executable_for as _executable_for,
    process_for as _process_for,
)


'''

_FOOTER = '''\

# ---------------------------------------------------------------------------
# Machine section — EMPTY. Machines are a deploy-time concern; rig layers
# (apps/manifest/rig.py) add MachineManifests. The spec declares none.
# ---------------------------------------------------------------------------
MACHINES: list = []


# ---------------------------------------------------------------------------
# Supervisor tree — SIDECARED in the sibling ``executor.py`` (same package).
#
# The supervisor hierarchy (restart strategies + child grouping) has NO .art
# declaration, so it lives in a sidecar that survives regeneration of THIS file.
# gen-manifest emits executor.py alongside this module; we re-export its
# SUPERVISORS so consumers read ``<this>.SUPERVISORS`` unchanged. Edit the tree
# in executor.py. (For an apps manifest the sidecar is a single ``app_sup`` node
# with the app members as children; the full platform tree is the services
# sidecar — the rig combines them.)
# ---------------------------------------------------------------------------

from .executor import SUPERVISORS  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Per-cluster Layer + SoftwareSpecification. One pair per .art cluster,
# named after the cluster (`<Cluster>Layer` / `<Cluster>Software`). The
# layer's `name=` is the lowercased cluster name. Upper layers (rig.py)
# compose against these — e.g. `AppSoftware = ApplicationsSoftware.
# mappend(AppSpecLayer)`.
# ---------------------------------------------------------------------------

from typing import cast

from artheia.manifest.application import ApplicationManifest
from artheia.manifest.layer import Layer  # noqa: E402,F811
from artheia.manifest.rig import SoftwareSpecification, VehicleIdentity
from artheia.manifest.applicative import Append, SetTransformTypes  # noqa: E402

{layers}

__all__ = [
{exports}
    "MACHINES",
    "COMPONENTS",
    "EXECUTABLES",
    "PROCESSES",
    "SUPERVISORS",
]
'''


def _pascal(cluster_name: str) -> str:
    """PascalCase, identifier-safe form of a cluster name for the
    ``<Cluster>Layer`` / ``<Cluster>Software`` symbols. ``Services`` →
    ``Services``; ``my-apps`` → ``MyApps``."""
    parts = [p for p in "".join(
        c if c.isalnum() else " " for c in cluster_name
    ).split() if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Cluster"


def _render_sections(
    clusters: "list[tuple[str, list]]",
    base_dir: str,
) -> "tuple[str, str, str]":
    """Render the per-cluster sections + the aggregate lists + the
    per-cluster Layer/Software definitions.

    *base_dir* is the manifest module's directory (e.g. ``app``); it +
    each member ident drive the directory-convention paths (app dir,
    bazel target, start_cmd) — see :mod:`artheia.manifest.utils`.

    Returns ``(sections_block, exports_block, layers_block)``.
    """
    lines: list[str] = []
    export_names: list[str] = []
    all_prefixes: list[str] = []
    layer_lines: list[str] = []

    for name, cluster_base_dir, pkg_cluster, members in clusters:
        bdir = cluster_base_dir or base_dir   # per-cluster, else the default
        # art_node cluster (the .art package), distinct from bdir (source dir).
        acluster = pkg_cluster or bdir
        pfx = _py_ident(name)          # SERVICES — section var prefix
        cls = _pascal(name)            # Services — Layer/Software symbol
        lname = name.lower()           # services — Layer.name= string
        all_prefixes.append(pfx)
        # Each member is (ident, composition, [nodes]) — all from the .art.
        members_lit = ",\n".join(
            f"    ({ident!r}, {comp!r}, {nodes!r})"
            for ident, comp, nodes in members
        )
        lines.append(
            f"# ---------------------------------------------------------"
            f"------------------\n"
            f"# Application section — cluster `{name}`.\n"
            f"# Each member: (ident, composition, [hosted node names]) "
            f"— all from the .art.\n"
            f"# Build/deploy paths derive from (base_dir={bdir!r}, "
            f"ident) via the\n"
            f"# directory convention (artheia.manifest.utils).\n"
            f"# ---------------------------------------------------------"
            f"------------------\n"
            f"{pfx}_MEMBERS: list[tuple[str, str, list[str]]] = [\n"
            f"{members_lit}\n"
            f"]\n"
            f"{pfx}_SHORTS = [m[0] for m in {pfx}_MEMBERS]\n"
            f"{pfx}_COMPONENTS = [\n"
            f"    app_component_for({bdir!r}, ident, comp, {acluster!r})\n"
            f"    for ident, comp, _ in {pfx}_MEMBERS\n"
            f"]\n"
            f"{pfx}_EXECUTABLES = [_executable_for(ident) "
            f"for ident, _, _ in {pfx}_MEMBERS]\n"
            f"{pfx}_PROCESSES = [\n"
            f"    app_process_for({bdir!r}, ident, nodes)\n"
            f"    for ident, _, nodes in {pfx}_MEMBERS\n"
            f"]\n"
        )
        for suffix in ("MEMBERS", "SHORTS", "COMPONENTS",
                       "EXECUTABLES", "PROCESSES"):
            export_names.append(f"{pfx}_{suffix}")

        # Per-cluster Layer + SoftwareSpecification, named after the
        # cluster. The supervisor tree is attached to every cluster's
        # layer (it's package-wide, sidecared in executor.py).
        layer_lines.append(
            f"# cluster `{name}` → {cls}Layer / {cls}Software.\n"
            f"{cls}Layer = Layer(\n"
            f'    name="{lname}",\n'
            f"    add_components={pfx}_COMPONENTS,\n"
            f"    add_executions={pfx}_PROCESSES,\n"
            f"    add_supervisors=SUPERVISORS,\n"
            f")\n"
            f"_{cls}App = ApplicationManifest(\n"
            f'    name="{lname}_app",\n'
            f'    host_machine="",  # rig layers fill in\n'
            f"    components=list({pfx}_COMPONENTS),\n"
            f")\n"
            f"{cls}Software: SoftwareSpecification = SoftwareSpecification(\n"
            f'    vehicle=VehicleIdentity(name=""),  # rig layers override\n'
            f"    applications=cast(set[SetTransformTypes], {{\n"
            f"        Append(_{cls}App),\n"
            f"    }}),\n"
            f"    execution_manifests=cast(set[SetTransformTypes], {{\n"
            f"        Append(p) for p in {pfx}_PROCESSES\n"
            f"    }}),\n"
            f"    supervisors=cast(set[SetTransformTypes], {{\n"
            f"        Append(s) for s in SUPERVISORS\n"
            f"    }}),\n"
            f")\n"
        )
        export_names.append(f"{cls}Layer")
        export_names.append(f"{cls}Software")

    # Aggregate lists span every cluster (consumers that want everything).
    comp = " + ".join(f"{p}_COMPONENTS" for p in all_prefixes) or "[]"
    exe = " + ".join(f"{p}_EXECUTABLES" for p in all_prefixes) or "[]"
    proc = " + ".join(f"{p}_PROCESSES" for p in all_prefixes) or "[]"
    lines.append(
        "# ---------------------------------------------------------"
        "------------------\n"
        "# Aggregate across all clusters (every component / process).\n"
        "# ---------------------------------------------------------"
        "------------------\n"
        f"COMPONENTS = {comp}\n"
        f"EXECUTABLES = {exe}\n"
        f"PROCESSES = {proc}\n"
    )

    exports = "\n".join(f'    "{n}",' for n in export_names)
    layers = "\n".join(layer_lines)
    return "\n".join(lines), exports, layers


# Apps supervisor-tree sidecar (generated next to applications.py). One
# ``app_sup`` SupervisorNode (one_for_one) with every app member as a child —
# the mount point the rig grafts onto the services tree's empty app_sup. The
# generated applications.py imports SUPERVISORS from here (`from .executor`).
_EXECUTOR_SIDECAR = '''\
"""Apps supervisor-tree sidecar — GENERATED from {source} by gen-manifest.

One ``app_sup`` node (one_for_one) whose children are this manifest's app
members. The generic rig grafts it onto the services supervisor tree's empty
``app_sup`` mount. Regenerate with applications.py (gen-manifest); the app set is
.art-derived, so DON'T hand-edit the children — change the cluster in the .art.
"""
from __future__ import annotations

from artheia.manifest.supervisor import RestartStrategy, SupervisorNode

# Every app member from the generated cluster sections (one_for_one: apps are
# peer-independent, like the platform app_sup).
SUPERVISORS: list[SupervisorNode] = [
    SupervisorNode(
        name="app_sup",
        strategy=RestartStrategy.ONE_FOR_ONE,
        children={children!r},
    ),
]
'''


def _app_shorts(clusters) -> "list[str]":
    """Every member ident across all clusters (the app_sup children)."""
    out: list[str] = []
    for _name, _bdir, _cluster, members in clusters:
        out.extend(ident for ident, _comp, _nodes in members)
    return out


def generate_manifest_proto(art_file: str, out_file: str) -> Path:
    """Render the manifest module from *art_file*'s clusters and write it to
    *out_file*; ALSO emit the sibling ``executor.py`` supervisor-tree sidecar
    (one ``app_sup`` node with the app members as children). Returns the
    applications.py path.

    The application directory-convention is keyed on *base_dir* — the name of the
    directory holding the manifest module (e.g. ``apps`` for
    ``apps/manifest/applications.py``). That's the source-tree root each member's
    ``<base_dir>/<ident>`` app dir hangs off.
    """
    out = Path(out_file)
    parent = out.parent
    base_dir = parent.parent.name if parent.name == "manifest" else parent.name

    clusters = _cluster_members(art_file)
    sections, exports, layers = _render_sections(clusters, base_dir)
    rendered = (
        _HEADER.format(source=art_file)
        + sections
        + _FOOTER.format(exports=exports, layers=layers)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)

    # Sidecar: executor.py next to applications.py, with the app_sup node.
    sidecar = out.parent / "executor.py"
    sidecar.write_text(
        _EXECUTOR_SIDECAR.format(source=art_file, children=_app_shorts(clusters)))
    # Ensure the package is importable (relative `from .executor`).
    init = out.parent / "__init__.py"
    if not init.exists():
        init.write_text("")
    return out
