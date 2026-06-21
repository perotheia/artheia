"""Standalone-app generator — the ``--kind lib`` arm of ``gen-app``.

A SIBLING to :mod:`.fc_app`, sharing its model walk and template set.
Different output shape: emits ``<out>/platform/{lib,impl,runtime,generated}/``
plus a top-level ``CMakeLists.txt`` so the app builds standalone on its
target (e.g. an RPi4) — **no Bazel, no workspace dependencies**.

Specifically vs. :mod:`.fc_app`:

- **No ``main/`` slice.** The standalone app owns its own ``main`` and
  drives runnable lifecycle itself. Lib mode exposes the node classes
  + start/stop API; the app constructs and orchestrates.
- **No ``BUILD.bazel`` files.** A hand-rolled ``CMakeLists.txt`` covers
  the lib + impl + vendored runtime in one tree.
- **Vendors ``platform/runtime``.** All headers from
  ``platform/runtime/include/`` and sources from
  ``platform/runtime/src/`` get copied into
  ``<out>/platform/runtime/`` so the app's CMake build needs nothing
  outside the app tree.
- **Co-locates protos.** ``--proto-out <out>/platform/generated`` is
  the default so the generated ``.proto`` (and the nanopb output the
  user runs next) live inside the app.

Use case: ``odd_path_monitor`` builds on an RPi4 natively (CMake) and
links no workspace targets — the gateway runs separately as a Linux
service shipping its own ``.ipk`` cross-compiled from the host.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ..model.loader import parse_file
from .fc_app import _build_model_view, _env, _write


def _nodes_instantiated_by_compositions(art_path: Path) -> set[str]:
    """Return the set of node-type names referenced as prototypes in any
    composition declared in *art_path* (the merged package.art +
    component.art).

    The app spec convention: a node is APP-OWNED only when the app's own
    composition instantiates it. Cross-package nodes pulled in via
    `import` (like the PSP's mega-node forward-decl in package.art)
    show up as NodeDecls in the model but are NOT prototypes of any
    local composition — so we filter them out before emitting C++.

    Returns an empty set if the file declares no compositions (in
    which case the caller should fall back to emitting all NodeDecls;
    legacy `fc_app` behaviour).
    """
    model = parse_file(str(art_path))
    instantiated: set[str] = set()
    for el in model.elements:
        if el.__class__.__name__ != "CompositionDecl":
            continue
        for sub in (getattr(el, "elements", []) or []):
            if sub.__class__.__name__ != "PrototypeDecl":
                continue
            # sub.type is the resolved NodeDecl reference; its `name`
            # attribute is the node-type name (e.g. "FlexRayIngress").
            t = getattr(sub, "type", None)
            n = getattr(t, "name", None)
            if n:
                instantiated.add(n)
    return instantiated


# Resolved at import time from this module's location. The runtime
# subtree lives at the workspace root. This module is at
# <ws>/artheia/artheia/generators/lib_app.py, so:
#   parents[2] = <ws>/artheia/            (artheia repo root)
#   parents[3] = <ws>/                    (workspace root)
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_SRC = _WORKSPACE_ROOT / "platform" / "runtime"


def _copy_runtime(dst: Path) -> list[str]:
    """Mirror platform/runtime/{include,src} into <dst>. Idempotent;
    overwrites existing files (regen semantics — never user-edited).

    The runtime is self-contained: its RPC wire header is the runtime's own
    TheiaMsgHeader.hh — the old libgw `gw_proto.h` dependency was SEVERED
    (platform/runtime no longer #includes anything from gateway/libs/libgw).
    So a --kind lib app vendors only platform/runtime/, nothing from gateway.

    Returns the list of paths written, for the "wrote" bucket.
    """
    wrote: list[str] = []

    # platform/runtime/{include,src} → <dst>/runtime/{include,src}
    for sub in ("include", "src"):
        src = _RUNTIME_SRC / sub
        if not src.is_dir():
            raise RuntimeError(
                f"platform/runtime/{sub} not found at {src}; "
                f"--kind lib needs the workspace runtime to vendor."
            )
        target = dst / sub
        target.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if not f.is_file():
                continue
            t = target / f.name
            shutil.copy2(f, t)
            wrote.append(str(t))

    return wrote


def _nanopb_compile(generated_dir: Path,
                    proto_path: Path,
                    include_root: Path) -> list[str]:
    """Nanopb-compile a single .proto into <generated_dir>/<pkg-path>/.

    The output path under generated_dir mirrors the proto's path
    relative to include_root — so passing
    platform/runtime/proto/platform_runtime/runtime.proto with
    include_root=platform/runtime/proto writes
    <generated_dir>/platform_runtime/runtime.pb.{c,h}.

    Workspace policy: rather than make every standalone-app build
    depend on the user remembering to run nanopb_generator, we run
    it from the host where the workspace + venv are available, and
    ship the compiled .pb.{c,h} alongside the runtime sources. The
    target needs only `pb.h` + `libprotobuf-nanopb.so` at build time.
    """
    if not proto_path.is_file():
        raise RuntimeError(f"proto not found at {proto_path}")

    rel = proto_path.relative_to(include_root)
    out_subdir = generated_dir / rel.parent
    out_subdir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["nanopb_generator",
         "-I", str(include_root),
         "--output-dir", str(generated_dir),
         str(proto_path)],
        check=True,
    )
    stem = rel.stem
    return [
        str(out_subdir / f"{stem}.pb.h"),
        str(out_subdir / f"{stem}.pb.c"),
    ]


def _nanopb_compile_runtime_proto(generated_dir: Path) -> list[str]:
    """Nanopb-compile platform/runtime/proto/platform_runtime/runtime.proto
    into <generated_dir>/platform_runtime/runtime.pb.{c,h}.

    The runtime's GenServer.hh / Tracer.hh / etc. #include
    "platform_runtime/runtime.pb.h" for the LogLevelPush /
    TraceControlPush control messages. Without the .pb.{c,h} present,
    nothing under runtime/src/ compiles.
    """
    proto = _WORKSPACE_ROOT / "platform" / "runtime" / "proto" / "platform_runtime" / "runtime.proto"
    proto_root = _WORKSPACE_ROOT / "platform" / "runtime" / "proto"
    return _nanopb_compile(generated_dir, proto, proto_root)


# ----------------------------------------------------------------------------
# CMakeLists.txt — emitted directly (no Jinja). Small enough that a string
# template is clearer than a separate file under templates/.
# ----------------------------------------------------------------------------

_CMAKELISTS_TEMPLATE = """\
# AUTO-GENERATED by `artheia gen-app --kind lib` — DO NOT EDIT.
# Standalone-app platform/ slice for {fc_short} ({source_file}).
#
# Builds on the target (e.g. RPi4) with plain CMake — no Bazel, no
# workspace dependencies. The app's top-level CMakeLists.txt does
# `add_subdirectory(platform)` and links against the {fc_short}_lib
# target below.
cmake_minimum_required(VERSION 3.16)
project({fc_short}_platform CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ── Vendored Theia runtime — built from runtime/src/ ────────────────────
# Header-only consumers include from runtime/include/; the few units
# with .cc bodies (Logger, NodeRef, Timer, TimerService, TipcMux, Clock)
# get rolled into platform_runtime.
file(GLOB _RUNTIME_SRCS CONFIGURE_DEPENDS
    "${{CMAKE_CURRENT_SOURCE_DIR}}/runtime/src/*.cc"
)
add_library(platform_runtime STATIC ${{_RUNTIME_SRCS}})
target_include_directories(platform_runtime PUBLIC
    "${{CMAKE_CURRENT_SOURCE_DIR}}/runtime/include"
    "${{CMAKE_CURRENT_SOURCE_DIR}}/generated"
)
target_compile_options(platform_runtime PRIVATE -Wall -Wextra)
# nanopb + pthread — nanopb is a system dep; pthread for std::thread.
find_package(Threads REQUIRED)
target_link_libraries(platform_runtime PUBLIC Threads::Threads)

# nanopb — try pkg-config first; fall back to a system include.
find_path(NANOPB_INCLUDE_DIR pb.h
    HINTS /usr/include /usr/include/nanopb /opt/homebrew/include)
if(NANOPB_INCLUDE_DIR)
    target_include_directories(platform_runtime PUBLIC ${{NANOPB_INCLUDE_DIR}})
endif()
find_library(NANOPB_LIB protobuf-nanopb
    HINTS /usr/lib /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu)
if(NANOPB_LIB)
    target_link_libraries(platform_runtime PUBLIC ${{NANOPB_LIB}})
endif()

# ── Generated nanopb messages — built from generated/**/*.pb.c ─────────
# Empty by default; populated after running the nanopb generator on the
# .proto files emitted under generated/<package-path>/. CMake CONFIGURE_DEPENDS
# re-runs the glob when files appear/disappear (CMake 3.12+).
file(GLOB_RECURSE _PB_SRCS CONFIGURE_DEPENDS
    "${{CMAKE_CURRENT_SOURCE_DIR}}/generated/*.pb.c"
)
if(_PB_SRCS)
    add_library({fc_short}_protos STATIC ${{_PB_SRCS}})
    target_include_directories({fc_short}_protos PUBLIC
        "${{CMAKE_CURRENT_SOURCE_DIR}}/generated"
    )
    if(NANOPB_INCLUDE_DIR)
        target_include_directories({fc_short}_protos PUBLIC ${{NANOPB_INCLUDE_DIR}})
    endif()
    target_link_libraries({fc_short}_protos PUBLIC platform_runtime)
else()
    # Header-only stand-in so the lib target can always link us, even
    # before the user has run nanopb_generator. Compiles to nothing.
    add_library({fc_short}_protos INTERFACE)
    target_include_directories({fc_short}_protos INTERFACE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/generated"
    )
    target_link_libraries({fc_short}_protos INTERFACE platform_runtime)
endif()

# ── Lib slice — the generated node headers ────────────────────────────
# Header-only: per-node Daemon.hh + Daemon_netgraph.hh, plus FC-wide
# Log.hh and {fc_short}_codecs.hh. Bodies live in impl/.
#
# Include root is THIS directory (the platform/ root), not lib/ — so
# impl/*.cc and the app's own consumers can `#include "lib/Foo.hh"`
# the same way the workspace's FCs do.
add_library({fc_short}_lib INTERFACE)
target_include_directories({fc_short}_lib INTERFACE
    "${{CMAKE_CURRENT_SOURCE_DIR}}"
)
target_link_libraries({fc_short}_lib INTERFACE {fc_short}_protos)

# ── Impl slice — the user-owned handler bodies ────────────────────────
# Per-node *_handlers.cc, write-once on first emit; the user fills these
# in. Listed explicitly so a CMake reconfigure picks up renames cleanly.
add_library({fc_short}_impl STATIC
{handler_srcs}
)
target_link_libraries({fc_short}_impl PUBLIC {fc_short}_lib)
target_compile_options({fc_short}_impl PRIVATE -Wall -Wextra)

# ── Exposed surface for the parent CMakeLists.txt ─────────────────────
# The app's top-level main.cpp links against {fc_short}_impl, which
# transitively pulls {fc_short}_lib → {fc_short}_protos → platform_runtime.
"""


def _emit_cmakelists(fc_short: str, source_file: str, handler_srcs: list[str]) -> str:
    """Render the CMakeLists.txt for the lib-mode platform/ slice.

    handler_srcs is a list of `impl/<Node>_handlers.cc` paths relative
    to <out>. Listed explicitly (not a glob) so a node-removal in .art
    fails the CMake configure visibly instead of silently shipping a
    stale handler.
    """
    lines = "\n".join(f"    {p}" for p in handler_srcs)
    return _CMAKELISTS_TEMPLATE.format(
        fc_short=fc_short,
        source_file=source_file,
        handler_srcs=lines,
    )


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------

def generate_lib(
    art_path: str | Path,
    out_dir: str | Path,
    *,
    proto_out: Optional[str | Path] = None,
    cxx_namespace: Optional[str] = None,
    force: bool = False,
) -> dict[str, list[str]]:
    """Generate the standalone-app ``platform/`` slice for one ``.art``.

    :param art_path:    ``.art`` file (typically ``vendor/<app>/system/<app>/component.art``).
    :param out_dir:     Where ``platform/{lib,impl,runtime,generated}`` lands
                        (typically ``vendor/<app>/platform/``).
    :param proto_out:   Where ``.proto`` files land (defaults to
                        ``<out_dir>/generated/`` so everything is
                        self-contained). Pass an explicit path to put
                        the proto tree elsewhere.
    :param cxx_namespace: C++ namespace override (same semantics as
                        ``--kind fc``).
    :param force:       Overwrite the impl slice (write-once after
                        first emit).

    Returns ``{status: [path,...]}`` for "wrote", "overwrote",
    "skipped-exists" — same shape as :func:`fc_app.generate_fc`.
    """
    # .absolute() NOT .resolve(): a consuming workspace links the app's .art in
    # (system/<app> -> ../../<app>/system/<app>) so its `import system.autosar.*`
    # resolves against the WORKSPACE's system/ tree (where system/autosar is
    # linked). .resolve() would follow the symlink to the app's own repo, where
    # the PSP import dir doesn't exist — defeating import resolution. .absolute()
    # keeps the in-workspace path so _import_dir climbs the right tree.
    art_path = Path(art_path).absolute()
    out_dir = Path(out_dir)

    # Default proto output lives inside the app tree — `--kind lib`'s
    # whole reason for being is "self-sufficient".
    if proto_out is None:
        proto_out = out_dir / "generated"
    else:
        proto_out = Path(proto_out)

    mv = _build_model_view(art_path, cxx_namespace_override=cxx_namespace)
    env = _env()

    # Filter to APP-OWNED nodes only — those instantiated as prototypes
    # by a composition in this .art. Anything else is a forward-decl
    # stub for cross-package resolution (e.g. the PSP mega-node) and
    # must NOT get C++ emitted on the app side.
    owned = _nodes_instantiated_by_compositions(art_path)
    if owned:
        skipped = [n.name for n in mv.nodes if n.name not in owned]
        mv.nodes = [n for n in mv.nodes if n.name in owned]
        if not mv.nodes:
            raise RuntimeError(
                f"--kind lib: no app-owned nodes found in {art_path}. "
                f"Declare a composition with at least one `prototype <Node> <name>`."
            )
        # Silent skip is fine — `_nodes_instantiated_by_compositions`'s
        # docstring explains the rule and the result list reflects what
        # got emitted. (Keeping a reference for diagnostics if needed.)
        _ = skipped

    results: dict[str, list[str]] = {
        "wrote": [], "overwrote": [], "skipped-exists": [],
    }

    ctx = {"model": mv, "source_file": str(art_path)}

    lib_dir = out_dir / "lib"
    impl_dir = out_dir / "impl"

    handler_srcs: list[str] = []
    for nv in mv.nodes:
        node_ctx = {**ctx, "node": nv}
        if nv.runnable:
            node_suffix = ".runnable"
        elif nv.statem is not None:
            node_suffix = ".statem"
        else:
            node_suffix = ""

        # Per-node Daemon header (regen).
        p = lib_dir / f"{nv.name}.hh"
        results[_write(p,
                       env.get_template(f"Daemon{node_suffix}.hh.j2").render(**node_ctx),
                       overwrite=True)].append(str(p))
        # Per-node netgraph (regen) — outbound peer addresses.
        p = lib_dir / f"{nv.name}_netgraph.hh"
        results[_write(p,
                       env.get_template("Netgraph.hh.j2").render(**node_ctx),
                       overwrite=True)].append(str(p))
        # State struct (write-once, APP-OWNED) — the shared Daemon.hh.j2 header
        # `#include`s "impl/<Node>_state.hh", so it MUST be emitted or the lib
        # won't compile. A statem node carries its data in the FSM holder, so it
        # has no per-node state header (matches fc_app). overwrite=force keeps a
        # user's hand-filled state from being clobbered on regen.
        if nv.statem is None:
            p = impl_dir / f"{nv.name}_state.hh"
            results[_write(p,
                           env.get_template("state.hh.j2").render(**node_ctx),
                           overwrite=force)].append(str(p))
        # Handler stubs (write-once unless --force).
        p = impl_dir / f"{nv.name}_handlers.cc"
        results[_write(p,
                       env.get_template(f"handlers{node_suffix}.cc.j2").render(**node_ctx),
                       overwrite=force)].append(str(p))
        handler_srcs.append(f"impl/{nv.name}_handlers.cc")

    # FC-wide codecs + log header.
    p = lib_dir / f"{mv.fc_short}_codecs.hh"
    results[_write(p,
                   env.get_template("Codecs.hh.j2").render(**ctx),
                   overwrite=True)].append(str(p))
    p = lib_dir / "Log.hh"
    results[_write(p,
                   env.get_template("Log.hh.j2").render(**ctx),
                   overwrite=True)].append(str(p))

    # Vendor the Theia runtime into <out>/runtime/. Headers + .cc files
    # both come along so the app's CMake build links a self-contained
    # libplatform_runtime without reaching out to the workspace.
    runtime_dir = out_dir / "runtime"
    for p in _copy_runtime(runtime_dir):
        results["wrote"].append(p)

    # Nanopb-compile the runtime's control proto into <out>/generated/.
    # Done at generator-time (not CMake-time) because the proto is
    # workspace-only and the target won't have it. Runs nanopb_generator
    # from PATH (typically the workspace .venv); errors loud if absent.
    generated_dir = out_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    for p in _nanopb_compile_runtime_proto(generated_dir):
        results["wrote"].append(p)

    # Top-level CMakeLists.txt. Always regen — never user-edited (the
    # banner comment says so; users wire the parent CMakeLists.txt
    # via add_subdirectory).
    p = out_dir / "CMakeLists.txt"
    results[_write(p,
                   _emit_cmakelists(mv.fc_short, str(art_path), handler_srcs),
                   overwrite=True)].append(str(p))

    # Proto slice — same machinery as --kind fc, just defaulted into
    # <out>/generated/ instead of platform/proto/.
    from .proto_package import generate_package_proto
    proto_path = generate_package_proto(art_path, proto_out)
    results["wrote"].append(str(proto_path))

    # …and nanopb-compile it right away so the impl/*.cc that
    # #includes the .pb.h finds it. proto_out is the include root
    # (matches how the .proto's `package` line lines up with the
    # subdir path generate_package_proto already laid out for us).
    for p in _nanopb_compile(generated_dir, Path(proto_path), Path(proto_out)):
        results["wrote"].append(p)

    # Imported-package protos. A standalone app vendors EVERYTHING it
    # needs, so any inbound message resolved from another package (e.g. a
    # PSP bus PDU pulled in via `import …flexray.*`) needs that package's
    # .proto + .pb.{c,h} under generated/ too — otherwise the codec's
    # `#include "<pkg-path>/<leaf>.pb.h"` won't resolve. Emit one per
    # distinct imported defining-package the model references.
    for imp_art in _imported_proto_sources(art_path, mv):
        ip = generate_package_proto(str(imp_art), proto_out)
        results["wrote"].append(str(ip))
        for p in _nanopb_compile(generated_dir, Path(ip), Path(proto_out)):
            results["wrote"].append(p)

    return results


def _imported_proto_sources(art_path: Path, mv) -> list[Path]:
    """Source ``.art`` files for every IMPORTED defining-package the model's
    inbound messages resolve to (deduped), excluding the app's own package.

    The standalone build must vendor these packages' protos. We map each
    distinct ``proto_subpath`` on a receiver-port data element back to its
    source package directory via the same import-resolution the scope
    provider uses, then point ``generate_package_proto`` at that dir."""
    from ..model.scope import _import_dir
    from ..model import parse_file as _pf

    entry = Path(art_path).absolute()
    model = _pf(str(art_path))
    entry_pkg = getattr(model, "name", "") or ""
    own_subpath = "/".join(entry_pkg.split(".")) if entry_pkg else ""

    # Map import-package-FQN -> its resolved directory (from this model's
    # `import` lines).
    imp_dirs: dict[str, Path] = {}
    for imp in getattr(model, "imports", []) or []:
        ipkg = imp.name[:-2] if imp.name.endswith(".*") else imp.name
        d = _import_dir(entry, entry_pkg, ipkg)
        if d is not None and d.is_dir():
            imp_dirs[ipkg] = d

    # Distinct defining-package subpaths referenced by inbound data.
    wanted: dict[str, Path] = {}  # subpath -> source .art
    for nv in mv.nodes:
        for port in getattr(nv, "ports", []):
            for d in getattr(port, "data", []):
                sub = getattr(d, "proto_subpath", "")
                if not sub or sub == own_subpath:
                    continue
                pkg_fqn = sub.replace("/", ".")
                src_dir = imp_dirs.get(pkg_fqn)
                if src_dir is None:
                    continue
                # Prefer package.art (schema layer carries the messages).
                for fname in ("package.art", "system.art", "component.art"):
                    cand = src_dir / fname
                    if cand.exists():
                        wanted[sub] = cand
                        break
    return list(wanted.values())
