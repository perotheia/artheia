"""Artheia Language Server (LSP) — pygls 2.x.

Features:
  - diagnostics from artheia.model (syntax + semantic errors)
  - goto-definition for cross-refs
  - completion: keywords + workspace symbols + gateway-catalog messages
"""
from .server import create_server, main

__all__ = ["create_server", "main"]
