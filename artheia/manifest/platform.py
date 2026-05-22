"""The Adaptive Platform base layer.

This is the L0 layer: a :class:`Rig` carrying the canonical set of
Functional Clusters. The authoritative shape lives in
:data:`services.manifest.fc.FcLayer` (an explicit, hand-authored
``Layer`` listing the 18 FCs); :data:`PlatformBase` lifts that layer
into a base :class:`Rig` and pairs it with the
:class:`ServiceManifest` derived from each FC's ``package.art``
(TIPC bindings, service-interface ports).

Upper layers (e.g. ``demo/manifest/rig.py``) build on this base via
:func:`merge_layers`; they add machines, attach the platform
application to a host, and may :class:`Override` per-FC startup
configuration.

The platform's machine list is empty — only the upper layers know
which physical machine the FCs land on. Likewise ``host_machine`` on
the platform application is empty until a rig layer fills it in.
"""

from __future__ import annotations

import os
from pathlib import Path

from artheia.manifest.application import ApplicationManifest
from artheia.manifest.loader import load_platform_services
from artheia.manifest.rig import Rig, VehicleIdentity


def _default_art_root() -> Path:
    """Resolve the .art source root for the platform FCs.

    Search order:

    1. ``$ARTHEIA_PLATFORM_SERVICES`` (absolute path).
    2. ``<repo_root>/platform/system`` discovered by walking up from
       this file (works in editable installs).
    3. Fall back to ``platform/system`` relative to cwd.

    The 18 FC directories live directly under this root as symlinks
    (e.g. ``platform/system/core -> ../../services/system/core``).
    A vendor that ships its own platform layout points
    ``ARTHEIA_PLATFORM_SERVICES`` at it.
    """
    env = os.environ.get("ARTHEIA_PLATFORM_SERVICES")
    if env:
        return Path(env)

    here = Path(__file__).resolve()
    # artheia/artheia/manifest/platform.py → up 4 levels to the repo root.
    for parent in [here, *here.parents]:
        candidate = parent / "platform" / "system"
        if candidate.is_dir():
            return candidate

    return Path("platform/system")


PLATFORM_SERVICES_ROOT = _default_art_root()


# ---- ServiceManifest derived from .art (kept — TIPC bindings live here) -----

PlatformServices, _ = load_platform_services(PLATFORM_SERVICES_ROOT)


# ---- FC components + executions sourced from services.manifest.fc -----------
# The Python hack: services.manifest.fc imports artheia.manifest submodules
# directly (not through the package __init__), and this module isn't itself
# re-exported by artheia/manifest/__init__.py. That breaks the cycle —
# nothing under artheia.manifest.* is partial when fc.py runs.

from services.manifest.fc import COMPONENTS as _FC_COMPONENTS    # noqa: E402
from services.manifest.fc import PROCESSES as _FC_PROCESSES      # noqa: E402
from services.manifest.fc import SUPERVISORS as _FC_SUPERVISORS  # noqa: E402


PlatformApplication = ApplicationManifest(
    name="platform_app",
    host_machine="",  # filled by upper layers
    components=list(_FC_COMPONENTS),
)


# ---- Final rig: empty machines, one application, full service + execution set

PlatformBase = Rig(
    vehicle=VehicleIdentity(name="platform", make="", model=""),
    machines=[],
    applications=[PlatformApplication],
    service_manifests=[PlatformServices],
    execution_manifests=list(_FC_PROCESSES),
    supervisors=list(_FC_SUPERVISORS),
)
