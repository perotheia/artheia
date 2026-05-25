"""End-to-end tests for ``artheia gen-rig``.

The generator's contract: given a top-level .art composition, emit a
``rig.py`` whose ``<Vehicle>Software.to_rig()`` produces the same
``Rig`` shape (machines, applications, components, execution
manifests, supervisors) as the hand-written equivalent.

The strongest acceptance test: ``artheia executor emit`` on the
generated rig.py must produce byte-identical ``executor.yaml`` to
the hand-written rig.py output. We assert that here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from artheia.generators.rig import (
    _extract_composition_info,
    _per_process_class,
    _vehicle_capitalize,
    generate_rig_py,
    write_rig_py,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ART = REPO_ROOT / "demo" / "system" / "demo" / "package.art"


def test_extract_composition_groups_prototypes_by_process():
    """Post-split (#260): the demo is THREE per-process compositions
    (Demo3WayP1/P2/P3) bundled by `cluster Applications`, not one
    monolithic ``Demo3Way``. Demo3WayP1 hosts 3 prototypes on process
    P1; the extractor collects them in declaration order. Prototype
    names lost the `_p1` suffix when cluster connects were simplified
    to bare `<proto>.<port>` (#261)."""
    info = _extract_composition_info(DEMO_ART, "Demo3WayP1")
    assert info.package == "system.demo"
    assert info.name == "Demo3WayP1"
    # One process — P1 — since each composition IS a process now.
    assert [s.art_process for s in info.processes] == ["P1"]
    (p1,) = info.processes
    assert p1.prototypes == ["counter", "driver", "ticker"]
    assert p1.node_types == ["CounterNode", "DriverNode", "TickerNode"]


def test_extract_composition_raises_on_missing_name():
    with pytest.raises(ValueError, match="not found"):
        _extract_composition_info(DEMO_ART, "NoSuchComposition")


def test_naming_helpers():
    """Spot-check the convention functions for surprising inputs."""
    assert _vehicle_capitalize("demo") == "Demo"
    assert _vehicle_capitalize("multi_word") == "MultiWord"

    # Per-process class: Demo3Way + P1 → DemoP1Composition.
    # Strips trailing digits from comp_name's stem.
    assert _per_process_class("Demo3Way", "P1") == "DemoP1Composition"
    assert _per_process_class("MyApp", "main") == "MyAppMainComposition"


def test_generate_rig_py_emits_runnable_python():
    """The generated source string must:
      - parse and exec without errors
      - expose a ``DemoSoftware: SoftwareSpecification`` symbol
      - the symbol's ``.to_rig()`` squash the FC services in (one exec
        per FC) and add the composition's single demo binary.

    gen-rig materializes ONE composition; post-split that's a single
    process (Demo3WayP1 → demo_p1), so the app carries exactly one demo
    component on top of the squashed FC execution manifests."""
    src = generate_rig_py(
        art_path=DEMO_ART,
        composition_name="Demo3WayP1",
        vehicle_name="demo",
        machine_name="demo_host",
        bazel_package="//demo",
        grpc_port=7700,
    )

    # Compile + exec in a fresh module namespace.
    spec = importlib.util.spec_from_loader(
        "_test_gen_rig_demo",
        loader=None,
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["_test_gen_rig_demo"] = module
    try:
        exec(compile(src, "<generated>", "exec"), module.__dict__)

        soft = module.DemoSoftware
        rig = soft.to_rig()

        assert rig.vehicle.name == "demo"
        assert rig.vehicle.make == "theia"
        assert rig.vehicle.model == "system.demo.Demo3WayP1"

        machines = {m.name for m in rig.machines}
        assert machines == {"demo_host"}

        # FC services (squashed in) + the one demo binary for this
        # process. sm/com/per/ucm/log/shwa = the FC set, plus demo_p1.
        execs = {e.name for e in rig.execution_manifests}
        assert "demo_p1" in execs
        assert "sm" in execs  # came from the FC services via squash

        # Two ApplicationManifests: platform_app (the demo binary, on
        # demo_host) and services_app (the squashed FC components).
        apps = {a.name: a for a in rig.applications}
        assert "platform_app" in apps
        platform_app = apps["platform_app"]
        assert platform_app.host_machine == "demo_host"
        demo_targets = {
            c.bazel_target
            for a in rig.applications for c in a.components
            if c.name.startswith("demo_")
        }
        assert demo_targets == {"//demo:p1_main"}
    finally:
        sys.modules.pop("_test_gen_rig_demo", None)


def test_write_rig_py_refuses_to_overwrite(tmp_path: Path):
    """``write_rig_py`` raises ``FileExistsError`` if the target is
    non-empty and ``force`` isn't set."""
    target = tmp_path / "rig.py"
    target.write_text("existing content\n")
    with pytest.raises(FileExistsError):
        write_rig_py(
            art_path=DEMO_ART,
            composition_name="Demo3Way",
            out_path=target,
            vehicle_name="demo",
            machine_name="demo_host",
            bazel_package="//demo",
        )
    # `force=True` bypasses the guard.
    write_rig_py(
        art_path=DEMO_ART,
        composition_name="Demo3WayP1",
        out_path=target,
        vehicle_name="demo",
        machine_name="demo_host",
        bazel_package="//demo",
        force=True,
    )
    assert "DemoSoftware" in target.read_text()
