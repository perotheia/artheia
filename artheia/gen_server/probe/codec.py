"""Lazy proto3 codec — compile a package's .proto to _pb2 on first use, cache.

protobuf 6 only (no gRPC). grpc_tools.protoc is used purely as the protoc
compiler; we emit _pb2 (never _pb2_grpc) and import it. Message classes are
keyed by the flattened nanopb type name (e.g. 'system_app_MyMsg') so encode/
decode line up with the wire service_id.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from artheia.generators.proto import _proto_package_name, package_subdir


class Codec:
    """Compiles + caches the _pb2 modules for the packages it's asked about.

    proto_root is where the committed .proto tree lives (platform/proto/).
    """

    # ONE process-shared _pb2 output dir across ALL Codec instances. protoc lays
    # each package under <_out>/<pkg-tree>/ with __init__.py's, and _ensure_package
    # imports them as `<pkg>.<leaf>_pb2`. Python caches the TOP package (e.g.
    # `system`) as a namespace package the first time ANY sub-package is imported.
    # If a second Codec used its OWN temp dir, that cached `system` namespace
    # would NOT span the second dir, so a different sub-package
    # (system.services.log vs system.supervisor) would fail with
    # "No module named 'system.services'". Sharing one dir makes every
    # sub-package live under the same `system`/`platform` namespace root.
    _SHARED_OUT: "Path | None" = None

    def __init__(self, proto_root: str | Path):
        self.proto_root = Path(proto_root)
        if Codec._SHARED_OUT is None:
            Codec._SHARED_OUT = Path(tempfile.mkdtemp(prefix="artheia_probe_pb2_"))
        self._out = Codec._SHARED_OUT
        # flattened proto package (e.g. 'system_app') -> the _pb2 module
        self._modules: dict[str, object] = {}
        # message class cache: flat type name 'system_app_MyMsg' -> class
        self._classes: dict[str, type] = {}

    # ---- compilation ------------------------------------------------------
    def _ensure_package(self, art_package: str) -> object:
        """Compile <root>/<subdir>/<leaf>.proto once; return its _pb2 module."""
        flat_pkg = _proto_package_name(art_package).replace(".", "_")
        if flat_pkg in self._modules:
            return self._modules[flat_pkg]

        # Two on-disk layouts exist:
        #   (1) dotted dirs  — system.services.per  -> system/services/per/per.proto
        #   (2) flat single  — platform.runtime     -> platform_runtime/runtime.proto
        # Most packages use (1) (package_subdir mirrors the .art tree). The
        # runtime control proto is laid out flat (matching the `import
        # "platform_runtime/runtime.proto"` convention every consumer uses), so
        # try the dotted path first, then the flat-underscore dir.
        from pathlib import Path as _Path
        leaf = art_package.split(".")[-1]
        candidates = [
            package_subdir(art_package) / f"{leaf}.proto",        # (1) dotted
            _Path(flat_pkg) / f"{leaf}.proto",                    # (2) flat dir
        ]
        proto_rel = None
        for cand in candidates:
            if (self.proto_root / cand).exists():
                proto_rel = cand
                break
        if proto_rel is None:
            raise FileNotFoundError(
                f"no .proto for package {art_package!r} at any of "
                + ", ".join(str(self.proto_root / c) for c in candidates)
            )
        subdir = proto_rel.parent
        proto_abs = self.proto_root / proto_rel

        from grpc_tools import protoc  # compiler only; no gRPC runtime imported

        # Compile the target proto AND any protos it imports, in one call, so a
        # cross-package message (e.g. supervisor's TraceConfig embedding
        # platform.runtime.TraceControlPush) gets its imported _pb2 emitted too.
        # The generated supervisor_pb2 does `from platform_runtime import
        # runtime_pb2`, a package-relative import, so self._out must be on
        # sys.path with package __init__.py's (protoc lays the tree out, we add
        # the inits).
        deps = self._collect_proto_imports(proto_abs)
        targets = [str(proto_rel)] + [str(d) for d in deps]
        rc = protoc.main([
            "",
            f"-I{self.proto_root}",
            f"--python_out={self._out}",
            *targets,
        ])
        if rc != 0:
            raise RuntimeError(f"protoc failed (rc={rc}) on {proto_rel}")

        # Make the emitted tree importable as packages (the generated code uses
        # absolute `from <pkg> import <leaf>_pb2`). Add __init__.py per dir and
        # put self._out on sys.path once.
        self._make_importable_tree()

        # Import the target module by its package path (mirrors proto layout):
        # <subdir-as-module>.<leaf>_pb2
        mod_path = ".".join(subdir.parts + (f"{leaf}_pb2",))
        import importlib as _il
        module = _il.import_module(mod_path)
        self._modules[flat_pkg] = module
        return module

    def _collect_proto_imports(self, proto_abs: "Path") -> "list":
        """Return the import paths (relative to proto_root) a .proto declares,
        recursively, so they get compiled alongside it."""
        from pathlib import Path as _P
        seen: set = set()
        out: list = []

        def walk(p: _P):
            try:
                text = p.read_text()
            except Exception:
                return
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("import ") and line.endswith(".proto\";"):
                    rel = line.split('"', 2)[1]
                    if rel in seen:
                        continue
                    seen.add(rel)
                    out.append(_P(rel))
                    dep_abs = self.proto_root / rel
                    if dep_abs.exists():
                        walk(dep_abs)

        walk(proto_abs)
        return out

    def _make_importable_tree(self) -> None:
        """Drop __init__.py into every dir under self._out and ensure self._out
        is on sys.path, so protoc's package-relative `from pkg import x_pb2`
        imports resolve."""
        out = str(self._out)
        if out not in sys.path:
            sys.path.insert(0, out)
        for d, _dirs, _files in os.walk(self._out):
            init = os.path.join(d, "__init__.py")
            if not os.path.exists(init):
                open(init, "w").close()

    def _message_class(self, art_package: str, proto_type: str) -> type:
        """proto_type is the flat nanopb name, e.g. 'system_app_MyMsg'.

        The generated _pb2 (package `system_app`) exposes the message under
        its short name `MyMsg`; strip the flattened-package prefix to find it.
        """
        if proto_type in self._classes:
            return self._classes[proto_type]
        module = self._ensure_package(art_package)
        flat_pkg = _proto_package_name(art_package).replace(".", "_")
        short = proto_type[len(flat_pkg) + 1:]  # drop 'system_app_'
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
