# artheia.manifest — Adaptive AUTOSAR manifest model

AUTOSAR-Adaptive-compliant split into four manifest kinds. Replaced the
mosaic-syscomp port that used to live under this name.

| File | Manifest kind | Granularity | Owns |
|---|---|---|---|
| `application.py` | **Application Manifest** | per application | SW component / composition design, executable description, process design |
| `machine.py`     | **Machine Manifest**     | per machine     | network interfaces, hardware resources, machine states |
| `service.py`     | **Service Manifest**     | per process     | data types, service interface defs, transport-layer endpoint bindings |
| `execution.py`   | **Execution Manifest**   | per process     | executable→process binding, timing/priority/resources, startup config + state deps |

Plus the composition layer:

| File | Purpose |
|---|---|
| `rig.py`        | :class:`Rig` (legacy flat-list shape) and :class:`SoftwareSpecification` (structured DSL — set-typed fields with inline :class:`Append` / :class:`Remove` transforms). :meth:`SoftwareSpecification.to_rig` bridges between them during the migration. |
| `transform.py`  | DSL engine: :class:`Layer` (with `.squash()`), :class:`Identifiable`, set transforms (:class:`Append` / :class:`Remove`), value markers (:class:`Undefined` / :class:`Default` / :class:`Defer`), :func:`identifiable_dataclass` decorator. Legacy :class:`Add` / :class:`Override` / :func:`apply_ops` compat shims at the bottom. |
| `layer.py`      | Legacy flat-list :class:`Layer` + :func:`merge_layers` — parallel `add_<X>` / `remove_<X>` / `override_<X>` list families per element kind. Used by ``services/manifest/fc.py::FcLayer`` and ``demo/manifest/rig.py::DemoLayer`` until those call sites migrate to :class:`SoftwareSpecification`. |
| `clusters.py`   | :data:`CLUSTERS` catalogue of the 18 Adaptive Platform Functional Clusters by short name. |
| `platform.py`   | :data:`PlatformBase` — the L0 rig sourced from :mod:`services.manifest.fc` and paired with the :class:`ServiceManifest` derived from `platform/system/services/<short>/package.art`. |
| `loader.py`     | textX-driven loader that turns `.art` files into Service + Execution manifests. |
| `supervisor.py` | :func:`build_supervisor_tree` — composes the supervisor view used by `artheia executor emit`. |

Background:

- `docs/manifest-dsl.md` — structured-DSL reference (`Layer.squash`,
  `SoftwareSpecification`, set transforms).
- `docs/tutorials/gen-rig.md` — bootstrap a vendor rig.py from `.art`.
- `docs/AUTOSAR/manifest.md`, `docs/AUTOSAR/adaptive.md` — upstream
  Adaptive AUTOSAR manifest spec.
- `docs/tasks/DONE/artheia-dsl-recovery.md` — design decisions behind
  the DSL recovery.

CLI surface:

- `artheia gen-rig <art_file> --composition <name> --out <path>` —
  bootstrap a vendor rig.py from an artheia composition.
- `artheia generate-manifest <module>` — emit the full Rig as YAML.
- `artheia executor emit <module>` — emit only the supervisor tree
  (`executor.yaml`) consumed by `services/supervisor/build/supervisor`.
- `artheia gui emit <module>` — emit the GUI manifest (machines.yaml).

All emit commands accept either a legacy `Rig` or the new
`SoftwareSpecification` export and auto-materialize via
`to_rig()`. Auto-pick prefers `*Software` over `*Rig`.
