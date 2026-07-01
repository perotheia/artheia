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


def _node_infos(models: list, comp: str) -> list:
    """Per-node supervisor metadata for composition *comp*: one dict per
    prototyped node, ``{name, reporting, tipc_type, tipc_instance}``.

    The C++ supervisor needs this in executor.json (load_worker → NodeInfo) to
    synthesise node_sup rows, decide which nodes to watchdog (reporting=true),
    and locate each node's trace-control TIPC server. The orthogonal
    DeploymentLayer is .art-free, so we resolve it HERE (gen-manifest has the
    model) and stash it in a PROCESS_NODES sidecar dict the serializer reads.

    *models* is the entry model PLUS every import-reachable model — a services
    cluster.art only forward-declares the per-FC compositions; their real
    bodies (with node prototypes + tipc) live in the imported component.art, so
    we search the whole set and use the FIRST composition body that carries
    prototypes. tipc type/instance pass through as the .art wrote them (hex or
    decimal; the supervisor get_str()s them). ``reporting`` mirrors fc_app.py's
    rule (default true; ``reporting = false`` opts out)."""
    for model in models:
        for el in getattr(model, "elements", []):
            if type(el).__name__ != "CompositionDecl" or el.name != comp:
                continue
            infos = []
            for p in getattr(el, "elements", []):
                if type(p).__name__ != "PrototypeDecl":
                    continue
                node = getattr(p, "type", None)
                if node is None:
                    continue
                tipc = getattr(node, "tipc", None)
                reporting_raw = (getattr(node, "reporting", "") or "true").lower()
                infos.append({
                    "name": p.name,
                    "reporting": reporting_raw == "true",
                    "tipc_type": str(getattr(tipc, "type", "")) if tipc else "",
                    "tipc_instance": str(getattr(tipc, "instance", "0"))
                                     if tipc else "0",
                })
            # The forward-decl stub has no prototypes; keep looking for the real
            # body in an imported model. Return as soon as we find a non-empty.
            if infos:
                return infos
    return []


def _modules_for(target: str) -> list:
    """The ChildSpec ``modules`` list (informational source path) from a bazel
    label: ``//services/sm/main:sm`` → ``["services/sm"]``,
    ``//apps/Demo3WayP1/main:apps`` → ``["apps/Demo3WayP1"]``."""
    lbl = target.lstrip("/")
    pkg = lbl.split(":", 1)[0]
    # drop the trailing /main (the bazel package holding the cc_binary).
    if pkg.endswith("/main"):
        pkg = pkg[: -len("/main")]
    return [pkg] if pkg else []


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


def _params_for_comp(models: list, comp: str, pkg: str) -> dict:
    """Return the gen-params dict for composition *comp* (the params{} defaults
    for each prototype node it hosts), derived from the first model in *models*
    that defines *comp* with a non-empty body.

    Mirrors :func:`build_params` from params_config.py but scoped to ONE
    composition so a multi-composition package produces a SEPARATE params dict
    per process ident rather than one merged blob."""
    from artheia.model import flatten_composition
    from artheia.generators.etcd_schema import _coerce_default

    for model in models:
        for el in getattr(model, "elements", []):
            if type(el).__name__ != "CompositionDecl" or el.name != comp:
                continue
            proto_decls, _ = flatten_composition(el)
            if not proto_decls:
                continue
            nodes: dict = {}
            const: dict = {}
            for proto in proto_decls:
                node_type = proto.type
                params = getattr(node_type, "params", None) or []
                if not params:
                    continue
                nodes[proto.name] = {p.name: _coerce_default(p) for p in params}
                ro = [p.name for p in params if getattr(p, "is_const", False)]
                if ro:
                    const[proto.name] = ro
            model_pkg = getattr(model, "name", None) or pkg
            out: dict = {"package": model_pkg, "nodes": nodes}
            if const:
                out["const"] = const
            return out
    return {"package": pkg, "nodes": {}}


def _config_defaults_for_comp(models: list, comp: str, pkg: str) -> dict:
    """Return the gen-config-defaults dict for composition *comp* (the etcd
    config defaults: config_type + digest + declared field values per prototype
    node), scoped to ONE composition from the first model that defines it.

    Mirrors :func:`build_config_defaults` from config_defaults.py but per-
    composition so a multi-composition package produces separate dicts."""
    from artheia.model import flatten_composition
    from artheia.generators.config_schema import _field_shape, _digest
    from artheia.generators.proto import _proto_package_name

    for model in models:
        for el in getattr(model, "elements", []):
            if type(el).__name__ != "CompositionDecl" or el.name != comp:
                continue
            proto_decls, _ = flatten_composition(el)
            if not proto_decls:
                continue
            configs: dict = {}
            for proto in proto_decls:
                node_type = proto.type
                cfg = getattr(node_type, "config", None)
                if cfg is None:
                    continue
                cfg_name = getattr(cfg, "name", None)
                if not cfg_name:
                    continue
                fields = _field_shape(cfg)
                values = {f["name"]: f["default"] for f in fields if "default" in f}
                try:
                    from textx import get_model
                    art_pkg = get_model(cfg).name or (model.name or "")
                except Exception:
                    art_pkg = model.name or ""
                flat = _proto_package_name(art_pkg).replace(".", "_") + "_" + cfg_name
                configs[proto.name] = {
                    "config_type": cfg_name,
                    "art_package": art_pkg,
                    "proto_type": flat,
                    "digest": _digest(cfg_name, fields),
                    "values": values,
                }
            model_pkg = getattr(model, "name", None) or pkg
            return {"package": model_pkg, "configs": configs}
    return {"package": pkg, "configs": {}}


def _render_process_nodes(process_nodes: dict) -> str:
    """Render the PROCESS_NODES sidecar dict: process name → its worker-spec
    detail (modules + per-node tipc/reporting) for the C++ supervisor's
    executor.json. serialize-manifest reads this off the manifest module and
    folds it into each leaf ChildSpec; the orthogonal DeploymentLayer stays
    .art-free."""
    import pprint
    body = pprint.pformat(process_nodes, indent=4, sort_dicts=True, width=88)
    out = [
        "\n\n# Per-process supervisor metadata (modules + nodes) resolved from\n"
        "# the .art at gen-manifest time. serialize-manifest folds this into the\n"
        "# executor.json worker leaves. DeploymentLayer stays transport-free.\n",
        f"PROCESS_NODES = {body}\n",
    ]
    return "".join(out)


def _render_process_params(process_params: dict) -> str:
    """Render the PROCESS_PARAMS sidecar: process name → gen-params JSON dict
    (the params{} defaults declared in the .art, one section per node keyed by
    prototype name). serialize-manifest writes this as config/<fc>.json per
    machine, deep-merging deploy/config/<machine>/<fc>.json on top."""
    import pprint
    body = pprint.pformat(process_params, indent=4, sort_dicts=True, width=88)
    out = [
        "\n\n# Per-process static params defaults derived from the .art at\n"
        "# gen-manifest time. serialize-manifest emits config/<fc>.json per\n"
        "# machine from this — no .art backtrack needed at install/deploy time.\n",
        f"PROCESS_PARAMS = {body}\n",
    ]
    return "".join(out)


def _render_process_config_defaults(process_config_defaults: dict) -> str:
    """Render the PROCESS_CONFIG_DEFAULTS sidecar: process name → gen-config-
    defaults dict (the config{} etcd-seed defaults per prototype node).
    serialize-manifest uses this for the first-boot etcd seed."""
    import pprint
    body = pprint.pformat(process_config_defaults, indent=4, sort_dicts=True, width=88)
    out = [
        "\n\n# Per-process etcd config-defaults derived from the .art at\n"
        "# gen-manifest time. serialize-manifest emits config-defaults.json per\n"
        "# machine for the first-boot etcd seed (migration/seed.py).\n",
        f"PROCESS_CONFIG_DEFAULTS = {body}\n",
    ]
    return "".join(out)


def _render_manifest(source: str, processes, services, app_name: str,
                     proc_names, process_nodes: dict,
                     process_params: dict | None = None,
                     process_config_defaults: dict | None = None) -> str:
    out = [_MANIFEST_HEADER.format(source=source)]

    # --- execution axis ---------------------------------------------------
    # processes is a SET. A bare `{}` is an empty DICT, not a set, so an empty
    # process list (e.g. an app package with zero compositions — the freshly
    # `theia init`'d workspace's bootstrap apps) must emit `set()`. Otherwise
    # combine()/mappend_set hits `set | dict` (TypeError) the moment this layer is
    # combined with a non-empty one. (Same rule as the application axis below.)
    if processes:
        out.append("    execution=ExecutionLayer(processes={\n")
        for ident, comp, base_dir, leaf, fg in processes:
            out.append(_render_process(ident, comp, base_dir, leaf, fg))
        out.append("    }),\n")
    else:
        out.append("    execution=ExecutionLayer(processes=set()),\n")

    # --- service axis (best-effort, from provided ports) ------------------
    if services:
        out.append("    service=ServiceLayer(instances={\n")
        for svc, fqn, inst, endpoint, provided_by in services:
            out.append(_render_service(svc, fqn, inst, endpoint, provided_by))
        out.append("    }),\n")

    # --- application axis: one AA bundling every process, host open -------
    # ApplicationLayer.processes is a SET of process names. A bare `{}` is an
    # empty DICT (not a set), so an empty composition (e.g. the bootstrap apps
    # cluster) must emit `set()` — otherwise simplify() carries a dict into
    # ApplicationTarget.processes (declared frozenset) and the frozen target's
    # hash blows up with "unhashable type: 'dict'".
    procs_lit = ", ".join(repr(n) for n in proc_names)
    procs_expr = "{" + procs_lit + "}" if proc_names else "set()"
    out.append("    applications=ApplicationSetLayer(applications={\n")
    out.append(
        f"        # one AA bundling every process; host bound by the variant.\n"
        f"        ApplicationLayer(name={app_name!r}, "
        f"processes={procs_expr}),\n"
    )
    out.append("    }),\n")

    out.append(")\n")
    out.append(_render_process_nodes(process_nodes))
    if process_params is not None:
        out.append(_render_process_params(process_params))
    if process_config_defaults is not None:
        out.append(_render_process_config_defaults(process_config_defaults))
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
    # The per-FC composition BODIES (node prototypes + tipc) live in the files
    # cluster.art imports — follow them so PROCESS_NODES can resolve services
    # nodes, not just same-file app nodes. Best-effort: a resolution failure
    # leaves nodes empty rather than aborting the manifest.
    from artheia.cli import _collect_imported_models
    try:
        models = [model] + [m for _p, m in _collect_imported_models(art_file, model)]
    except Exception:
        models = [model]
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
    process_nodes: dict = {}   # ident -> {"modules": [...], "nodes": [...]}
    # ident -> gen-params dict (params{} defaults). Built at gen-manifest time
    # so serialize-manifest can emit config/<fc>.json without any .art backtrack.
    process_params: dict = {}
    # ident -> gen-config-defaults dict (etcd config seed defaults).
    process_config_defaults: dict = {}
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
            process_nodes[ident] = {
                "modules": _modules_for(target),
                "nodes": _node_infos(models, comp),
            }
            # Build params from the model that defines this composition. The
            # merged `models` list includes imported models so services nodes
            # resolve just as app nodes do. build_params walks ALL compositions
            # in the model — scope to the one matching `comp` by building a
            # mini-model view: find the right model then filter.
            process_params[ident] = _params_for_comp(models, comp, pkg)
            process_config_defaults[ident] = _config_defaults_for_comp(
                models, comp, pkg)
        all_services.extend(_provided_services(model, members, pkg))

    app_name = (pkg.split(".")[-1] if pkg else default_base) or "app"
    rendered = _render_manifest(
        art_file, processes, all_services, app_name, proc_names,
        process_nodes, process_params, process_config_defaults)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)

    # Sidecar: executor.py — hand-editable supervisor tree.
    #
    # Write-once for HAND-EDITS (restart strategies, grouping) once the tree has
    # real children. BUT the .art-derived child set must never go stale: a fresh
    # workspace's FIRST gen-manifest runs on the EMPTY scaffold (no processes →
    # an executor with no `_sup` groups), and the old strict write-once then kept
    # that empty tree forever — so after adding the first app, executor.json
    # serialized to `null` and the supervisor FATAL'd ("manifest root must have
    # 'children'"). The user had to know to pass --force.
    #
    # Hands-off fix: refresh the sidecar when it's absent, --force'd, OR it's
    # still in the EMPTY-scaffold state (no `_sup` group yet) while the .art now
    # declares processes. A genuinely hand-edited tree (has `_sup` groups) is
    # left untouched.
    sidecar = out.parent / "executor.py"
    refresh = force or not sidecar.exists()
    if not refresh and fg_to_procs:
        # Look at the SUPERVISORS list body only (not the docstring, which
        # mentions "<function_group>_sup"). An empty-scaffold executor has just
        # the `root` node with `children=[]` and NO `name="<fg>_sup"` child —
        # detect that exact shape and refresh it. A hand-edited tree (any
        # name="…_sup") is left alone.
        existing = sidecar.read_text(errors="ignore")
        body = existing.split("SUPERVISORS", 1)[-1]
        if 'name="' not in body or '_sup"' not in body:
            refresh = True
            print("refreshing empty executor.py (.art now declares processes)")
    if refresh:
        sidecar.write_text(_render_executor(art_file, fg_to_procs))
    else:
        print("keep existing executor.py")

    # Package importability.
    init = out.parent / "__init__.py"
    if not init.exists():
        init.write_text("")
    return out
