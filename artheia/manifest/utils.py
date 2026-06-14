"""Shared builders for the generated manifest-proto modules.

``artheia gen-manifest-proto`` emits one module per system ``.art`` whose
per-cluster sections are built by calling these three helpers. They were
inlined in every generated file; factoring them here keeps the generated
output small and lets the logic evolve without regenerating (the
generated modules just ``from artheia.manifest.utils import ...``).

Each helper maps a cluster-member short name to one AUTOSAR Adaptive
manifest element:

- :func:`component_for`  → :class:`SwComponent`  (buildable handle)
- :func:`executable_for` → :class:`Executable`   (Application Manifest §3.18)
- :func:`process_for`    → :class:`Process`       (Execution Manifest §8.2)
"""

from __future__ import annotations

import importlib
import os

from artheia.manifest.application import (
    BuildTypeEnum,
    Executable,
    ExecutionStateReportingBehaviorEnum,
    RootSwComponentPrototype,
    SwComponent,
)
from artheia.manifest.execution import (
    Process,
    SchedulingPolicy,
    StartupConfig,
    StateDependentStartupConfig,
    TerminationBehaviorEnum,
)


def _daemon_class(short: str) -> str:
    """``com`` → ``ComDaemon``; ``vehicle_update`` → ``VehicleUpdateDaemon``."""
    return "".join(p.capitalize() for p in short.split("_")) + "Daemon"


def _prebuilt_owners() -> "frozenset[str]":
    """Cluster owners (base_dir) whose binaries are INSTALLED, not built — listed
    in $THEIA_PREBUILT_OWNERS (comma/space-separated). A downstream workspace that
    installed the theia-services .deb sets THEIA_PREBUILT_OWNERS=services, so the
    Services components are marked non-bazel-buildable and run in place from
    $THEIA_ROOT/bin instead of being compiled from a (absent) source tree."""
    raw = os.environ.get("THEIA_PREBUILT_OWNERS", "")
    return frozenset(t for t in raw.replace(",", " ").split() if t)


def _prebuilt_bin(ident: str) -> str:
    """Absolute on-target path of a prebuilt component's binary —
    $THEIA_ROOT/bin/<ident> (default /opt/theia/bin/<ident>, where the deb
    installs it). Used as the run-in-place start_cmd."""
    root = os.environ.get("THEIA_ROOT", "/opt/theia").rstrip("/")
    return f"{root}/bin/{ident}"


# ---------------------------------------------------------------------------
# Directory-structure convention for application clusters.
#
# Nothing in this block guesses: it encodes one filesystem discipline, keyed
# on (base_dir, ident), so every build/deploy path is derivable instead of
# hand-listed. base_dir is the manifest module's directory (e.g. ``apps``);
# ident is the cluster-member handle (e.g. ``p1``):
#
#   app source dir   <base_dir>/<ident>          (gen-app --base-dir/<ident>
#                                                  regenerates the lib part)
#   bazel target     //<base_dir>/<ident>:<ident>
#   start_cmd        ["bin/<ident>"]             (on-target install path)
#   art_node         system.<base_dir>.<ident>/<Composition>
#
# The cluster name itself is NOT part of the path — it is only the manifest
# grouping (which section the member lands in), irrelevant to gen/regen.
# ---------------------------------------------------------------------------


def app_dir(base_dir: str, ident: str) -> str:
    """Source dir for an application member: ``<base_dir>/<ident>``."""
    return f"{base_dir}/{ident}"


def app_bazel_target(base_dir: str, ident: str, composition: str) -> str:
    """Bazel label for a gen-app member's binary. Two layout conventions, by
    base_dir:

      - services FCs:  ``//services/<ident>/main:<ident>``  (per-FC dir + binary
        named after the FC short — e.g. log → //services/log/main:log).
      - app members:   ``//<base_dir>/<composition>/main:demo``  (per-composition
        app dir whose cc_binary is conventionally named ``apps`` — e.g.
        apps/Demo3WayP1 → //apps/Demo3WayP1/main:demo).
    """
    if base_dir == "services":
        return f"//services/{ident}/main:{ident}"
    return f"//{base_dir}/{composition}/main:demo"


def app_start_cmd(ident: str) -> list[str]:
    """On-target launch command: ``["bin/<ident>"]``."""
    return [f"bin/{ident}"]


def app_component_for(
    base_dir: str, ident: str, composition: str, cluster: "str | None" = None
) -> SwComponent:
    """SwComponent for an application member, paths derived from the
    (base_dir, ident) directory convention — no guessing, no hand-listing.

    ``composition`` is the .art composition the member instantiates
    (``member.type.name``); it's carried into ``art_node`` so the handle
    points at the real composition rather than a synthesized class name.

    ``cluster`` is the member's .art PACKAGE cluster (the segment after
    ``system.`` in its ``package`` decl, e.g. ``demo`` for ``system.demo``).
    The ``art_node`` MUST name the real package — that's how the supervisor
    re-resolves each prototype to its node's TIPC address
    (``_collect_nodes_for_app`` parses ``system/<cluster>/component.art``).
    It defaults to ``base_dir`` for back-compat, but ``base_dir`` is the
    SOURCE DIRECTORY (the bazel-target prefix, e.g. ``apps``) which is NOT
    always the package cluster (the demo lives in ``apps/`` but its package
    is ``system.demo``). Pass it explicitly whenever they differ.
    """
    art_cluster = cluster or base_dir
    return SwComponent(
        name=ident,
        bazel_target=app_bazel_target(base_dir, ident, composition),
        owner=base_dir,
        art_node=f"system.{art_cluster}/{composition}",
        # A prebuilt owner (installed via deb, no source tree) is NOT bazel-built;
        # it runs in place from $THEIA_ROOT/bin (see app_process_for).
        bazel_buildable=base_dir not in _prebuilt_owners(),
    )


def app_process_for(
    base_dir: str, ident: str, nodes: "list[str] | None" = None
) -> Process:
    """Execution Manifest Process for an application member.

    start_cmd is the directory-convention ``bin/<ident>`` (overridable by a
    per-app ``manifest.<base_dir>.<ident>.executor`` PROCESS, same as FCs).
    ``nodes`` is the member composition's hosted prototype names — carried
    so the supervisor can list them statically in executor.json.
    """
    try:
        mod = importlib.import_module(
            f"manifest.{base_dir}.{ident}.executor"
        )
    except ImportError:
        mod = None
    if mod is not None and hasattr(mod, "PROCESS"):
        return mod.PROCESS

    # Prebuilt owner → run in place from the installed deb ($THEIA_ROOT/bin/<ident>,
    # absolute); else the on-target install convention bin/<ident>.
    start_cmd = ([_prebuilt_bin(ident)] if base_dir in _prebuilt_owners()
                 else app_start_cmd(ident))
    return Process(
        name=ident,
        executable=ident,
        function_cluster_affiliation="",
        start_cmd=start_cmd,
        nodes=list(nodes or []),
        state_dependent_startup_config=[
            StateDependentStartupConfig(
                function_group_state=["Default.Running"],
                startup_config=StartupConfig(
                    name=f"{ident}_startup",
                    scheduling_policy=SchedulingPolicy.SCHED_OTHER,
                    scheduling_priority=0,
                    termination_behavior=(
                        TerminationBehaviorEnum.PROCESS_IS_NOT_SELF_TERMINATING
                    ),
                ),
            ),
        ],
    )


def component_for(short: str) -> SwComponent:
    """One bazel-buildable handle per cluster member. The FC binary is the
    gen-app `//services/<short>/main:<short>` cc_binary."""
    return SwComponent(
        name=short,
        bazel_target=f"//services/{short}/main:{short}",
        owner="platform",
        art_node=f"services.{short}/{_daemon_class(short)}",
        bazel_buildable=True,
    )


def executable_for(short: str) -> Executable:
    """Adaptive Application Manifest Executable entry (§3.18)."""
    return Executable(
        name=short,
        category="PLATFORM_LEVEL",
        build_type=BuildTypeEnum.BUILD_TYPE_RELEASE,
        reporting_behavior=(
            ExecutionStateReportingBehaviorEnum.REPORTING_BEHAVIOR_INDIVIDUAL
        ),
        root_sw_component_prototype=RootSwComponentPrototype(
            name=f"{short}_root",
            application_type=_daemon_class(short),
        ),
    )


def process_for(short: str) -> Process:
    """Execution Manifest Process (§8.2).

    Tries to import ``PROCESS`` from ``manifest.services.<short>.executor``
    (hand-edited, survives ``artheia gen-app``). Falls back to an empty
    start_cmd for members without an executor.py — the supervisor
    refuses to launch those.
    """
    try:
        mod = importlib.import_module(f"manifest.services.{short}.executor")
    except ImportError:
        mod = None

    if mod is not None and hasattr(mod, "PROCESS"):
        return mod.PROCESS

    return Process(
        name=short,
        executable=short,
        function_cluster_affiliation=short,
        start_cmd=[],
        state_dependent_startup_config=[
            StateDependentStartupConfig(
                function_group_state=["Default.Running"],
                startup_config=StartupConfig(
                    name=f"{short}_startup",
                    scheduling_policy=SchedulingPolicy.SCHED_OTHER,
                    scheduling_priority=0,
                    termination_behavior=(
                        TerminationBehaviorEnum.PROCESS_IS_NOT_SELF_TERMINATING
                    ),
                ),
            ),
        ],
    )
