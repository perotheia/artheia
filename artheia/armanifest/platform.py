"""The Adaptive Platform base layer — derived from .art sources.

This is the L0 layer: a :class:`Rig` carrying the canonical set of
Functional Clusters as :class:`SwComponent` entries, plus a matching
:class:`ServiceManifest` and list of :class:`ExecutionManifest` synthesised
from ``platforms/system/services/<short>/package.art`` via the
:mod:`armanifest.loader`.

Vendor-side layers (Macan, Tornado, …) build on top of this rig via
:func:`merge_layers`; they add or remove FCs, attach machines, override
per-service bindings, and tune per-FC :class:`ExecutionManifest`
attributes (priority, affinity, memory caps).

The platform's machine list is empty — only the upper layers know
which physical machine the FCs land on. Likewise ``host_machine`` on
the platform application is empty until a rig layer fills it in.
"""

from __future__ import annotations

import os
from pathlib import Path

from artheia.armanifest.application import ApplicationManifest, SwComponent
from artheia.armanifest.clusters import CLUSTERS
from artheia.armanifest.loader import load_platform_services
from artheia.armanifest.rig import Rig, VehicleIdentity


def _default_art_root() -> Path:
    """Resolve the .art source root for the platform FCs.

    Search order:

    1. ``$ARTHEIA_PLATFORM_SERVICES`` (absolute path).
    2. ``<repo_root>/platforms/system/services`` discovered by walking
       up from this file (works in editable installs).
    3. Fall back to ``platforms/system/services`` relative to cwd.

    A vendor that ships its own platform layout points
    ``ARTHEIA_PLATFORM_SERVICES`` at it.
    """
    env = os.environ.get("ARTHEIA_PLATFORM_SERVICES")
    if env:
        return Path(env)

    here = Path(__file__).resolve()
    # artheia/artheia/armanifest/platform.py → up 4 levels to the repo root.
    for parent in [here, *here.parents]:
        candidate = parent / "platforms" / "system" / "services"
        if candidate.is_dir():
            return candidate

    return Path("platforms/system/services")


PLATFORM_SERVICES_ROOT = _default_art_root()


# ---- SwComponent list: one bazel target per FC -------------------------------
# Layered manifests reference these by name; an upper layer can Override
# the bazel_target to point at a vendor-specific build (e.g. a Macan
# replacement implementation of `crypto`).

def _component_for(short: str) -> SwComponent:
    return SwComponent(
        name=short,
        bazel_target=f"//services/{short}",
        owner="platform",
        art_node=f"services.{short}/{''.join(p.capitalize() for p in short.split('_'))}Daemon",
    )


PlatformApplication = ApplicationManifest(
    name="platform_app",
    host_machine="",  # filled by upper layers
    components=[_component_for(fc.short) for fc in CLUSTERS],
)


# ---- ServiceManifest + ExecutionManifests sourced from .art ------------------

PlatformServices, PlatformExecution = load_platform_services(PLATFORM_SERVICES_ROOT)


# ---- Final rig: empty machines, one application, full service + execution set

PlatformBase = Rig(
    vehicle=VehicleIdentity(name="platform", make="", model=""),
    machines=[],
    applications=[PlatformApplication],
    service_manifests=[PlatformServices],
    execution_manifests=PlatformExecution,
)
