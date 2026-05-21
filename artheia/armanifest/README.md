# armanifest — Adaptive AUTOSAR manifest model (scaffold)

Replaces `artheia.manifest` (the mosaic-syscomp port) with an
AUTOSAR-Adaptive-compliant split into four manifest kinds.

| File | Manifest kind | Granularity | Owns |
|---|---|---|---|
| `application.py` | **Application Manifest** | per application | SW component / composition design, executable description, process design |
| `machine.py`     | **Machine Manifest**     | per machine     | network interfaces, hardware resources, machine states |
| `service.py`     | **Service Manifest**     | per process     | data types, service interface defs, transport-layer endpoint bindings |
| `execution.py`   | **Execution Manifest**   | per process     | executable→process binding, timing/priority/resources, startup config + state deps |

Background: `docs/autosar/manifest.md`.

Status: scaffolding only. Dataclasses carry the structure; field-level
spec is TBD. Once the schema firms up:

1. Add a serializer (modelled on `artheia.manifest.serialize`).
2. Teach the CLI a `generate-armanifest` subcommand that runs a vehicle
   syscomp module and emits the four manifests separately.
3. Migrate `vendor.vehicles.*` syscomp files off `artheia.manifest`.
