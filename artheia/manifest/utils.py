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


# ---------------------------------------------------------------------------
# Directory-structure convention for application clusters.
#
# Nothing in this block guesses: it encodes one filesystem discipline, keyed
# on (base_dir, ident), so every build/deploy path is derivable instead of
# hand-listed. base_dir is the manifest module's directory (e.g. ``demo``);
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


def app_bazel_target(base_dir: str, ident: str) -> str:
    """Bazel label for an application member: ``//<base_dir>/<ident>:<ident>``."""
    return f"//{base_dir}/{ident}:{ident}"


def app_start_cmd(ident: str) -> list[str]:
    """On-target launch command: ``["bin/<ident>"]``."""
    return [f"bin/{ident}"]


def app_component_for(
    base_dir: str, ident: str, composition: str
) -> SwComponent:
    """SwComponent for an application member, paths derived from the
    (base_dir, ident) directory convention — no guessing, no hand-listing.

    ``composition`` is the .art composition the member instantiates
    (``member.type.name``); it's carried into ``art_node`` so the handle
    points at the real composition rather than a synthesized class name.
    """
    return SwComponent(
        name=ident,
        bazel_target=app_bazel_target(base_dir, ident),
        owner=base_dir,
        art_node=f"system.{base_dir}.{ident}/{composition}",
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

    return Process(
        name=ident,
        executable=ident,
        function_cluster_affiliation="",
        start_cmd=app_start_cmd(ident),
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
    """One bazel-buildable handle per cluster member."""
    return SwComponent(
        name=short,
        bazel_target=f"//services/{short}",
        owner="platform",
        art_node=f"services.{short}/{_daemon_class(short)}",
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
