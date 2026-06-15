# Single source of truth for the version is pyproject.toml; derive __version__
# from the installed package metadata so the two never drift.
try:
    from importlib.metadata import version as _v
    __version__ = _v("artheia")
except Exception:  # pragma: no cover — source tree without dist metadata
    __version__ = "0.0.0+unknown"
