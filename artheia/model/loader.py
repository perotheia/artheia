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


_METAMODEL = None


def load_metamodel():
    global _METAMODEL
    if _METAMODEL is None:
        mm = metamodel_from_file(str(GRAMMAR_PATH))
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
    # Strip the package line from component.art content — Model
    # allows at most one package line per source.
    cmp_text = _strip_package_line(cmp_text)
    merged = pkg_text.rstrip() + "\n\n// ---- component.art ----\n\n" + cmp_text
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


def parse_file(path: str | Path):
    """Parse a .art file, optionally merging package.art + component.art."""
    p = Path(path)
    primary, sibling = _sibling_split(p)
    if sibling is None:
        return load_metamodel().model_from_file(str(primary))
    merged, anchor = _merged_source(primary, sibling)
    return load_metamodel().model_from_str(merged, file_name=str(anchor))


def parse_string(src: str, file_name: Optional[str] = None):
    return load_metamodel().model_from_str(src, file_name=file_name)
