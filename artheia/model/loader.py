"""textX metamodel loading and entry points for parsing Artheia files.

A typical .art directory holds two files:

- ``package.art``    — schema layer: messages, enums, interfaces, nodes.
- ``component.art``  — wiring/deploy layer: compositions, clusters.

The split is convention, not enforced by the grammar. When parsing
``package.art`` :func:`parse_file` automatically concatenates a sibling
``component.art`` (if present) so cross-refs (a composition pointing
at a node, a cluster pointing at a composition) resolve in one go.

Pointing :func:`parse_file` at ``component.art`` directly does the same
thing in reverse — package.art is pulled in first. This keeps every
caller working whether it knows about the split or not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from textx import metamodel_from_file

from ..grammar import GRAMMAR_PATH
from .validators import register_validators
from .scope import register_scope_provider


_METAMODEL = None


def load_metamodel():
    global _METAMODEL
    if _METAMODEL is None:
        mm = metamodel_from_file(str(GRAMMAR_PATH))
        # Scope provider FIRST: it follows `import pkg.*` lines so a
        # cross-ref (iface/message/node/composition/cluster) resolves
        # against imported packages, not just the current model.
        register_scope_provider(mm)
        register_validators(mm)
        _METAMODEL = mm
    return _METAMODEL


def _sibling_split(path: Path) -> tuple[Path, Optional[Path]]:
    """Return (primary, sibling). Primary is the file at *path*;
    sibling is the other half of the package/component pair if it
    exists in the same directory."""
    name = path.name
    if name == "package.art":
        sibling = path.with_name("component.art")
    elif name == "component.art":
        sibling = path.with_name("package.art")
    else:
        return path, None
    return path, (sibling if sibling.exists() else None)


def _merged_source(primary: Path, sibling: Path) -> tuple[str, Path]:
    """Concatenate ``package.art`` + ``component.art`` content into one
    virtual source.

    package.art comes first (schema before wiring) so cross-refs in
    component.art can resolve forward. The single ``package`` line
    (which has to appear at most once per model) is taken from
    whichever file declares it; if both do, they must match.

    Returns (merged_source_str, anchor_path). The anchor path is the
    primary file — textX uses it for error location and dependency
    tracking.
    """
    if primary.name == "package.art":
        pkg_text = primary.read_text()
        cmp_text = sibling.read_text()
    else:
        pkg_text = sibling.read_text()
        cmp_text = primary.read_text()

    pkg_decl = _extract_package_line(pkg_text)
    cmp_decl = _extract_package_line(cmp_text)
    if pkg_decl and cmp_decl and pkg_decl != cmp_decl:
        raise ValueError(
            f"package mismatch between {primary} and {sibling}: "
            f"{pkg_decl!r} vs {cmp_decl!r}"
        )
    # The grammar is `package? imports* elements*` — imports must come
    # before ANY element. Naively concatenating package.art + component.art
    # would bury component.art's import lines after package.art's elements,
    # which is a syntax error. So hoist every import from BOTH files to the
    # top (deduplicated, order-preserving) and strip them from the bodies.
    # This lets component.art carry its own `import` lines (e.g. a placeholder
    # FC importing system.supervisor for a forward-decl'd node).
    imports = _collect_imports(pkg_text) + _collect_imports(cmp_text)
    seen: set[str] = set()
    imports = [i for i in imports if not (i in seen or seen.add(i))]

    # Strip the package line + import lines from both halves — Model allows
    # at most one package line, and imports are re-emitted at the top.
    pkg_body = _strip_import_lines(pkg_text)
    cmp_body = _strip_import_lines(_strip_package_line(cmp_text))

    head = (pkg_decl + "\n\n") if pkg_decl else ""
    import_block = ("\n".join(imports) + "\n\n") if imports else ""
    # pkg_body still leads with its own package line; drop it (head re-adds).
    pkg_body = _strip_package_line(pkg_body)
    merged = (
        head + import_block
        + pkg_body.strip()
        + "\n\n// ---- component.art ----\n\n"
        + cmp_body
    )
    return merged, primary


def _extract_package_line(src: str) -> Optional[str]:
    """Return the ``package x.y.z`` line if any, else None."""
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("package "):
            return s
        if s and not s.startswith("//") and not s.startswith("/*"):
            # First non-comment, non-package line — no package decl.
            return None
    return None


def _strip_package_line(src: str) -> str:
    """Remove the first ``package ...`` line (if any) from *src*."""
    out: list[str] = []
    stripped = False
    for line in src.splitlines():
        if not stripped and line.strip().startswith("package "):
            stripped = True
            continue
        out.append(line)
    return "\n".join(out)


def _collect_imports(src: str) -> list[str]:
    """Return the ``import ...`` lines from *src*, stripped of any trailing
    ``//`` comment, in source order."""
    out: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import "):
            # Drop trailing line comments so dedup keys on the FQN alone.
            code = s.split("//", 1)[0].rstrip()
            out.append(code)
    return out


def _strip_import_lines(src: str) -> str:
    """Remove every ``import ...`` line from *src* (they're re-emitted at the
    top of the merged source)."""
    return "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("import ")
    )


def _postprocess(model):
    """Hook for transforms that have to run AFTER textX cross-refs
    resolve. Currently:

      - :mod:`artheia.model.inherit` flattens ``node X prototype Base``
        so generators see standalone-looking NodeDecls — the derived
        node absorbs the base's ports/params/statem/config/flags/tipc
        unless it overrode them.

    In-place mutation; returns the same model object."""
    from .inherit import resolve_inheritance
    resolve_inheritance(model)
    return model


def parse_file(path: str | Path):
    """Parse a .art file, optionally merging package.art + component.art."""
    p = Path(path)
    primary, sibling = _sibling_split(p)
    if sibling is None:
        return _postprocess(load_metamodel().model_from_file(str(primary)))
    merged, anchor = _merged_source(primary, sibling)
    return _postprocess(
        load_metamodel().model_from_str(merged, file_name=str(anchor)))


def parse_file_standalone(path: str | Path):
    """Parse a single .art file WITHOUT merging its package.art/component.art
    sibling. Used for the catalog (bus) packages: their component.art is the
    small bus-node file, and merging in the 512/1025-PDU package.art monolith
    is the O(N²) we avoid — the per-PDU types resolve lazily from catalog.json.
    Shared by the CLI parse path + the LSP."""
    return _postprocess(load_metamodel().model_from_file(str(Path(path))))


def parse_string(src: str, file_name: Optional[str] = None):
    return _postprocess(
        load_metamodel().model_from_str(src, file_name=file_name))
