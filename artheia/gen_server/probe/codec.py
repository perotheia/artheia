"""Lazy proto3 codec — compile a package's .proto to _pb2 on first use, cache.

protobuf 6 only (no gRPC). grpc_tools.protoc is used purely as the protoc
compiler; we emit _pb2 (never _pb2_grpc) and import it. Message classes are
keyed by the flattened nanopb type name (e.g. 'system_demo_Inc') so encode/
decode line up with the wire service_id.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

from artheia.generators.proto import _proto_package_name, package_subdir


class Codec:
    """Compiles + caches the _pb2 modules for the packages it's asked about.

    proto_root is where the committed .proto tree lives (platform/proto/).
    """

    def __init__(self, proto_root: str | Path):
        self.proto_root = Path(proto_root)
        self._out = Path(tempfile.mkdtemp(prefix="artheia_probe_pb2_"))
        # flattened proto package (e.g. 'system_demo') -> the _pb2 module
        self._modules: dict[str, object] = {}
        # message class cache: flat type name 'system_demo_Inc' -> class
        self._classes: dict[str, type] = {}

    # ---- compilation ------------------------------------------------------
    def _ensure_package(self, art_package: str) -> object:
        """Compile <root>/<subdir>/<leaf>.proto once; return its _pb2 module."""
        flat_pkg = _proto_package_name(art_package).replace(".", "_")
        if flat_pkg in self._modules:
            return self._modules[flat_pkg]

        subdir = package_subdir(art_package)
        leaf = art_package.split(".")[-1]
        proto_rel = subdir / f"{leaf}.proto"
        proto_abs = self.proto_root / proto_rel
        if not proto_abs.exists():
            raise FileNotFoundError(
                f"no .proto for package {art_package!r} at {proto_abs}"
            )

        from grpc_tools import protoc  # compiler only; no gRPC runtime imported

        rc = protoc.main([
            "",
            f"-I{self.proto_root}",
            f"--python_out={self._out}",
            str(proto_rel),
        ])
        if rc != 0:
            raise RuntimeError(f"protoc failed (rc={rc}) on {proto_rel}")

        # The emitted module mirrors the proto path: <subdir>/<leaf>_pb2.py
        pb2_path = self._out / subdir / f"{leaf}_pb2.py"
        mod_name = f"_artheia_probe_{flat_pkg}_pb2"
        spec = importlib.util.spec_from_file_location(mod_name, pb2_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        self._modules[flat_pkg] = module
        return module

    def _message_class(self, art_package: str, proto_type: str) -> type:
        """proto_type is the flat nanopb name, e.g. 'system_demo_Inc'.

        The generated _pb2 (package `system_demo`) exposes the message under
        its short name `Inc`; strip the flattened-package prefix to find it.
        """
        if proto_type in self._classes:
            return self._classes[proto_type]
        module = self._ensure_package(art_package)
        flat_pkg = _proto_package_name(art_package).replace(".", "_")
        short = proto_type[len(flat_pkg) + 1:]  # drop 'system_demo_'
        cls = getattr(module, short)
        self._classes[proto_type] = cls
        return cls

    # ---- encode / decode --------------------------------------------------
    def encode(self, art_package: str, proto_type: str, **fields) -> bytes:
        cls = self._message_class(art_package, proto_type)
        msg = cls(**fields)
        return msg.SerializeToString()

    def decode(self, art_package: str, proto_type: str, data: bytes) -> dict:
        cls = self._message_class(art_package, proto_type)
        msg = cls()
        msg.ParseFromString(data)
        # Return a plain dict of set + default scalar fields.
        return {f.name: getattr(msg, f.name) for f in cls.DESCRIPTOR.fields}
