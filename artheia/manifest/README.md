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
| `rig.py`        | :class:`Rig` bundles the four manifest kinds + a vehicle identity + machine list. |
| `layer.py`      | :class:`Layer` + :func:`merge_layers` — compose deltas (platform → vehicle-family → rig). |
| `transform.py`  | identity-keyed :class:`Add` / :class:`Remove` / :class:`Override` primitives. |
| `clusters.py`   | :data:`CLUSTERS` catalogue of the 18 Adaptive Platform Functional Clusters by short name. |
| `platform.py`   | :data:`PlatformBase` — the L0 rig synthesized from `platforms/system/services/<short>/package.art`. |
| `loader.py`     | textX-driven loader that turns `.art` files into Service + Execution manifests. |
| `supervisor.py` | :func:`build_supervisor_tree` — composes the supervisor view used by `artheia executor emit`. |

Background: `docs/AUTOSAR/manifest.md` and `docs/AUTOSAR/adaptive.md`.

CLI surface:

- `artheia generate-manifest <module>` — emit the full Rig as YAML.
- `artheia executor emit <module>` — emit only the supervisor tree
  (`executor.yaml`) consumed by `services/supervisor/build/supervisor`.
