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
