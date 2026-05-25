"""Node-RED module management + inject trigger. Ported from nr-mcp (MIT)."""

from .client import NRClient, NRError, NRNotFoundError


async def get_installed_modules(client: NRClient, query: str | None) -> dict:
    raw = await client.get_nodes()
    modules = []
    for mod in raw:
        name = mod.get("name", mod.get("id", "unknown"))
        version = mod.get("version", "?")
        types = mod.get("types", [])
        if not types and "nodes" in mod:
            types = []
            for n in mod["nodes"]:
                types.extend(n.get("types", []))
        if query:
            q = query.lower()
            if q not in name.lower() and not any(q in t.lower() for t in types):
                continue
        modules.append({"name": name, "version": version, "types": types, "enabled": mod.get("enabled", True)})
    return {"modules": modules, "total": len(modules), "query": query}


async def install_module(client: NRClient, module_name: str) -> dict:
    if not module_name or " " in module_name:
        raise NRError(f"Invalid module name: '{module_name}'")
    result = await client.install_module(module_name)
    return {"success": True, "module": module_name, "version": result.get("version", "unknown"),
            "types": result.get("types", []), "message": f"Module '{module_name}' installed successfully"}


async def inject_node(client: NRClient, node_id: str) -> dict:
    flows, _ = await client.get_flows()
    node = next((f for f in flows if f["id"] == node_id), None)
    if not node:
        raise NRNotFoundError(f"Node '{node_id}' not found")
    if node.get("type") != "inject":
        raise NRError(f"Node '{node_id}' is type '{node.get('type')}', not 'inject'")
    await client.inject(node_id)
    return {"success": True, "node_id": node_id, "node_name": node.get("name", ""), "message": "Inject triggered"}
