"""Structural, catalog-free validation of Node-RED flow node lists.

Pure functions (no I/O): given a node list, report errors (high-precision,
unambiguous breakages) and warnings (heuristic). See
docs/superpowers/specs/2026-05-26-validate-flow-design.md.
"""

_CONTAINER_TYPES = {"tab", "subflow"}        # valid targets for a node's `z`
_STRUCTURAL_TYPES = {"tab", "subflow", "group"}  # not real node "sets" -> skip type check


def _finding(code, node_id, message, focus_ids):
    f = {"code": code, "node_id": node_id, "message": message}
    if focus_ids and node_id in focus_ids:
        f["focus"] = True
    return f


def validate_flows(flows, *, focus_ids=None, registered_types=None):
    errors = []
    warnings = []
    ids = set()
    container_ids = set()

    # first pass: identity + container ids
    for n in flows:
        nid = n.get("id")
        if not nid:
            errors.append(_finding("missing_id", None,
                                   f"Node missing 'id' (type {n.get('type', '?')})", focus_ids))
            continue
        if nid in ids:
            errors.append(_finding("duplicate_id", nid, f"Duplicate node id '{nid}'", focus_ids))
        ids.add(nid)
        if n.get("type") in _CONTAINER_TYPES:
            container_ids.add(nid)

    # second pass: per-node checks
    for n in flows:
        nid = n.get("id")
        if not nid:
            continue
        ntype = n.get("type")
        if not ntype:
            errors.append(_finding("missing_type", nid, f"Node '{nid}' missing 'type'", focus_ids))
        z = n.get("z")
        if z and z not in container_ids:
            errors.append(_finding("bad_z", nid,
                                   f"Node '{nid}' has z='{z}', not an existing tab/subflow", focus_ids))
        for out in n.get("wires", []) or []:
            for target in out:
                if target not in ids:
                    errors.append(_finding("wire_to_missing", nid,
                                           f"Node '{nid}' wires to missing node '{target}'", focus_ids))
        if ntype == "group":
            for member in n.get("nodes", []) or []:
                if member not in ids:
                    errors.append(_finding("group_member_missing", nid,
                                           f"Group '{nid}' lists missing member '{member}'", focus_ids))
        # warnings (heuristic)
        if (registered_types is not None and ntype
                and ntype not in _STRUCTURAL_TYPES and not ntype.startswith("subflow:")
                and ntype not in registered_types):
            warnings.append(_finding("unregistered_type", nid,
                                     f"Node '{nid}' has unregistered type '{ntype}'", focus_ids))
        server = n.get("server")
        if isinstance(server, str) and server and server not in ids:
            warnings.append(_finding("server_ref_missing", nid,
                                     f"Node '{nid}' references missing server config '{server}'", focus_ids))

    return {"valid": not errors, "errors": errors, "warnings": warnings, "checked": len(flows)}


def _registered_types(nodes_payload) -> set:
    """Best-effort: union of node 'types' from the /nodes payload."""
    types = set()
    if isinstance(nodes_payload, list):
        for entry in nodes_payload:
            if isinstance(entry, dict):
                for t in entry.get("types", []) or []:
                    types.add(t)
    return types


async def validate_flow(client, *, nodes=None) -> dict:
    """Validate the current flows, or (current + `nodes`) when nodes are given."""
    flows, _ = await client.get_flows()
    if nodes:
        candidate = flows + nodes
        focus = {n["id"] for n in nodes if n.get("id")}
    else:
        candidate, focus = flows, None
    registered = _registered_types(await client.get_nodes())
    return validate_flows(candidate, focus_ids=focus, registered_types=registered or None)
