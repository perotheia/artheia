"""Catalog of Adaptive Platform Functional Clusters (FCs).

Each FC has a short name (used in namespaces and include structure),
its Log&Trace context ID, and a human-readable display name. The
runtime tracks one ``ServiceManifest`` per FC, organised under
``services/<short>/system/package.art``.

Reference: AUTOSAR Adaptive Platform spec — list of platform functional
clusters with their SHORTNAME, Log&Trace context ID, and full name.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionalCluster:
    """One entry in the Adaptive Platform's FC catalogue."""

    short: str          # e.g. "crypto"
    context_id: str     # e.g. "#CRY"
    display: str        # e.g. "Cryptography"


CLUSTERS: tuple[FunctionalCluster, ...] = (
    FunctionalCluster("core",   "#COR", "Adaptive Platform Core"),
    FunctionalCluster("com",    "#COM", "Communication Management"),
    FunctionalCluster("crypto", "#CRY", "Cryptography"),
    FunctionalCluster("diag",   "#DIA", "Diagnostics"),
    FunctionalCluster("exec",   "#EXE", "Execution Management"),
    FunctionalCluster("fw",     "#FWX", "Firewall"),
    FunctionalCluster("idsm",   "#IDS", "Intrusion Detection System Manager"),
    FunctionalCluster("log",    "#LOG", "Log and Trace"),
    FunctionalCluster("nm",     "#NMX", "Network Management"),
    FunctionalCluster("osi",    "#OSI", "Operating System Interface"),
    FunctionalCluster("per",    "#PER", "Persistency"),
    FunctionalCluster("phm",    "#PHM", "Platform Health Management"),
    FunctionalCluster("rds",    "#RDS", "Raw Data Stream"),
    FunctionalCluster("sm",     "#SMX", "State Management"),
    FunctionalCluster("tsync",  "#TSY", "Time Synchronization"),
    FunctionalCluster("ucm",    "#UCM", "Update and Configuration Management"),
    FunctionalCluster("vucm",   "#VUM", "Vehicle Update and Configuration Management"),
    FunctionalCluster("shwa",   "#SHA", "Safe Hardware Accelerator"),
)


BY_SHORT: dict[str, FunctionalCluster] = {fc.short: fc for fc in CLUSTERS}
