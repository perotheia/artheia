"""textX metamodel loading and entry points for parsing Artheia files."""
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


def parse_file(path: str | Path):
    return load_metamodel().model_from_file(str(path))


def parse_string(src: str, file_name: Optional[str] = None):
    return load_metamodel().model_from_str(src, file_name=file_name)
