"""Read-only Node-RED inspection tools. Ported from nr-mcp (MIT) + PR #3."""

from .client import NRClient, NRError, NRNotFoundError


async def get_flow_summary(client: NRClient) -> dict:
    flows, _ = await client.get_flows()
    tabs = [f for f in flows if f.get("type") == "tab"]
    groups = [f for f in flows if f.get("type") == "group"]
    nodes = [f for f in flows if f.get("type") not in ("tab", "group", "subflow", "comment") and "z" in f]
    tab_map = {}
    for tab in tabs:
        tab_map[tab["id"]] = {"id": tab["id"], "label": tab.get("label", ""), "node_count": 0, "groups": []}
    for node in nodes:
        z = node.get("z", "")
        if z in tab_map:
            tab_map[z]["node_count"] += 1
    for group in groups:
        z = group.get("z", "")
        if z in tab_map:
            tab_map[z]["groups"].append({
                "id": group["id"], "label": group.get("name", group.get("label", "")),
                "node_count": len(group.get("nodes", [])),
            })
    return {"total_nodes": len(nodes), "total_tabs": len(tabs), "tabs": list(tab_map.values())}


async def get_flow(client: NRClient, name_or_id: str, fields: list[str] | None = None) -> dict:
    flows, _ = await client.get_flows()
    tabs = [f for f in flows if f.get("type") == "tab"]
    tab = None
    q = name_or_id.lower()
    for t in tabs:
        if t["id"] == name_or_id:
            tab = t
            break
    if not tab:
        matches = [t for t in tabs if q in t.get("label", "").lower()]
        if len(matches) == 1:
            tab = matches[0]
        elif len(matches) > 1:
            raise NRNotFoundError(f"Ambiguous tab name '{name_or_id}'. Matches: {[m.get('label') for m in matches]}")
        else:
            raise NRNotFoundError(f"Tab not found: '{name_or_id}'")
    tab_id = tab["id"]
    nodes = [f for f in flows if f.get("z") == tab_id and f.get("type") != "group"]
    groups = [f for f in flows if f.get("z") == tab_id and f.get("type") == "group"]
    if fields:
        keep = set(fields) | {"id", "type", "z"}
        nodes = [{k: v for k, v in n.items() if k in keep} for n in nodes]
    return {
        "tab": {"id": tab["id"], "label": tab.get("label", ""), "type": "tab", "info": tab.get("info", "")},
        "nodes": nodes,
        "groups": [{"id": g["id"], "label": g.get("name", ""), "nodes": g.get("nodes", [])} for g in groups],
        "total_nodes": len(nodes),
    }


async def search_nodes(client: NRClient, query: str, node_type: str | None = None,
                       flow: str | None = None, max_results: int = 20) -> dict:
    flows, _ = await client.get_flows()
    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    nodes = [f for f in flows if "z" in f and f.get("type") not in ("tab", "group")]
    q = query.lower()
    results = []
    for node in nodes:
        if node_type and node.get("type") != node_type:
            continue
        if flow:
            fl = flow.lower()
            z = node.get("z", "")
            if z != flow and fl not in tabs.get(z, "").lower():
                continue
        match_in = None
        snippet = ""
        for field in ("name", "type", "func", "template", "action", "data"):
            val = node.get(field, "")
            if isinstance(val, str) and q in val.lower():
                match_in, snippet = field, val[:200]
                break
        if match_in:
            results.append({
                "id": node["id"], "name": node.get("name", ""), "type": node.get("type", ""),
                "flow_id": node.get("z", ""), "flow_label": tabs.get(node.get("z", ""), ""),
                "match_in": match_in, "snippet": snippet,
            })
            if len(results) >= max_results:
                break
    return {"results": results, "total": len(results), "truncated": len(results) >= max_results}


async def get_function_code(client: NRClient, node_id: str) -> dict:
    flows, _ = await client.get_flows()
    node = next((f for f in flows if f.get("id") == node_id), None)
    if not node:
        raise NRNotFoundError(f"Node not found: {node_id}")
    if node.get("type") != "function":
        return {"error": f"Node {node_id} is type '{node.get('type')}', not 'function'"}
    return {
        "node_id": node_id, "name": node.get("name", ""), "code": node.get("func", ""),
        "outputs": node.get("outputs", 1), "initialize": node.get("initialize", ""),
        "finalize": node.get("finalize", ""),
    }


async def get_node_config(client: NRClient, node_id: str) -> dict:
    flows, _ = await client.get_flows()
    node = next((f for f in flows if f.get("id") == node_id), None)
    if not node:
        raise NRNotFoundError(f"Node not found: {node_id}")
    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    groups = [f for f in flows if f.get("type") == "group"]
    downstream = []
    for wl in node.get("wires", []):
        downstream.extend(wl)
    upstream = []
    for f in flows:
        for wl in f.get("wires", []):
            if node_id in wl:
                upstream.append(f["id"])
    group_label = None
    for g in groups:
        if node_id in g.get("nodes", []):
            group_label = g.get("name", g.get("label", ""))
            break
    result = dict(node)
    result["_flow_label"] = tabs.get(node.get("z", ""), "")
    result["_group_label"] = group_label
    result["_downstream"] = downstream
    result["_upstream"] = upstream
    return result


def _subflow_name(flows: list[dict], def_id: str) -> str:
    d = next((f for f in flows if f.get("id") == def_id and f.get("type") == "subflow"), None)
    return d.get("name", "") if d else ""


def _port_sources(port: dict) -> list[dict]:
    return [{"node_id": w["id"], "port": w.get("port", 0)} for w in port.get("wires", [])]


def _resolve_subflow(flows: list[dict], def_id: str) -> dict:
    sf = next((f for f in flows if f.get("id") == def_id and f.get("type") == "subflow"), None)
    if sf is None:
        raise NRNotFoundError(f"Subflow not found: '{def_id}'")
    labels = sf.get("outputLabels", [])
    outputs = []
    for i, port in enumerate(sf.get("out", [])):
        label = labels[i] if i < len(labels) and labels[i] else f"out {i}"
        outputs.append({"index": i, "label": label, "sources": _port_sources(port)})
    inputs = [{"index": i, "wires": [{"node_id": w["id"]} for w in port.get("wires", [])]}
              for i, port in enumerate(sf.get("in", []))]
    status = {"sources": _port_sources(sf["status"])} if sf.get("status") else None

    feeds_out: dict[str, list[int]] = {}
    for o in outputs:
        for s in o["sources"]:
            feeds_out.setdefault(s["node_id"], []).append(o["index"])
    status_ids = {s["node_id"] for s in (status["sources"] if status else [])}

    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    nodes, nested = [], []
    for n in flows:
        if n.get("z") != def_id:
            continue
        targets = []
        for wl in n.get("wires", []):
            targets.extend(wl)
        node = dict(n)
        node["_targets"] = targets
        node["_feeds_outputs"] = feeds_out.get(n["id"], [])
        node["_feeds_status"] = n["id"] in status_ids
        ntype = n.get("type", "")
        if ntype.startswith("subflow:"):
            nd = ntype.split(":", 1)[1]
            ptr = {"def_id": nd, "def_name": _subflow_name(flows, nd)}
            node["_nested_subflow"] = ptr
            nested.append({"instance_node_id": n["id"], **ptr})
        nodes.append(node)

    instances = []
    inst_type = f"subflow:{def_id}"
    for f in flows:
        if f.get("type") == inst_type:
            wired = [i for i, wl in enumerate(f.get("wires", [])) if wl]
            instances.append({"node_id": f["id"], "flow_id": f.get("z", ""),
                              "flow_label": tabs.get(f.get("z", ""), ""), "wired_outputs": wired})

    return {
        "subflow": {"id": sf["id"], "name": sf.get("name", ""),
                    "category": sf.get("category", ""), "color": sf.get("color", ""),
                    "info": sf.get("info", "")},
        "inputs": inputs, "outputs": outputs, "status": status, "env": sf.get("env", []),
        "nodes": nodes, "nested_subflows": nested, "instances": instances,
        "total_nodes": len(nodes),
    }


async def get_subflow(client: NRClient, name_or_id: str) -> dict:
    flows, _ = await client.get_flows()
    return _resolve_subflow(flows, name_or_id)


def _read_context(data: dict, *, scope: dict, key: str | None, not_found: str) -> dict:
    stores = {name: (c or {}) for name, c in data.items() if isinstance(c, dict)}
    if key is None:
        return {**scope, "stores": {name: list(c.keys()) for name, c in stores.items()}}
    order = (["default"] if "default" in stores else []) + [n for n in stores if n != "default"]
    for name in order:
        if key in stores[name]:
            return {**scope, "key": key, "store": name, "value": stores[name][key].get("msg")}
    raise NRNotFoundError(not_found)


async def get_flow_context(client: NRClient, flow_id: str, key: str | None = None) -> dict:
    data = await client.get_context(flow_id)
    return _read_context(data, scope={"flow_id": flow_id}, key=key,
                         not_found=f"Context key not found: '{key}'")


async def get_global_context(client: NRClient, key: str | None = None) -> dict:
    data = await client.get_global_context()
    return _read_context(data, scope={"scope": "global"}, key=key,
                         not_found=f"Global context key not found: '{key}'")

