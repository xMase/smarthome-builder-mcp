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
