"""Generate the PSP-level CAN bus registry that aggregates all CAN namespaces
into a single psp_can_lookup() entry point for libpsp.so.

Ported verbatim from gateway/pero_cmp_lnx/tools/gen_psp_registry.py; output
is byte-identical.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def generate(
    can_namespaces: List[str],
    include_dir: str,
    out_dir: str,
) -> List[str]:
    """Emit psp_can_registry.{c,h} into out_dir. Returns the written paths."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        keep_trailing_newline=True,
    )

    data = dict(
        can_namespaces=list(can_namespaces),
        pero_cmp_include=os.path.abspath(include_dir),
    )

    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    for tmpl_name, fname in [
        ("psp_can_registry.c.j2", "psp_can_registry.c"),
        ("psp_can_registry.h.j2", "psp_can_registry.h"),
    ]:
        text = env.get_template(tmpl_name).render(**data)
        path = os.path.join(out_dir, fname)
        with open(path, "w") as f:
            f.write(text)
        print(f"  wrote: {path}")
        written.append(path)
    return written
