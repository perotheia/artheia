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

    # Process-shared content ledger: <proto path relative to its root> →
    # sha256 of the .proto SOURCE that was compiled+imported at that module
    # path. A second Codec (second ArtheiaContext) asking to compile the SAME
    # proto path with DIFFERENT content is a hard error: protoc would
    # overwrite the shared _pb2 file, but sys.modules (and protobuf's
    # descriptor pool, which registers by file path) keep the FIRST version —
    # encode would then use stale descriptors and emit wrong tags, which the
    # peer's nanopb decode silently drops. One process cannot hold two
    # versions of one proto; failing loudly here turns a lost-cast heisenbug
    # into an immediate, actionable error (seen in the wild: two robot tests
    # building contexts against protos regenerated in between).
    _COMPILED: "dict[str, str]" = {}

    def __init__(self, proto_root: str | Path):
        self.proto_root = Path(proto_root)
        # FRAMEWORK FALLBACK ROOT: a workspace proto may embed platform
        # common types (platform.msgs.geometry.Vec2 → `import
        # "platform/msgs/geometry/geometry.proto"`), which live under
        # $THEIA_ROOT/platform/proto — not the workspace root. Resolve
        # imports across both roots (workspace first) so the probe keeps
        # working on such packages (env.sh exports THEIA_ROOT; without it
        # behavior is unchanged).
        self._roots = [self.proto_root]
        _tr = os.environ.get("THEIA_ROOT")
        if _tr:
            _fp = Path(_tr) / "platform" / "proto"
            if _fp.is_dir():
                self._roots.append(_fp)
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
        all_rel = [proto_rel] + list(deps)

        # Content guard (see _COMPILED): compile each proto path at most once
        # per process; a re-request with IDENTICAL content reuses the already-
        # imported module tree, a re-request with DIFFERENT content fails loud.
        import hashlib
        targets = []
        for rel in all_rel:
            src = self._resolve_rel(rel)
            digest = hashlib.sha256(src.read_bytes()).hexdigest() if src.exists() else ""
            prev = Codec._COMPILED.get(str(rel))
            if prev is None:
                Codec._COMPILED[str(rel)] = digest
                targets.append(str(rel))
            elif prev != digest:
                raise RuntimeError(
                    f"proto {rel} (root {self.proto_root}) differs from the "
                    f"version already compiled+imported in this process by an "
                    f"earlier ArtheiaContext/Codec. One process cannot hold two "
                    f"versions of one proto (sys.modules + the protobuf "
                    f"descriptor pool keep the first) — encoding with the stale "
                    f"one would emit wire bytes the peer silently drops. Use a "
                    f"fresh process for the regenerated proto tree, or one "
                    f"context per proto snapshot."
                )
            # prev == digest → already compiled; skip recompilation.
        if targets:
            rc = protoc.main([
                "",
                *[f"-I{r}" for r in self._roots],
                f"--python_out={self._out}",
                *targets,
            ])
            if rc != 0:
                # Roll back the ledger for the paths we claimed but failed to
                # compile, so a later (correct) attempt isn't spuriously blocked.
                for t in targets:
                    Codec._COMPILED.pop(t, None)
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

    def _resolve_rel(self, rel) -> "Path":
        """First include root containing `rel` (workspace first, then the
        framework fallback); the workspace path if none — the caller's
        exists() check then reports it missing against the primary root."""
        for r in self._roots:
            p = Path(r) / rel
            if p.exists():
                return p
        return self.proto_root / rel

    def _collect_proto_imports(self, proto_abs: "Path") -> "list":
        """Return the import paths (relative to an include root) a .proto
        declares, recursively, so they get compiled alongside it."""
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
                    dep_abs = self._resolve_rel(rel)
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
