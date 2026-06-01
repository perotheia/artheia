"""Import-following scope provider for Artheia cross-references.

textX resolves a `[Type|FQN]` cross-reference against the *current* model
only. Artheia models are split across packages and stitched together with
`import <pkg>.*` lines (the .art analogue of C++ `#include` for symbol
visibility). This module registers a textX scope provider that, when a
reference can't be resolved locally, follows the importing model's
`import` lines into the imported packages and resolves the name there.

Resolution rules (matching the user-facing contract):

  - A reference may be written **bare** (`requires EML_01_Iface`) when the
    name is unique across everything in scope (the local model + all
    imported packages). The provider returns the single match.

  - A reference may be written **fully-qualified**
    (`requires system.autosar.mlbevo_gen2.flexray.EML_01_Iface`) to pick a
    specific package. This is REQUIRED when a bare name is ambiguous —
    e.g. `Licht_hinten_01_Iface` exists in both the `.kcan` and `.flexray`
    sub-packages. A bare reference to an ambiguous name raises a
    `TextXSemanticError` naming the candidate packages.

  - `extern` forward-declarations in the local model are NOT resolution
    targets — they are placeholders that themselves resolve to the
    imported real definition. The provider skips them when indexing the
    local model so a bare reference jumps straight to the real decl.

The package→directory mapping reuses the same relative-climb logic the
forward-decl resolver in :mod:`artheia.cli` uses (see ``_import_dir``),
so the symlinked workspace layout resolves identically whether a file is
reached via its canonical symlinked path or its real path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from textx import TextXSemanticError
from textx.scoping.providers import PlainName
from textx.scoping.tools import get_model


# ---------------------------------------------------------------------------
# package FQN -> directory (relative climb from the importing file)
# ---------------------------------------------------------------------------


def _import_dir(entry: Path, entry_pkg: str, import_pkg: str) -> Optional[Path]:
    """Resolve ``import <import_pkg>`` to a directory, relative to the
    importing file's own package + location.

    Layout-independent: never reconstructs a global workspace root, so it
    works whether the entry was reached via the canonical symlinked path
    or via the symlink target. (Identical algorithm to
    ``artheia.cli._import_dir`` — kept here to avoid a cli<-model import
    cycle.)
    """
    entry_dir = entry.parent
    e_parts = entry_pkg.split(".") if entry_pkg else []
    i_parts = import_pkg.split(".") if import_pkg else []

    common = 0
    for a, b in zip(e_parts, i_parts):
        if a != b:
            break
        common += 1

    up = len(e_parts) - common
    base = entry_dir
    for _ in range(up):
        base = base.parent

    p = base
    for seg in i_parts[common:]:
        p = p / seg
    return p


_PKG_FILE_PRIORITY = ("system.art", "cluster.art", "package.art", "component.art")


# ---------------------------------------------------------------------------
# element family + name indexing
# ---------------------------------------------------------------------------

# The grammar families we resolve across imports. A reference's target
# metaclass (`obj_ref.cls`) is matched against these so a Node ref never
# binds to an Interface of the same name, etc. MessageDecl / EnumDecl share
# the `MessageOrEnum` abstract family.
_FAMILIES = {
    "MessageDecl": "message_or_enum",
    "EnumDecl": "message_or_enum",
    "MessageOrEnum": "message_or_enum",
    "SenderReceiverInterface": "interface",
    "ClientServerInterface": "interface",
    "InterfaceDecl": "interface",
    "NodeDecl": "node",
    "CompositionDecl": "composition",
    "ClusterDecl": "cluster",
}


def _family_of(class_name: str) -> Optional[str]:
    return _FAMILIES.get(class_name)


def _is_extern(el) -> bool:
    """True if *el* is an `extern` forward-decl (skip when indexing as a
    resolution target — the real def lives in an imported package)."""
    return bool(getattr(el, "extern", False))


# ---------------------------------------------------------------------------
# the scope provider
# ---------------------------------------------------------------------------


class ImportFollowingScopeProvider:
    """A textX scope provider that follows Artheia `import` lines.

    Registered as the catch-all ``{"*.*": provider}``. For every
    cross-reference it: (1) tries the local model, then (2) walks the
    importing model's imports into the imported packages and resolves
    there, with bare-unique / FQN / ambiguity semantics.

    A per-metamodel parse cache (keyed by resolved package directory)
    keeps each imported package parsed at most once per metamodel, and a
    re-entrancy guard avoids infinite loops on cyclic imports.
    """

    def __init__(self):
        # resolved-dir -> parsed model (raw textX parse, no postprocess)
        self._pkg_cache: dict[Path, object] = {}
        self._loading: set[Path] = set()
        # resolved-dir -> _CatalogIndex (lazy per-PDU stub resolution for
        # generated bus packages — the textX N² escape hatch).
        self._catalog_cache: dict[Path, "_CatalogIndex"] = {}
        # textX's stock GLOBAL-by-name resolver — the same behaviour a
        # metamodel with no registered provider gets by default. We delegate
        # to it FIRST so intra-model references (incl. cross-composition
        # PrototypeDecl PortRefs, same-model decls) resolve exactly as
        # before; only genuine misses on import-resolvable families fall
        # through to import-following. PlainName returns None for a dotted
        # FQN ref (no object is named with dots), which is exactly when our
        # import resolver should take over.
        self._default = PlainName()

    # -- public textX entry point -------------------------------------------

    def __call__(self, obj, attr, obj_ref):
        if obj_ref is None:
            return None

        # 1) textX default first: resolves intra-model refs (PrototypeDecl,
        #    same-package messages/ifaces/nodes, real local decls). A local
        #    `extern` stub of the same name does NOT satisfy a ref here —
        #    the default matches by name regardless of extern, so we guard
        #    against binding to a stub below.
        resolved = self._default(obj, attr, obj_ref)
        if resolved is not None and not _is_extern(resolved):
            return resolved

        # 2) Only import-resolvable families follow imports. Anything else
        #    (e.g. an unresolved PrototypeDecl) returns whatever the default
        #    gave (None) so textX raises its standard error.
        target_family = _family_of(obj_ref.cls.__name__)
        if target_family is None:
            return resolved

        model = get_model(obj)
        return self._resolve_via_imports(
            model, obj, target_family, obj_ref.obj_name, obj_ref)

    # -- import resolution --------------------------------------------------

    def _resolve_via_imports(self, model, obj, family: str, name: str, obj_ref):
        entry_path = getattr(model, "_tx_filename", None)
        if not entry_path:
            return None  # parsed from a string with no file anchor
        entry = Path(entry_path).absolute()
        entry_pkg = getattr(model, "name", "") or ""

        leaf = name.rsplit(".", 1)[-1]
        dotted = "." in name
        want_pkg = name.rsplit(".", 1)[0] if dotted else None

        matches: list[tuple[str, object]] = []  # (pkg_fqn, element)

        # OWN-package resolution first: a same-package ref (e.g. a bus node's
        # `provides <Pdu>_Iface` port, where the iface lives in THIS file's own
        # package — the catalog dir's package.art/catalog.json) has no `import`
        # to follow. So try the entry file's OWN directory: if it carries a
        # catalog.json, the iface/message resolves LAZILY from the catalog
        # (O(1)) — which is exactly how a bus mega-node in component.art binds
        # its per-PDU iface ports without dragging in the package.art monolith.
        if not dotted or want_pkg == entry_pkg:
            own = self._resolve_one_in_package(
                entry.parent, entry_pkg, family, leaf)
            if own is not None:
                matches.append((entry_pkg, own))

        for imp in getattr(model, "imports", []) or []:
            imp_pkg = imp.name[:-2] if imp.name.endswith(".*") else imp.name
            pkg_dir = _import_dir(entry, entry_pkg, imp_pkg)
            if pkg_dir is None or not pkg_dir.is_dir():
                continue
            if dotted and imp_pkg != want_pkg:
                # An FQN ref names exactly one package — skip imports that
                # aren't it (also avoids loading unrelated huge buses).
                continue
            el = self._resolve_one_in_package(pkg_dir, imp_pkg, family, leaf)
            if el is not None:
                matches.append((imp_pkg, el))

        if not matches:
            # Let textX raise the standard "unresolved" error.
            return None
        if len(matches) == 1:
            return matches[0][1]

        # >1 match. If the ref was an FQN we already filtered by package;
        # multiple still means a genuine duplicate (shouldn't happen).
        # If it was bare, demand an FQN, naming the candidate packages.
        if dotted:
            return matches[0][1]
        pkgs = sorted({pkg for pkg, _ in matches})
        from textx.scoping.tools import get_parser
        parser = get_parser(obj)
        line, col = parser.pos_to_linecol(obj_ref.position)
        raise TextXSemanticError(
            f"reference to '{leaf}' is ambiguous — defined in "
            f"{len(pkgs)} imported packages: {', '.join(pkgs)}. "
            f"Qualify it with the full package path, e.g. "
            f"'{pkgs[0]}.{leaf}'.",
            line=line, col=col,
            filename=getattr(model, "_tx_filename", None),
        )

    # -- per-package single-name resolution ---------------------------------

    def _resolve_one_in_package(self, pkg_dir: Path, pkg_fqn: str,
                                family: str, leaf: str):
        """Resolve a single *leaf* of *family* in the package at *pkg_dir*.

        FAST PATH — if the directory carries a ``catalog.json`` (a
        generated, read-only bus package), resolve via the catalog index:
        generate + parse a TINY stub containing only the one referenced
        PDU's ``message`` + ``interface { data }`` and return that object.
        This sidesteps textX's O(N²) cross-ref resolution over the
        thousand-PDU monolith — cost scales with refs-used, not bus size.

        SLOW PATH — a hand-written package (no catalog): parse its merged
        .art once (cached) and scan its elements. These are small.
        """
        catalog = pkg_dir / "catalog.json"
        if catalog.exists():
            hit = self._catalog_index(pkg_dir, pkg_fqn).lookup(family, leaf)
            if hit is not None:
                return hit
            # The catalog indexes ONLY message/interface (the per-PDU types) —
            # it returns None for everything else (family "node" etc.). A bus
            # package's component.art also carries the small `<Bus>_Bus` NODE
            # (node-only by design — no N² eager ifaces/ports). Resolve it by
            # parsing ONLY component.art (never the package.art message monolith,
            # which would be the N² we're avoiding) — the file is tiny, so this
            # stays O(1). This is what lets a sender's `extern node atomic
            # <Bus>_Bus { }` resolve to the real PSP node while the thousand
            # PDUs stay on the catalog fast path.
            if family in ("message_or_enum", "interface"):
                return None  # catalog is authoritative for these — a miss is a miss
            return self._resolve_in_component_only(pkg_dir, family, leaf)
        model = self._load_package(pkg_dir)
        if model is None:
            return None
        for el in getattr(model, "elements", []) or []:
            if _family_of(el.__class__.__name__) != family:
                continue
            if _is_extern(el):
                continue
            if el.name == leaf:
                return el
        return None

    def _resolve_in_component_only(self, pkg_dir: Path, family: str, leaf: str):
        """For a catalog (bus) package: resolve a non-catalog family (the
        `<Bus>_Bus` node) WITHOUT paying the bus's O(N²) parse cost. The bus
        component.art carries the node + N extern iface fwd-decls + N node ports
        — parsing it whole is seconds for a big bus. So when a CONSUMER (e.g. the
        gateway) resolves the bus node, we extract ONLY that node's declaration
        (a node-only PROJECTION — ports stripped) and parse that tiny stub. The
        full node+ports view is the bus's own parse / `--force`, not a consumer's
        default. Cached per (dir, leaf)."""
        if family != "node":
            return None
        comp = pkg_dir / "component.art"
        if not comp.exists():
            return None
        key = (pkg_dir.resolve(), "node_proj", leaf)
        cached = self._pkg_cache.get(key)
        if cached is not None or key in self._pkg_cache:
            return cached
        try:
            from .loader import parse_bus_node_projection
            m = parse_bus_node_projection(comp, leaf)
        except Exception:
            m = None
        el = None
        if m is not None:
            for e in getattr(m, "elements", []) or []:
                if (_family_of(e.__class__.__name__) == "node"
                        and not _is_extern(e) and e.name == leaf):
                    el = e
                    break
        self._pkg_cache[key] = el
        return el

    def _catalog_index(self, pkg_dir: Path, pkg_fqn: str) -> "_CatalogIndex":
        key = pkg_dir.resolve()
        idx = self._catalog_cache.get(key)
        if idx is None:
            idx = _CatalogIndex(pkg_dir / "catalog.json", pkg_fqn)
            self._catalog_cache[key] = idx
        return idx

    # -- imported-package parsing (cached, re-entrancy-guarded) -------------

    def _load_package(self, pkg_dir: Path):
        """Parse the package rooted at *pkg_dir* (merging package.art +
        component.art) and return the raw textX model, cached. Returns
        None if the directory has no .art entry file or is mid-load
        (cyclic import). Used only for catalog-less (hand-written)
        packages — generated bus packages take the catalog fast path."""
        key = pkg_dir.resolve()
        if key in self._pkg_cache:
            return self._pkg_cache[key]
        if key in self._loading:
            return None  # cycle — break it; the in-progress load will index
        candidate = None
        for fname in _PKG_FILE_PRIORITY:
            if (pkg_dir / fname).exists():
                candidate = pkg_dir / fname
                break
        if candidate is None:
            return None

        self._loading.add(key)
        try:
            from .loader import load_metamodel, _sibling_split, _merged_source
            mm = load_metamodel()
            primary, sibling = _sibling_split(candidate)
            if sibling is None:
                m = mm.model_from_file(str(primary))
            else:
                merged, anchor = _merged_source(primary, sibling)
                m = mm.model_from_str(merged, file_name=str(anchor))
            self._pkg_cache[key] = m
            return m
        except Exception:
            self._pkg_cache[key] = None
            return None
        finally:
            self._loading.discard(key)


# ---------------------------------------------------------------------------
# catalog-backed lazy index (the textX N² escape hatch)
# ---------------------------------------------------------------------------


class _CatalogIndex:
    """Lazy resolver for a generated bus package, driven by its
    ``catalog.json`` instead of parsing the (thousand-PDU) ``.art``.

    The catalog is the authoritative read-only index: message names are
    the keys; each carries its scalar fields (``proto_type`` + ``name``).
    For a requested PDU we synthesize a TINY ``.art`` source —

        package <bus>
        message <Pdu> { <type> <field> ... }
        interface senderReceiver <Pdu>_Iface { data <Pdu> record }

    — parse just that on the shared metamodel (so the returned objects are
    real textX instances, which ``textx_isinstance`` accepts), and cache
    the resulting ``MessageDecl`` / ``SenderReceiverInterface`` per PDU.
    Resolution cost scales with the number of PDUs an app actually
    references, never the bus size.
    """

    def __init__(self, catalog_path: Path, pkg_fqn: str):
        import json
        self._pkg = pkg_fqn
        self._catalog_path = catalog_path
        try:
            data = json.loads(catalog_path.read_text())
            self._messages: dict = data.get("messages", {}) or {}
        except Exception:
            self._messages = {}
        # leaf-name -> resolved object (MessageDecl / SenderReceiverInterface)
        self._msg_cache: dict[str, object] = {}
        self._iface_cache: dict[str, object] = {}

    def lookup(self, family: str, leaf: str):
        if family == "interface":
            return self._iface(leaf)
        if family == "message_or_enum":
            return self._message(leaf)
        # nodes/compositions/clusters aren't catalog-resolvable — the bus
        # mega-node is forward-declared (extern) on the consuming side.
        return None

    # -- internal -----------------------------------------------------------

    def _pdu_for_iface(self, iface_leaf: str) -> str | None:
        """`EML_01_Iface` -> `EML_01` if that PDU is in the catalog."""
        if iface_leaf.endswith("_Iface"):
            pdu = iface_leaf[: -len("_Iface")]
            if pdu in self._messages:
                return pdu
        return None

    def _parse_stub(self, pdu: str):
        """Parse a one-PDU stub; return its (message, iface) objects."""
        from .loader import load_metamodel
        fields = self._messages.get(pdu, {}).get("fields", []) or []
        lines = [f"package {self._pkg}", f"message {pdu} {{"]
        for f in fields:
            ptype = f.get("proto_type")
            fname = f.get("name")
            if ptype and fname:
                lines.append(f"    {ptype} {fname}")
        lines.append("}")
        lines.append(
            f"interface senderReceiver {pdu}_Iface {{ data {pdu} record }}"
        )
        src = "\n".join(lines)
        mm = load_metamodel()
        # file_name anchors get_model().name to the bus package so the
        # defining-package codec naming in fc_app keys off the bus.
        m = mm.model_from_str(src, file_name=str(self._catalog_path))
        msg = iface = None
        for el in m.elements:
            kind = el.__class__.__name__
            if kind == "MessageDecl" and el.name == pdu:
                msg = el
            elif kind == "SenderReceiverInterface" and el.name == f"{pdu}_Iface":
                iface = el
        return msg, iface

    def _message(self, leaf: str):
        if leaf in self._msg_cache:
            return self._msg_cache[leaf]
        if leaf not in self._messages:
            self._msg_cache[leaf] = None
            return None
        msg, _ = self._parse_stub(leaf)
        self._msg_cache[leaf] = msg
        return msg

    def _iface(self, leaf: str):
        if leaf in self._iface_cache:
            return self._iface_cache[leaf]
        pdu = self._pdu_for_iface(leaf)
        if pdu is None:
            self._iface_cache[leaf] = None
            return None
        msg, iface = self._parse_stub(pdu)
        # Cache both so a later `data <Pdu>` ref reuses the same object.
        self._msg_cache[pdu] = msg
        self._iface_cache[leaf] = iface
        return iface


def register_scope_provider(mm) -> None:
    """Register the import-following scope provider on metamodel *mm* as the
    catch-all for every cross-reference attribute."""
    mm.register_scope_providers({"*.*": ImportFollowingScopeProvider()})
