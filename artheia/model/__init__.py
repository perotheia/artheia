"""Metamodel loading + semantic validation for Artheia."""
from .flatten import flatten_composition
from .loader import (
    load_metamodel,
    parse_file,
    parse_file_standalone,
    parse_string,
)

__all__ = [
    "flatten_composition",
    "load_metamodel",
    "parse_file",
    "parse_file_standalone",
    "parse_string",
]
