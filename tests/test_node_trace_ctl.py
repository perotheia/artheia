"""gen-app: NodeTraceCtl filter-map emission for reporting=true nodes.

Per #363: every reporting=true NodeDecl gets per-(node, msg_type)
trace_enable / trace_enabled / trace_clear_all methods on the
generated daemon class + an internal trace_filter_ map. The wire-side
NodeTraceCtl server (called from supervisor on Configure push) plugs
into these methods later (#361).

reporting=false nodes get neither — supervisor cannot push trace
config to them; matches AUTOSAR Non-Reporting semantics.

These tests run gen-app against a tiny .art fixture and inspect the
generated .hh string. C++ compile coverage lives downstream (Bazel
build of services/log[trace] once #355 lands the Tracer.hh hook).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from artheia.generators.fc_app import generate_fc


def _build_art(tmp_path: Path, body: str) -> Path:
    """Lay out a minimal package.art + (empty) component.art so
    gen-app can read it as a normal FC source."""
    pkg = tmp_path / "system" / "services" / "tfc"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.art").write_text(body)
    (pkg / "component.art").write_text(
        "package system.services.tfc\n"
        "composition TfcComp { prototype Worker w }\n"
    )
    return pkg / "package.art"


def _run_gen_app(tmp_path: Path, art: Path) -> Path:
    """Invoke generate_fc and return the lib/.hh path."""
    out_dir = art.parent  # gen-app writes lib/main/impl alongside .art
    manifest_dir = tmp_path / "manifest"
    proto_dir = tmp_path / "proto"
    manifest_dir.mkdir(exist_ok=True)
    proto_dir.mkdir(exist_ok=True)
    generate_fc(
        str(art), str(out_dir),
        manifest_out=str(manifest_dir),
        proto_out=str(proto_dir),
        cxx_namespace="ara::tfc",
        force=True,
    )
    # gen-app emits several headers into lib/: the Daemon class
    # (<Node>.hh — the one we want), a sibling <Node>_netgraph.hh
    # (static routing table), and Log.hh (per-FC log helper, #383).
    # Select the Daemon header by node name rather than by exclusion so
    # new sidecar headers don't break the count.
    lib_dir = out_dir / "lib"
    daemon = lib_dir / "Worker.hh"
    assert daemon.exists(), (
        f"expected Daemon header {daemon} in {sorted(lib_dir.glob('*.hh'))}"
    )
    return daemon


def test_reporting_true_emits_trace_api(tmp_path):
    """A reporting=true node (the default) gets trace_enable +
    trace_enabled + trace_clear_all + the filter map."""
    art = _build_art(tmp_path,
        """
        package system.services.tfc

        message Ping { }

        interface clientServer Ctl {
            operation Echo(in p:Ping) returns Ping
        }

        node atomic Worker {
            tipc type=0x80019999 instance=0
            ports {
                server ctl provides Ctl
            }
        }
        """
    )
    hh = _run_gen_app(tmp_path, art).read_text()
    assert "kReporting = true" in hh
    # The trace control API is emitted as inline in-class definitions
    # (signature + body), not decl-only — so match the open-brace form.
    assert "void trace_enable(const char* msg_type, bool enabled) {" in hh
    assert "bool trace_enabled(const char* msg_type) const {" in hh
    assert "void trace_clear_all() {" in hh
    assert "trace_filter_" in hh
    # Bodies delegate to the per-node tracer (the runtime sink #361).
    assert "::theia::runtime::tracer_for(kNodeName).trace_enable(" in hh


def test_reporting_false_omits_trace_api(tmp_path):
    """A reporting=false node has neither the methods nor the
    filter map. The kReporting constant reflects the opt-out."""
    art = _build_art(tmp_path,
        """
        package system.services.tfc

        message Ping { }

        interface clientServer Ctl {
            operation Echo(in p:Ping) returns Ping
        }

        node atomic Worker {
            tipc type=0x80019998 instance=0
            reporting=false
            ports {
                server ctl provides Ctl
            }
        }
        """
    )
    hh = _run_gen_app(tmp_path, art).read_text()
    assert "kReporting = false" in hh
    assert "void trace_enable" not in hh
    assert "trace_filter_" not in hh


def test_reporting_omitted_defaults_to_true(tmp_path):
    """Per #362 + model/inherit defaults: missing reporting field
    is reporting=true. gen-app should emit the trace API."""
    art = _build_art(tmp_path,
        """
        package system.services.tfc

        message Ping { }

        interface clientServer Ctl {
            operation Echo(in p:Ping) returns Ping
        }

        node atomic Worker {
            tipc type=0x80019997 instance=0
            ports {
                server ctl provides Ctl
            }
        }
        """
    )
    hh = _run_gen_app(tmp_path, art).read_text()
    assert "kReporting = true" in hh
    assert "void trace_enable(" in hh
