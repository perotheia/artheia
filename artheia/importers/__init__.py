"""AUTOSAR-side importers (DBC + FIBEX).

ARXML used to live here. It was removed because the gateway already
generates its netgraph from DBC + FIBEX — the same sources we use here.
The parser at `_asam_cmp_parser` is vendored verbatim from theia so the
two stacks agree byte-for-byte on what a frame is.
"""
from .autosar import import_dbc, import_fibex

__all__ = ["import_dbc", "import_fibex"]
