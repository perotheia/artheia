"""Stand-ins for the protobuf types the original mosaic model imported.

The original Adaptive-AUTOSAR-adjacent code referenced a single bazel-generated
protobuf module: ``tools.orchestrate.proto.rig_pb2``. It is not portable
outside the mosaic monorepo, but the only symbol the manifest model actually
consumes is the ``AAOSBuildType`` enum.

Defining it as a plain :class:`enum.Enum` keeps :mod:`artheia.manifest.core`
unchanged while letting the package import cleanly from anywhere.

Anything else that used to be a protobuf message (cluster config, camera
config, etc.) is described in artheia ``.art`` files instead and lives
outside the Python ADT — see ``artheia/docs/MANUAL.md``.
"""

from __future__ import annotations

from enum import Enum


class AAOSBuildType(Enum):
    AAOS_BUILD_TYPE_UNKNOWN = "AAOS_BUILD_TYPE_UNKNOWN"
    AAOS_BUILD_TYPE_PHYSICAL_RELEASE = "AAOS_BUILD_TYPE_PHYSICAL_RELEASE"
    AAOS_BUILD_TYPE_PHYSICAL_USERDEBUG = "AAOS_BUILD_TYPE_PHYSICAL_USERDEBUG"
    AAOS_BUILD_TYPE_EMULATOR = "AAOS_BUILD_TYPE_EMULATOR"


class _RigPb2Shim:
    """Compatibility shim so legacy ``rig_pb2.AAOSBuildType`` references work."""

    AAOSBuildType = AAOSBuildType


rig_pb2 = _RigPb2Shim()
