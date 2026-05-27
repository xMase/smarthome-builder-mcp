"""Structure-aware subflow write tools, on the deploy gate (deploy.commit).

Each function builds a pure rebuild(flows) closure (re-run on optimistic-lock
conflict) and reuses the sub-project A read resolvers in flows.py.
"""

import copy
import secrets

from .client import NRError, NRNotFoundError  # noqa: F401  (NRNotFoundError raised via flows helpers)
from .deploy import commit
from .flows import _resolve_subflow, _find_subflow_def_id


def _new_node_id(existing: set[str]) -> str:
    nid = secrets.token_hex(8)                 # 16 hex chars, Node-RED style
    while nid in existing:
        nid = secrets.token_hex(8)
    return nid


async def instantiate_subflow(client, store, settings, *, subflow, z, name=None,
                              env=None, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)          # raises NRNotFoundError
    if not any(f.get("id") == z and f.get("type") == "tab" for f in flows):
        raise NRError(f"Target tab '{z}' not found")
    n_out = len(_resolve_subflow(flows, def_id)["outputs"])
    new_id = _new_node_id({f["id"] for f in flows})        # once, before commit
    node = {"id": new_id, "type": f"subflow:{def_id}", "z": z, "name": name or "",
            "env": env or [], "wires": [[] for _ in range(n_out)], "x": 100, "y": 100}

    def rebuild(fl):
        return copy.deepcopy(fl) + [copy.deepcopy(node)]

    result = await commit(client, store, settings, rebuild=rebuild,
                          summary=f"instantiate subflow {def_id} on {z} as {new_id}",
                          confirm=confirm)
    result["node_id"] = new_id
    return result


async def add_subflow_port(client, store, settings, *, subflow, label=None,
                           source=None, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)
    if source is not None:
        sid = source.get("node_id")
        if not any(f.get("id") == sid and f.get("z") == def_id for f in flows):
            raise NRError(f"source node '{sid}' is not an internal node of subflow '{def_id}'")

    def rebuild(fl):
        new = copy.deepcopy(fl)
        sf = next(f for f in new if f.get("id") == def_id and f.get("type") == "subflow")
        out = sf.setdefault("out", [])
        wires = [{"id": source["node_id"], "port": source.get("port", 0)}] if source else []
        out.append({"x": 600, "y": 40 + 60 * len(out), "wires": wires})
        labels = sf.setdefault("outputLabels", [])
        while len(labels) < len(out) - 1:        # pad any pre-existing missing labels
            labels.append("")
        labels.append(label if label is not None else f"out {len(out) - 1}")
        for f in new:
            if f.get("type") == f"subflow:{def_id}":
                f.setdefault("wires", []).append([])
        return new

    return await commit(client, store, settings, rebuild=rebuild,
                        summary=f"add output port to subflow {def_id}", confirm=confirm)


async def remove_subflow_port(client, store, settings, *, subflow, index, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)

    def rebuild(fl):
        new = copy.deepcopy(fl)
        sf = next(f for f in new if f.get("id") == def_id and f.get("type") == "subflow")
        out = sf.get("out", [])
        if not (0 <= index < len(out)):
            raise NRError(f"Output port {index} out of range (0..{len(out) - 1})")
        del out[index]
        labels = sf.get("outputLabels", [])
        if index < len(labels):
            del labels[index]
        for f in new:
            if f.get("type") == f"subflow:{def_id}":
                w = f.get("wires", [])
                if index < len(w):
                    del w[index]
        return new

    return await commit(client, store, settings, rebuild=rebuild,
                        summary=f"remove output port {index} from subflow {def_id}",
                        confirm=confirm)


async def delete_subflow(client, store, settings, *, subflow, cascade=False, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)

    def rebuild(fl):
        inst_type = f"subflow:{def_id}"
        instances = [f["id"] for f in fl if f.get("type") == inst_type]
        if instances and not cascade:
            raise NRError(f"Subflow '{def_id}' has {len(instances)} instance(s): "
                          f"{instances}. Pass cascade=true to delete them too, or "
                          f"remove them first.")
        remove = {def_id} | {f["id"] for f in fl if f.get("z") == def_id}
        if cascade:
            remove |= set(instances)
        new = [copy.deepcopy(f) for f in fl if f["id"] not in remove]
        for f in new:
            if f.get("type") == "group" and "nodes" in f:
                f["nodes"] = [n for n in f["nodes"] if n not in remove]
            if "wires" in f:
                f["wires"] = [[w for w in out if w not in remove] for out in f["wires"]]
        return new

    summary = f"delete subflow {def_id}" + (" (cascade)" if cascade else "")
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def create_subflow(client, store, settings, *, name, internal_nodes, outputs,
                         input_to=None, status_from=None, env=None, confirm=False):
    for n in internal_nodes:
        if not n.get("id") or not n.get("type"):
            raise NRError(f"internal node missing 'id' or 'type': {n}")
    internal_ids = [n["id"] for n in internal_nodes]
    if len(set(internal_ids)) != len(internal_ids):
        raise NRError(f"duplicate internal node ids: {internal_ids}")
    input_list = ([] if input_to is None
                  else [input_to] if isinstance(input_to, str) else list(input_to))

    flows, _ = await client.get_flows()
    def_id = _new_node_id({f["id"] for f in flows} | set(internal_ids))

    id_set = set(internal_ids)
    for o in outputs:
        if o.get("source", {}).get("node_id") not in id_set:
            raise NRError(f"output source '{o.get('source', {}).get('node_id')}' "
                          f"is not one of the internal nodes")
    for t in input_list:
        if t not in id_set:
            raise NRError(f"input_to '{t}' is not one of the internal nodes")
    if status_from is not None and status_from.get("node_id") not in id_set:
        raise NRError(f"status_from '{status_from.get('node_id')}' "
                      f"is not one of the internal nodes")

    def rebuild(fl):
        existing = {f["id"] for f in fl}
        clash = (set(internal_ids) | {def_id}) & existing
        if clash:
            raise NRError(f"node id(s) already exist: {sorted(clash)}")
        def_node = {
            "id": def_id, "type": "subflow", "name": name,
            "in": ([{"x": 60, "y": 60, "wires": [{"id": t} for t in input_list]}]
                   if input_list else []),
            "out": [{"x": 600, "y": 40 + 60 * i,
                     "wires": [{"id": o["source"]["node_id"],
                                "port": o["source"].get("port", 0)}]}
                    for i, o in enumerate(outputs)],
            "outputLabels": [o.get("label") or f"out {i}" for i, o in enumerate(outputs)],
            "env": env or [], "color": "#DDAA99",
        }
        if status_from is not None:
            def_node["status"] = {"x": 600, "y": 40 + 60 * len(outputs),
                                  "wires": [{"id": status_from["node_id"],
                                             "port": status_from.get("port", 0)}]}
        internals = []
        for n in internal_nodes:
            m = copy.deepcopy(n)
            m["z"] = def_id
            internals.append(m)
        return copy.deepcopy(fl) + [def_node] + internals

    result = await commit(client, store, settings, rebuild=rebuild,
                          summary=f"create subflow {def_id} ({name})", confirm=confirm)
    result["subflow_id"] = def_id
    return result
