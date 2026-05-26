"""FastMCP server for node-red-homeassistant-mcp."""

import json

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .client import NRClient, NRError
from .backup import BackupStore
from . import flows, edit, modules, ha

settings = Settings()
client = NRClient(settings)
store = BackupStore(settings.backup_dir)

mcp = FastMCP(
    "node-red-homeassistant-mcp",
    json_response=True,
    host=settings.mcp_http_host,
    port=settings.mcp_http_port,
    stateless_http=True,
)


def _ok(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False)


def _err(msg: str) -> str:
    return _ok({"error": msg})


def _wrap(coro_factory):
    """Run a tool body, mapping NRError -> structured error JSON."""
    async def runner():
        try:
            return _ok(await coro_factory())
        except NRError as e:
            return _err(str(e))
        except Exception as e:  # noqa: BLE001 - surface unexpected errors to the client
            return _err(f"Unexpected error: {type(e).__name__}: {e}")
    return runner()


# ---- read-only ----
@mcp.tool()
async def nr_get_flow_summary() -> str:
    """Overview of all Node-RED tabs with node counts and groups."""
    return await _wrap(lambda: flows.get_flow_summary(client))


@mcp.tool()
async def nr_get_flow(name_or_id: str, fields: list[str] | None = None) -> str:
    """Get one tab with its nodes/groups. `fields` returns only selected node keys (compact)."""
    return await _wrap(lambda: flows.get_flow(client, name_or_id, fields=fields))


@mcp.tool()
async def nr_search_nodes(query: str, type: str | None = None, flow: str | None = None, max_results: int = 20) -> str:
    """Search nodes by name/type/function-code/template."""
    return await _wrap(lambda: flows.search_nodes(client, query, node_type=type, flow=flow, max_results=max_results))


@mcp.tool()
async def nr_get_function_code(node_id: str) -> str:
    """Get a function node's JavaScript code."""
    return await _wrap(lambda: flows.get_function_code(client, node_id))


@mcp.tool()
async def nr_get_node_config(node_id: str) -> str:
    """Full node config incl. upstream/downstream and group membership."""
    return await _wrap(lambda: flows.get_node_config(client, node_id))


@mcp.tool()
async def nr_get_flow_context(flow_id: str, key: str | None = None) -> str:
    """Read flow context (key=null lists keys)."""
    return await _wrap(lambda: flows.get_flow_context(client, flow_id, key))


@mcp.tool()
async def nr_get_global_context(key: str | None = None) -> str:
    """Read global context (key=null lists keys)."""
    return await _wrap(lambda: flows.get_global_context(client, key))


@mcp.tool()
async def nr_get_debug_output(flow_id: str, key: str | None = None) -> str:
    """Read debug data stored to flow context."""
    return await _wrap(lambda: flows.get_debug_output(client, flow_id, key))


# ---- mutating (confirm-gated) ----
@mcp.tool()
async def nr_safe_deploy(node_id: str, fields: dict, confirm: bool = False, description: str | None = None) -> str:
    """Patch one node's fields. In propose mode previews unless confirm=true. Backup-first."""
    return await _wrap(lambda: edit.safe_deploy(client, store, settings, node_id=node_id, fields=fields, confirm=confirm, description=description))


@mcp.tool()
async def nr_safe_deploy_many(updates: list[dict], confirm: bool = False, description: str | None = None) -> str:
    """Patch many nodes ([{node_id, fields}]) in one atomic deploy. Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.safe_deploy_many(client, store, settings, updates=updates, confirm=confirm, description=description))


@mcp.tool()
async def nr_add_wire(from_id: str, output_index: int, to_id: str, confirm: bool = False) -> str:
    """Idempotently add a wire from_id[output_index] -> to_id. Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.add_wire(client, store, settings, from_id=from_id, output_index=output_index, to_id=to_id, confirm=confirm))


@mcp.tool()
async def nr_create_nodes(nodes: list[dict], confirm: bool = False, description: str | None = None) -> str:
    """Create nodes/groups (each needs id,type and z for tab nodes). Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.create_nodes(client, store, settings, nodes=nodes, confirm=confirm, description=description))


@mcp.tool()
async def nr_delete_nodes(node_ids: list[str], confirm: bool = False, description: str | None = None) -> str:
    """Delete nodes by id (cleans group/wire refs). Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.delete_nodes(client, store, settings, node_ids=node_ids, confirm=confirm, description=description))


# ---- backups ----
@mcp.tool()
async def nr_backup_flow() -> str:
    """Manually snapshot the current flows to a backup file. Returns the backup path."""
    async def _do():
        flows, _ = await client.get_flows()
        path = store.snapshot(flows, "manual backup")
        return {"backup": str(path)}
    return await _wrap(lambda: _do())


@mcp.tool()
async def nr_list_backups() -> str:
    """List saved flow backups (path, timestamp, label)."""
    async def _do():
        return {"backups": store.list_backups()}
    return await _wrap(lambda: _do())


@mcp.tool()
async def nr_restore_backup(path: str, confirm: bool = False) -> str:
    """Restore flows from a backup file. Confirm-gated, backup-first (the current state is itself snapshotted before overwrite)."""
    return await _wrap(lambda: edit.restore_backup(client, store, settings, path=path, confirm=confirm))


# ---- modules / inject ----
@mcp.tool()
async def nr_get_installed_modules(query: str | None = None) -> str:
    """List installed Node-RED modules and their node types."""
    return await _wrap(lambda: modules.get_installed_modules(client, query))


@mcp.tool()
async def nr_install_module(module_name: str) -> str:
    """Install a Node-RED npm module (30-60s)."""
    return await _wrap(lambda: modules.install_module(client, module_name))


@mcp.tool()
async def nr_inject(node_id: str) -> str:
    """Trigger an inject node to fire."""
    return await _wrap(lambda: modules.inject_node(client, node_id))


# ---- HA awareness ----
@mcp.tool()
async def ha_list_ha_nodes() -> str:
    """List Home Assistant nodes in the flows with their domain/service/entity."""
    return await _wrap(lambda: ha.list_ha_nodes(client))


@mcp.tool()
async def ha_get_server_config() -> str:
    """List the Home Assistant server config nodes Node-RED uses."""
    return await _wrap(lambda: ha.get_server_config(client))


@mcp.tool()
async def ha_validate_calls() -> str:
    """Validate api-call-service nodes against live HA (requires HA_URL + HA_TOKEN)."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA validation needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.validate_calls(client, ha_client))
    finally:
        await ha_client.close()


def main():
    """Console-script entry point."""
    transport = "streamable-http" if settings.mcp_transport == "http" else "stdio"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
