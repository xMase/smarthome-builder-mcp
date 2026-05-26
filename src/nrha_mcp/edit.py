"""Mutating Node-RED tools, refactored onto the deploy gate (deploy.commit).

Validation/mutation logic ported from nr-mcp (MIT) + PR #3, restructured into pure
rebuild(flows) closures so it re-runs on optimistic-lock conflict retry.
"""

import copy

from .client import NRError, NRNotFoundError
from .deploy import commit

IMMUTABLE = {"id", "type", "z"}


async def safe_deploy(client, store, settings, *, node_id, fields, confirm=False, description=None):
    bad = set(fields) & IMMUTABLE
    if bad:
        raise NRError(f"Cannot modify immutable fields: {bad}")

    def rebuild(flows):
        new = copy.deepcopy(flows)
        idx = next((i for i, f in enumerate(new) if f.get("id") == node_id), None)
        if idx is None:
            raise NRNotFoundError(f"Node not found: {node_id}")
        new[idx].update(fields)
        return new

    summary = description or f"patch {node_id}: {list(fields)}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def safe_deploy_many(client, store, settings, *, updates, confirm=False, description=None):
    for u in updates:
        if "node_id" not in u or "fields" not in u:
            raise NRError(f"Each update needs 'node_id' and 'fields': {u}")
        bad = set(u.get("fields", {})) & IMMUTABLE
        if bad:
            raise NRError(f"Cannot modify immutable fields {bad} on '{u['node_id']}'")

    def rebuild(flows):
        new = copy.deepcopy(flows)
        index = {f["id"]: i for i, f in enumerate(new)}
        missing = [u["node_id"] for u in updates if u["node_id"] not in index]
        if missing:
            raise NRNotFoundError(f"Nodes not found: {missing}")
        for u in updates:
            new[index[u["node_id"]]].update(u["fields"])
        return new

    summary = description or f"patch {len(updates)} nodes"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def add_wire(client, store, settings, *, from_id, output_index, to_id, confirm=False):
    # Pre-check idempotency against current state (cheap, avoids a needless deploy)
    flows, _ = await client.get_flows()
    ids = {f["id"] for f in flows}
    if from_id not in ids:
        raise NRNotFoundError(f"Source node not found: '{from_id}'")
    if to_id not in ids:
        raise NRNotFoundError(f"Target node not found: '{to_id}'")
    src = next(f for f in flows if f["id"] == from_id)
    cur = src.get("wires", [])
    if len(cur) > output_index and to_id in cur[output_index]:
        return {"deployed": False, "already_existed": True,
                "message": f"Wire already exists: {from_id}[{output_index}] -> {to_id}"}

    def rebuild(fl):
        new = copy.deepcopy(fl)
        idx = next(i for i, f in enumerate(new) if f["id"] == from_id)
        wires = [list(w) for w in new[idx].get("wires", [])]
        while len(wires) <= output_index:
            wires.append([])
        if to_id not in wires[output_index]:
            wires[output_index].append(to_id)
        new[idx]["wires"] = wires
        return new

    summary = f"wire {from_id}[{output_index}] -> {to_id}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def create_nodes(client, store, settings, *, nodes, confirm=False, description=None):
    for n in nodes:
        if not n.get("id") or not n.get("type"):
            raise NRError(f"Node missing required 'id' or 'type': {n}")
        if "z" not in n and n["type"] not in ("tab", "subflow"):
            raise NRError(f"Node '{n['id']}' missing 'z' (target tab ID)")

    def rebuild(flows):
        # tabs created in THIS call are valid targets for nodes in the same batch
        new_tab_ids = {n["id"] for n in nodes if n.get("type") == "tab"}
        tab_ids = {f["id"] for f in flows if f.get("type") == "tab"} | new_tab_ids
        existing = {f["id"] for f in flows}
        for n in nodes:
            z = n.get("z", "")
            if z and z not in tab_ids and n["type"] not in ("tab", "subflow"):
                raise NRError(f"Target tab '{z}' not found for node '{n['id']}'")
            if n["id"] in existing:
                raise NRError(f"Node ID '{n['id']}' already exists")
        return copy.deepcopy(flows) + copy.deepcopy(nodes)

    summary = description or f"create {len(nodes)} node(s): {[n['id'] for n in nodes]}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def restore_backup(client, store, settings, *, path, confirm=False):
    restored = store.restore(path)

    def rebuild(flows):
        return copy.deepcopy(restored)

    return await commit(client, store, settings, rebuild=rebuild,
                        summary=f"restore backup {path}", confirm=confirm)


async def delete_nodes(client, store, settings, *, node_ids, confirm=False, description=None):
    ids = set(node_ids)

    def rebuild(flows):
        existing = {f["id"] for f in flows}
        missing = ids - existing
        if missing:
            raise NRError(f"Nodes not found: {missing}")
        for f in flows:
            if f["id"] in ids and f.get("type") == "tab":
                raise NRError(f"Refusing to delete tab '{f['id']}'. Remove all nodes first.")
        new = [copy.deepcopy(f) for f in flows if f["id"] not in ids]
        for f in new:
            if f.get("type") == "group" and "nodes" in f:
                f["nodes"] = [nid for nid in f["nodes"] if nid not in ids]
            if "wires" in f:
                f["wires"] = [[w for w in out if w not in ids] for out in f["wires"]]
        return new

    summary = description or f"delete {len(node_ids)} node(s): {node_ids}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)
