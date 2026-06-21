"""``.art`` → cluster-member derivation shared by the manifest generators.

Lifted out of the (now-removed) legacy ``manifest_proto`` generator: the
``.art`` → ``(cluster, base_dir, pkg_cluster, members)`` derivation and the
bazel-target prefix logic. ``gen-manifest`` (:mod:`manifest_gen`) reuses these
to map a system ``.art`` onto the orthogonal-ARA deployment model.

A cluster member's handle (the ``ident`` in ``composition Com com``) is the
FC/app short name; ``ClusterMember.name`` carries it. Members that aren't in
any cluster never reach the manifest.

These helpers depend only on the textX model layer (:mod:`artheia.model`) — no
manifest dataclasses — so the composition engine can change underneath them.
"""

from __future__ import annotations

import os
from pathlib import Path

from artheia.model import parse_file
from artheia.model.scope import _import_dir, _PKG_FILE_PRIORITY


# ---------------------------------------------------------------------------
# .art package / import scanning.
# ---------------------------------------------------------------------------


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
      apps/system/apps/component.art          → "apps"
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


# ---------------------------------------------------------------------------
# Cluster + member extraction.
# ---------------------------------------------------------------------------


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
    ``package`` decl (``system.demo`` → ``demo``, ``system.services.com`` →
    ``services``). This is the cluster the ``art_node`` must name so the
    supervisor resolves each node's TIPC addr from
    ``system/<cluster>/component.art`` — DISTINCT from base_dir (the source
    DIRECTORY, e.g. ``apps`` for the demo whose package is ``system.demo``)."""
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


def _cluster_members(art_file: str) -> "list[tuple[str, str, str, list]]":
    """Return ``[(cluster_name, base_dir, pkg_cluster,
    [(ident, composition, [nodes]), ...]), ...]`` for every ``cluster`` declared
    in *art_file*, in source order. ``base_dir`` is the SOURCE DIR (bazel-target
    prefix); ``pkg_cluster`` is the .art PACKAGE cluster (the art_node cluster) —
    they DIFFER when a cluster lives in a dir != its package (the demo: dir
    ``apps``, package ``system.demo``).

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
        base_dir = ""   # inline members → caller's default (output-path base_dir)
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


# ---------------------------------------------------------------------------
# Bazel-target derivation (lifted from the removed manifest.utils).
# ---------------------------------------------------------------------------


def _prebuilt_owners() -> "frozenset[str]":
    """Cluster owners (base_dir) whose binaries are INSTALLED, not built — listed
    in $THEIA_PREBUILT_OWNERS (comma/space-separated)."""
    raw = os.environ.get("THEIA_PREBUILT_OWNERS", "")
    return frozenset(t for t in raw.replace(",", " ").split() if t)


def app_dir(base_dir: str, ident: str) -> str:
    """Source dir for an application member: ``<base_dir>/<ident>``."""
    return f"{base_dir}/{ident}"


def app_bazel_target(base_dir: str, ident: str, composition: str,
                     cluster: "str | None" = None) -> str:
    """Bazel label for a gen-app member's binary. Two layout conventions, by
    base_dir:

      - services FCs:  ``//services/<ident>/main:<ident>``  (per-FC dir + binary
        named after the FC short — e.g. log → //services/log/main:log).
      - app members:   ``//<base_dir>/<composition>/main:<cluster>`` (per-composition
        app dir whose cc_binary is named after the .art PACKAGE cluster — gen-app
        names the binary ``model.fc_short`` = the package's last segment. e.g.
        apps/Demo3WayP1 → //apps/Demo3WayP1/main:apps for package ``system.apps``).

    ``cluster`` is the package's last segment (= the generated binary name).
    Defaults to ``base_dir`` for back-compat (the common case dir==cluster).
    """
    if base_dir == "services":
        return f"//services/{ident}/main:{ident}"
    return f"//{base_dir}/{composition}/main:{cluster or base_dir}"
