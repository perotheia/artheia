"""Generate a NEW-ENGINE deployment manifest from a system ``.art``.

This is the orthogonal-ARA successor to the legacy manifest-proto path. Given an
``.art`` subtree (a ``system.art`` / ``component.art`` declaring clusters +
compositions + nodes) it emits TWO sibling files:

  * ``manifest.py`` — a Python module defining ``DEPLOYMENT =
    DeploymentLayer(...)`` built on :mod:`artheia.manifest.deployment`. The
    ``.art`` maps primarily onto the EXECUTION axis (one ``ProcessLayer`` per
    cluster member) and, best-effort, onto the SERVICE axis (one
    ``ServiceInstanceLayer`` per *provided* interface a member's nodes offer).
    Machines are intentionally left open (the deploy variant binds them);
    one ``ApplicationLayer`` bundles every process with its host_machine open.
    The emitted module is INLINE + LITERAL — the process / service rows ARE the
    table, no helpers, no pre-defined generators — mirroring the hand-authored
    examples in ``manifest/demo/base.py`` / ``manifest/services/base.py``.

  * ``executor.py`` — a hand-editable supervisor-tree sidecar. WRITE-ONCE: only
    written if absent or ``--force`` is passed (mirrors ``gen-app``'s impl/
    write-once rule). The tree is derived from DEPLOYMENT: a root
    ``one_for_one`` with one ``<function_group>_sup`` per function group, each
    parenting its processes.

The base_dir / cluster-member derivation lives in :mod:`_art_clusters`
(``_cluster_members`` + ``_base_dir_for`` + ``app_bazel_target``) so the
bazel-target prefix is right (``apps`` for ``system.apps``, ``services`` for
``system.services``).
"""

from __future__ import annotations

from pathlib import Path

from artheia.model import parse_file

# Reuse the .art → (cluster, base_dir, pkg_cluster, members) derivation and the
# bazel-target helper — the prefix logic is identical and battle-tested.
from artheia.generators._art_clusters import _cluster_members, app_bazel_target


# ---------------------------------------------------------------------------
# Service-axis extraction — best-effort provided-interface discovery.
# ---------------------------------------------------------------------------
#
# A node *provides* an interface when it owns a SERVER port (clientServer
# provider) or a SENDER port (senderReceiver provider). The hosting process is
# the cluster member ``ident``. We walk each member's composition prototypes to
# their resolved NodeDecls and collect (interface_fqn, instance_id) pairs.
#
# LIMITATION: instance ids / transport endpoints are NOT in the .art at the
# manifest level, so we synthesize a stable instance_id and leave the endpoint
# to the variant (binding/endpoint default in the deployment model). A node's
# tipc address (when present) seeds the endpoint as a hint.

_PROVIDER_PORTS = {"ServerPort", "SenderPort"}


def _iface_fqn(iface, pkg: str) -> str:
    """A best-effort fully-qualified name for an interface decl."""
    name = getattr(iface, "name", None) or "Unknown"
    return f"{pkg}.{name}" if pkg else name


def _tipc_endpoint(node) -> "str | None":
    """A ``tipc://<type>:<instance>`` endpoint hint from a node's tipc addr."""
    tipc = getattr(node, "tipc", None)
    if tipc is None:
        return None
    t = getattr(tipc, "type", None)
    inst = getattr(tipc, "instance", 0)
    if t is None:
        return None
    try:
        return f"tipc://{int(t):#x}:{int(inst)}"
    except (TypeError, ValueError):
        return None


def _provided_services(model, members, pkg: str):
    """Return ``[(service_name, interface_fqn, instance_id, endpoint, ident)]``
    for every interface PROVIDED by a node hosted in a member's composition.

    *members* is ``[(ident, composition, [node names]), ...]`` from
    ``_cluster_members``; the node names index the model's CompositionDecls so
    we can resolve each prototype to its NodeDecl and read its provider ports."""
    # Index composition -> {prototype node-name: NodeDecl} via the model.
    comp_nodes: dict[str, dict] = {}
    for el in getattr(model, "elements", []):
        if type(el).__name__ != "CompositionDecl":
            continue
        protos = {}
        for p in getattr(el, "elements", []):
            if type(p).__name__ == "PrototypeDecl":
                protos[getattr(p, "name", None)] = getattr(p, "type", None)
        comp_nodes[el.name] = protos

    out = []
    seen: set[tuple] = set()
    for ident, comp, _nodes in members:
        protos = comp_nodes.get(comp, {})
        for proto_name, node in protos.items():
            if node is None:
                continue
            for port in getattr(node, "ports", []) or []:
                if type(port).__name__ not in _PROVIDER_PORTS:
                    continue
                iface = getattr(port, "iface", None)
                if iface is None:
                    continue
                fqn = _iface_fqn(iface, pkg)
                iface_name = getattr(iface, "name", "Unknown")
                key = (ident, fqn)
                if key in seen:
                    continue
                seen.add(key)
                # service name: <process>_<interface> lower-cased + safe.
                svc = f"{ident}_{iface_name}".lower()
                out.append((
                    svc, fqn, len(out) + 1, _tipc_endpoint(node), ident,
                ))
    return out


# ---------------------------------------------------------------------------
# manifest.py rendering — inline + literal DeploymentLayer.
# ---------------------------------------------------------------------------

_MANIFEST_HEADER = '''\
"""AUTO-GENERATED from {source}, DO NOT EDIT (regen via gen-manifest).

A base :class:`DeploymentLayer` on the orthogonal-ARA engine
(:mod:`artheia.manifest.deployment`). Each cluster member maps to one
EXECUTION-axis process; provided interfaces map to SERVICE-axis instances.

``machine`` is intentionally LEFT OPEN on every process: this is a BASE
manifest — a deploy variant binds each process to a machine (see
``manifest/demo/single.py`` for the override idiom). ``validate()`` of THIS
base therefore reports ``machine`` Undefined; that is expected — the variant
makes it consistent.

Authoring style: inline + literal. The process / service rows ARE the table.
"""
from __future__ import annotations

from artheia.manifest.algebra import Default, Explicit
from artheia.manifest.deployment import (
    ApplicationLayer,
    ApplicationSetLayer,
    DeploymentLayer,
    ExecutionLayer,
    ProcessLayer,
    ServiceInstanceLayer,
    ServiceLayer,
)

DEPLOYMENT = DeploymentLayer(
'''


def _render_process(ident: str, comp: str, base_dir: str, target: str,
                    fg: str) -> str:
    return (
        f"        ProcessLayer(\n"
        f"            name={ident!r}, executable=Explicit({target!r}),\n"
        f"            start_cmd=Explicit({f'bin/{ident}'!r}), "
        f"function_group=Explicit({fg!r}),\n"
        f'            fg_states={{"Startup", "Running"}},\n'
        f"        ),\n"
    )


def _render_service(svc: str, fqn: str, inst: int, endpoint: "str | None",
                    provided_by: str) -> str:
    ep = (f" endpoint=Explicit({endpoint!r}),"
          if endpoint else "")
    return (
        f"        ServiceInstanceLayer(\n"
        f"            name={svc!r}, interface=Explicit({fqn!r}),\n"
        f"            instance_id=Explicit({inst}),{ep}\n"
        f"            provided_by=Explicit({provided_by!r}),\n"
        f"        ),\n"
    )


def _render_manifest(source: str, processes, services, app_name: str,
                     proc_names) -> str:
    out = [_MANIFEST_HEADER.format(source=source)]

    # --- execution axis ---------------------------------------------------
    out.append("    execution=ExecutionLayer(processes={\n")
    for ident, comp, base_dir, leaf, fg in processes:
        out.append(_render_process(ident, comp, base_dir, leaf, fg))
    out.append("    }),\n")

    # --- service axis (best-effort, from provided ports) ------------------
    if services:
        out.append("    service=ServiceLayer(instances={\n")
        for svc, fqn, inst, endpoint, provided_by in services:
            out.append(_render_service(svc, fqn, inst, endpoint, provided_by))
        out.append("    }),\n")

    # --- application axis: one AA bundling every process, host open -------
    procs_lit = ", ".join(repr(n) for n in proc_names)
    out.append("    applications=ApplicationSetLayer(applications={\n")
    out.append(
        f"        # one AA bundling every process; host bound by the variant.\n"
        f"        ApplicationLayer(name={app_name!r}, "
        f"processes={{{procs_lit}}}),\n"
    )
    out.append("    }),\n")

    out.append(")\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# executor.py sidecar — derived supervisor tree (write-once).
# ---------------------------------------------------------------------------

_EXECUTOR_HEADER = '''\
"""Supervisor tree for {source} — hand-editable.

Regenerate only with --force. gen-manifest derives an initial tree from the
DEPLOYMENT: a ``root`` supervisor (one_for_all) whose children are one
``<function_group>_sup`` per function group, each (one_for_one) parenting its
processes BY NAME. ``SupervisorNode.children`` is a list of names; leaves
resolve to the matching process at build time. Once written this file is YOURS
to edit (restart strategies, grouping) — a plain ``gen-manifest`` run keeps it
untouched.
"""
from __future__ import annotations

from artheia.manifest.supervisor import RestartStrategy, SupervisorNode

SUPERVISORS: list[SupervisorNode] = [
    SupervisorNode(
        name="root",
        strategy=RestartStrategy.ONE_FOR_ALL,
        children=[{roots}],
    ),
{groups}]
'''


def _render_executor(source: str, fg_to_procs: "dict[str, list[str]]") -> str:
    root_children = ", ".join(f'"{fg}_sup"' for fg in sorted(fg_to_procs))
    groups = []
    for fg, procs in sorted(fg_to_procs.items()):
        children = ", ".join(repr(p) for p in sorted(procs))
        groups.append(
            f"    SupervisorNode(\n"
            f'        name="{fg}_sup",\n'
            f"        strategy=RestartStrategy.ONE_FOR_ONE,\n"
            f"        children=[{children}],\n"
            f"    ),\n"
        )
    return _EXECUTOR_HEADER.format(
        source=source, roots=root_children, groups="".join(groups))


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def generate_manifest(art_file: str, out_file: str, force: bool = False) -> Path:
    """Render ``manifest.py`` (a base :class:`DeploymentLayer`) from
    *art_file*'s clusters and write it to *out_file*; ALSO emit the sibling
    ``executor.py`` supervisor sidecar (write-once unless *force*). Ensures the
    package ``__init__.py`` exists. Returns the ``manifest.py`` path."""
    out = Path(out_file)

    clusters = _cluster_members(art_file)
    model = parse_file(art_file)
    pkg = ""
    for line in Path(art_file).read_text().splitlines():
        s = line.strip()
        if s.startswith("package "):
            pkg = s[len("package "):].split("//")[0].strip()
            break

    # base_dir for inline members (clusters with base_dir="") = the output
    # module's source-tree dir, same convention as _art_clusters.
    parent = out.parent
    default_base = parent.parent.name if parent.name == "manifest" else parent.name

    processes = []          # (ident, composition, base_dir, leaf, fg)
    fg_to_procs: dict[str, list[str]] = {}
    proc_names: list[str] = []
    all_services = []
    for cluster_name, cluster_base_dir, pkg_cluster, members in clusters:
        # The bazel-target prefix is the SOURCE-TREE root the members hang off:
        # the .art PACKAGE cluster (services / apps) when known, else the
        # resolved base_dir, else the output module's dir. (pkg_cluster lines up
        # with the hand-authored manifest/{services,demo}/base.py targets —
        # //services/com/main:com, //apps/Demo3WayP1/main:apps.)
        bdir = pkg_cluster or cluster_base_dir or default_base
        fg = cluster_name.lower() if cluster_name else "app"
        for ident, comp, _nodes in members:
            # Canonical bazel target — handles the services-vs-apps split:
            #   services FCs → //services/<ident>/main:<ident>
            #   app members  → //<bdir>/<Comp>/main:<pkg_cluster or bdir>
            target = app_bazel_target(bdir, ident, comp, pkg_cluster)
            processes.append((ident, comp, bdir, target, fg))
            proc_names.append(ident)
            fg_to_procs.setdefault(fg, []).append(ident)
        all_services.extend(_provided_services(model, members, pkg))

    app_name = (pkg.split(".")[-1] if pkg else default_base) or "app"
    rendered = _render_manifest(
        art_file, processes, all_services, app_name, proc_names)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)

    # Sidecar: executor.py — write-once unless --force.
    sidecar = out.parent / "executor.py"
    if force or not sidecar.exists():
        sidecar.write_text(_render_executor(art_file, fg_to_procs))
    else:
        print("keep existing executor.py")

    # Package importability.
    init = out.parent / "__init__.py"
    if not init.exists():
        init.write_text("")
    return out
