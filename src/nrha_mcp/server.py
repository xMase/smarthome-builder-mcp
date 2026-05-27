"""FastMCP server for node-red-homeassistant-mcp."""

import json

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .client import NRClient, NRError
from .backup import BackupStore
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics, ha_ws, subflow_edit

settings = Settings()
client = NRClient(settings)
store = BackupStore(settings.backup_dir)

HA_AUTHORING_GUIDANCE = (
    "When authoring Node-RED flows that use Home Assistant "
    "(node-red-contrib-home-assistant-websocket) nodes: "
    "(1) use ha_search_entities to resolve the correct entity_id; "
    "(2) reuse an existing source node when one already provides the data "
    "(e.g. an existing poll-state for a sensor) instead of adding a new one; "
    "(3) to build a NEW HA node, first nr_search_nodes for an existing node of the "
    "same type and clone its schema; "
    "(4) avoid deprecated fields like state_type/stateType — use outputProperties; "
    "(5) get the HA server config id via ha_get_server_config."
)

mcp = FastMCP(
    "node-red-homeassistant-mcp",
    instructions=HA_AUTHORING_GUIDANCE,
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
async def nr_get_subflow(name_or_id: str) -> str:
    """Resolve a subflow by name, definition id, or instance id. Returns the
    definition, its inputs/outputs/status resolved to the internal nodes that feed
    each port (with output labels), the internal nodes (annotated with where they
    actually wire, including subflow output ports), pointers to nested subflows, and
    every instance with which outputs it wires. Use this for subflows; nr_get_flow
    handles tabs only."""
    return await _wrap(lambda: flows.get_subflow(client, name_or_id))


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
async def nr_capture_debug(
    window_ms: int = 2000,
    inject_node_id: str | None = None,
    topics: list[str] | None = None,
    node_id: str | None = None,
    flow_id: str | None = None,
    max_messages: int = 100,
) -> str:
    """Capture live debug/status messages from the Node-RED /comms WebSocket.

    Subscribes first, then (optionally) triggers `inject_node_id`, then collects
    messages for `window_ms`. Use this to verify a flow's output deterministically:
    debug-node output is NOT in flow context, so the context tools cannot see it.
    Without `inject_node_id` it passively listens for the window. `topics` selects
    channels: "debug", "status", "notification" (default ["debug", "status"])."""
    return await _wrap(lambda: runtime.capture_debug(
        client,
        window_ms=window_ms,
        inject_node_id=inject_node_id,
        topics=topics,
        node_id=node_id,
        flow_id=flow_id,
        max_messages=max_messages,
    ))


@mcp.tool()
async def nr_get_runtime_events(window_ms: int = 2000, max_messages: int = 100) -> str:
    """Capture Node-RED runtime/system events from the /comms `notification/#` channel.

    Surfaces deploy outcomes, runtime-state changes (flows started/stopped, safe
    mode) and node/module problems (failed loads, unregistered types). Use it right
    after a deploy to check nothing broke. It listens passively for `window_ms`, so
    trigger the action during the window. (Does not filter by severity yet; returns
    events by source.)"""
    return await _wrap(lambda: runtime.get_runtime_events(
        client, window_ms=window_ms, max_messages=max_messages))


@mcp.tool()
async def nr_validate_flow(nodes: list[dict] | None = None) -> str:
    """Validate flow structure and report errors/warnings WITHOUT deploying.

    With `nodes`: validates (current flows + those nodes) — run it before
    nr_create_nodes/nr_safe_deploy to catch problems early (findings caused by the
    new nodes are marked `focus`). Without `nodes`: audits the current deployed
    flows. Checks references (wires, z/tab, group members), duplicate ids, and node
    types; it does NOT check per-type fields."""
    return await _wrap(lambda: validate.validate_flow(client, nodes=nodes))


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
    """Create nodes/groups (each needs id,type and z for tab nodes). Confirm-gated, backup-first.
    For HA nodes: clone the schema of an existing same-type node (nr_search_nodes) and prefer reusing existing source nodes; avoid deprecated fields like state_type (use outputProperties)."""
    return await _wrap(lambda: edit.create_nodes(client, store, settings, nodes=nodes, confirm=confirm, description=description))


@mcp.tool()
async def nr_delete_nodes(node_ids: list[str], confirm: bool = False, description: str | None = None) -> str:
    """Delete nodes by id (cleans group/wire refs). Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.delete_nodes(client, store, settings, node_ids=node_ids, confirm=confirm, description=description))


@mcp.tool()
async def nr_instantiate_subflow(subflow: str, z: str, name: str | None = None, env: list[dict] | None = None, confirm: bool = False) -> str:
    """Instantiate a subflow (by name/def-id/instance-id) on tab `z` with the correct number of output wire slots. Returns the new node_id. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.instantiate_subflow(client, store, settings, subflow=subflow, z=z, name=name, env=env, confirm=confirm))


@mcp.tool()
async def nr_add_subflow_port(subflow: str, label: str | None = None, source: dict | None = None, confirm: bool = False) -> str:
    """Append an output port to a subflow, synced to the definition AND every instance's wires. `source`={node_id,port} wires the new port to an internal node. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.add_subflow_port(client, store, settings, subflow=subflow, label=label, source=source, confirm=confirm))


@mcp.tool()
async def nr_remove_subflow_port(subflow: str, index: int, confirm: bool = False) -> str:
    """Remove output port `index` from a subflow, synced to the definition AND every instance's wires. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.remove_subflow_port(client, store, settings, subflow=subflow, index=index, confirm=confirm))


@mcp.tool()
async def nr_delete_subflow(subflow: str, cascade: bool = False, confirm: bool = False) -> str:
    """Delete a subflow definition + its internal nodes. Refuses if instances exist unless cascade=true (which also deletes the instances). Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.delete_subflow(client, store, settings, subflow=subflow, cascade=cascade, confirm=confirm))


@mcp.tool()
async def nr_create_subflow(name: str, internal_nodes: list[dict], outputs: list[dict],
                            input_to: str | list[str] | None = None,
                            status_from: dict | None = None,
                            env: list[dict] | None = None, confirm: bool = False) -> str:
    """Create a subflow definition from scratch: assembles the definition node +
    internal nodes (z auto-set) + validated port wiring in one atomic deploy.
    `outputs`=[{label?, source:{node_id, port?}}]; `input_to`=internal id or list;
    `status_from`={node_id, port?}. Returns subflow_id. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.create_subflow(client, store, settings,
        name=name, internal_nodes=internal_nodes, outputs=outputs,
        input_to=input_to, status_from=status_from, env=env, confirm=confirm))


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


@mcp.tool()
async def nr_diff(path: str, path_b: str | None = None) -> str:
    """Diff a backup against the current flows, or against another backup.

    `path` is a backup path from nr_list_backups. With only `path`: before=that
    backup, after=current flows (what changed since the snapshot). With `path_b`:
    before=path, after=path_b. Returns added/removed (each with id/type/name) and
    field-level `changed`, plus a `summary` count."""
    return await _wrap(lambda: deploy.diff_backup(client, store, path=path, path_b=path_b))


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
async def ha_search_entities(query: str, limit: int = 20) -> str:
    """Search live Home Assistant entities by entity_id or friendly name (e.g. "umidità bagno").
    Use this to resolve the correct entity_id BEFORE authoring HA nodes. Requires HA_URL + HA_TOKEN."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA entity search needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.search_entities(ha_client, query, limit))
    finally:
        await ha_client.close()


@mcp.tool()
async def ha_get_state(entity_id: str) -> str:
    """Get the current state + attributes of a Home Assistant entity. Requires HA_URL + HA_TOKEN."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA get_state needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.get_entity_state(ha_client, entity_id))
    finally:
        await ha_client.close()


@mcp.tool()
async def ha_get_services(domain: str | None = None) -> str:
    """List Home Assistant services for authoring call-service nodes. Requires HA_URL + HA_TOKEN.

    Without `domain`: a compact index {domain: [service names]}. With `domain`:
    that domain's services with description and fields (each field:
    description/example/required). Use it to fill a call-service node's
    domain/service/data correctly."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA get_services needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.get_services(ha_client, domain))
    finally:
        await ha_client.close()


@mcp.tool()
async def ha_get_entity_history(entity_id: str, hours: int = 24, max_points: int = 200) -> str:
    """Recent state history of a Home Assistant entity. Requires HA_URL + HA_TOKEN.

    Returns up to `max_points` most-recent state changes over the last `hours`,
    with `first`/`last` and a `total_changes` count. NOTE: HA keeps raw history for
    a limited period (recorder default ~10 days); for wider spans (weeks/months)
    use long-term statistics, not this tool."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA get_entity_history needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.get_entity_history(ha_client, entity_id, hours=hours, max_points=max_points))
    finally:
        await ha_client.close()


@mcp.tool()
async def nr_diagnostics() -> str:
    """Read-only health snapshot: Node-RED reachability/version/runtime-state,
    context store names, flow counts, installed node sets, MCP config, and Home
    Assistant configured/reachable. Run it first when something seems off."""
    return await _wrap(lambda: diagnostics.run(client, settings))


@mcp.tool()
async def ha_get_entity_statistics(entity_id: str, days: int = 30,
                                   period: str = "day", max_buckets: int = 200) -> str:
    """Long-term statistics (downsampled mean/min/max) for a Home Assistant entity.
    Requires HA_URL + HA_TOKEN.

    Returns up to `max_buckets` aggregate buckets over the last `days`, bucketed by
    `period` ("hour"|"day"|"week"|"month"). This is the tool for WIDE spans
    (weeks/months/year) where raw history (ha_get_entity_history) is retention-limited."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA get_entity_statistics needs HA_URL and HA_TOKEN to be set.")
    return await _wrap(lambda: ha_ws.get_entity_statistics(
        settings, entity_id, days=days, period=period, max_buckets=max_buckets))


@mcp.tool()
async def ha_check_nodered_integration() -> str:
    """Check whether the 'Node-RED Companion' integration is set up in Home Assistant.
    ha-switch/ha-button/ha-sensor nodes can only publish entities to HA when this
    integration is installed (Settings -> Devices & Services) - a manual HA step no tool
    can perform. Read-only; requires an admin HA token. Returns
    {installed, entries:[{entry_id,title,state}], note}."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA check_nodered_integration needs HA_URL and HA_TOKEN to be set.")
    return await _wrap(lambda: ha_ws.check_nodered_integration(settings))


def main():
    """Console-script entry point."""
    transport = "streamable-http" if settings.mcp_transport == "http" else "stdio"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
