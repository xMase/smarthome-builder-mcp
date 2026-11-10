"""MCP server for smarthome-builder-mcp."""

import hmac
import json
import logging
from importlib.metadata import PackageNotFoundError, version

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent, Tool as MCPTool

from .config import Settings
from .client import NRClient, NRError
from .backup import BackupStore
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics, subflow_edit, esphome, esphome_proxy

settings = Settings()
client = NRClient(settings)
store = BackupStore(settings.backup_dir)

HA_AUTHORING_GUIDANCE = (
    "Authoring Node-RED + Home Assistant "
    "(node-red-contrib-home-assistant-websocket) flows.\n\n"
    "Nodes: resolve entity_id via ha-mcp's ha_search (a separate MCP server the client must also connect); reuse an existing source node "
    "(e.g. a poll-state) before adding one; to add a new HA node, clone the CURRENT "
    "schema of a same-type node via nr_search_nodes rather than emitting fields from "
    "memory — training-data schemas are often stale (e.g. the deprecated "
    "state_type/stateType; use outputProperties instead). Validate new nodes with "
    "nr_validate_flow before deploying. Server config id from nr_get_ha_servers.\n\n"
    "Layout: left→right by data flow; columns ~180px (x), rows ~80px (y), every x/y "
    "on a 20px grid, no overlaps. Group related nodes (one automation, or all nodes "
    "for an entity) in a type:'group' node: descriptive name, style:{label:true}, "
    "member ids in 'nodes', g=<groupId> on each member (both must agree), x/y/w/h "
    "bounding the members.\n\n"
    "Mutating flows is confirm-gated + backup-first: tools preview, then re-call with "
    "confirm=true. To change HA itself (entity-registry edits, etc.) use ha-mcp's typed "
    "tools (e.g. ha_set_entity) — don't build throwaway nodes for it. Before creating "
    "ha-switch/ha-button/ha-sensor nodes, check with ha-mcp's "
    "ha_get_integration(domain=\"nodered\") that the Node-RED Companion integration exists "
    "with state loaded; setting it up is a manual HA step."
    "\n\nESPHome: esphome_* tools other than esphome_logs / esphome_list_backups / "
    "esphome_restore_backup come from the Device Builder itself. Writes need confirm=true "
    "(install and set_secret always). Before esphome_update_config, read esphome_get_config "
    "and pass its text as expected."
)

try:
    _version = version("smarthome-builder-mcp")
except PackageNotFoundError:  # source checkout run without installing the package
    _version = "0.0.0+unknown"

class _StaticTokenVerifier:
    """Accepts exactly one bearer token: MCP_AUTH_TOKEN."""

    def __init__(self, token: str):
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        # Starlette decodes header bytes as latin-1: re-encode to get the raw (UTF-8) bytes back.
        if hmac.compare_digest(token.encode("latin-1", "replace"), self._token):
            return AccessToken(token=token, client_id="static-token", scopes=[])
        return None


_auth = {}
if settings.mcp_auth_token is not None:
    _auth = {
        "token_verifier": _StaticTokenVerifier(settings.mcp_auth_token.get_secret_value()),
        # issuer_url is required by AuthSettings but published nowhere without an auth_server_provider.
        "auth": AuthSettings(issuer_url="http://localhost", resource_server_url=None),
    }

_log = logging.getLogger(__name__)


class _Server(MCPServer):
    """Adds the esphome_* tools generated from the Device Builder's MCP server (no static decorator)."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        if not settings.ha_url or not settings.ha_token:
            return tools
        try:
            upstream = await esphome_proxy.list_tools(settings)
            taken = {t.name for t in tools}
            return tools + [
                MCPTool(name=u["name"], description=u["description"], input_schema=u["inputSchema"])
                for u in upstream if u["name"] not in taken]
        except Exception as e:  # noqa: BLE001 - upstream down or malformed must not hide the static tools
            _log.warning("ESPHome upstream tool list unavailable: %s: %s", type(e).__name__, e)
            return tools

    async def call_tool(self, name, arguments, context=None):
        if self._tool_manager.get_tool(name) is not None or not name.startswith(esphome_proxy.PREFIX):
            return await super().call_tool(name, arguments, context)
        text = _esphome_unset() or await _wrap(
            lambda: esphome_proxy.call(settings, name[len(esphome_proxy.PREFIX):], arguments))
        return CallToolResult(content=[TextContent(type="text", text=text)])


mcp = _Server(
    "smarthome-builder-mcp",
    instructions=HA_AUTHORING_GUIDANCE,
    version=_version,
    **_auth,
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
async def nr_export_flow(name_or_id: str) -> str:
    """Export one tab as an import-ready Node-RED flow array.

    Returns {flow, tab_id, node_count, group_count}; `flow` is the flat
    [tab, ...groups, ...nodes] array to paste into Node-RED's Import dialog. Full
    fidelity. Does NOT bundle subflow definitions or global config nodes the tab
    references — use nr_backup_flow for a complete all-flows snapshot."""
    return await _wrap(lambda: flows.export_flow(client, name_or_id))


@mcp.tool()
async def nr_get_subflow(name_or_id: str) -> str:
    """Resolve a subflow by name, definition id, or instance id. Returns the definition;
    its inputs/outputs/status resolved to the internal nodes feeding each port (with
    labels); the internal nodes (annotated with where they wire, incl. output ports);
    nested-subflow pointers; and every instance with its output wiring. Use for subflows;
    nr_get_flow handles tabs only."""
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

    Subscribes, optionally triggers `inject_node_id`, then collects for `window_ms`.
    Use it to verify a flow's output: debug-node output is NOT in flow context, so the
    context tools can't see it. `topics` selects channels "debug"/"status"/"notification"
    (default ["debug", "status"])."""
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

    Surfaces deploy outcomes, runtime-state changes (flows started/stopped, safe mode)
    and node/module problems (failed loads, unregistered types). Run it right after a
    deploy to check nothing broke; it listens passively for `window_ms`, so trigger the
    action during the window."""
    return await _wrap(lambda: runtime.get_runtime_events(
        client, window_ms=window_ms, max_messages=max_messages))


@mcp.tool()
async def nr_validate_flow(nodes: list[dict] | None = None) -> str:
    """Validate flow structure and report errors/warnings WITHOUT deploying.

    With `nodes`: validates current flows + those nodes — run before nr_create_nodes/
    nr_safe_deploy to catch problems early (findings from the new nodes are marked
    `focus`). Without `nodes`: audits the deployed flows. Checks references (wires,
    z/tab, group members), duplicate ids, and node types; not per-type fields."""
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
    """Create nodes/groups (each needs id,type; tab nodes also need z). Confirm-gated,
    backup-first. Follow the server instructions for HA-node authoring, layout, and
    grouping (a group needs both its 'nodes' member list AND g=<groupId> on each member)."""
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
async def nr_list_ha_nodes() -> str:
    """List Home Assistant nodes in the flows with their domain/service/entity."""
    return await _wrap(lambda: ha.list_ha_nodes(client))


@mcp.tool()
async def nr_get_ha_servers() -> str:
    """List the Home Assistant server config nodes Node-RED uses."""
    return await _wrap(lambda: ha.get_server_config(client))


@mcp.tool()
async def nr_diagnostics() -> str:
    """Read-only health snapshot: Node-RED reachability/version/runtime-state,
    context store names, flow counts, installed node sets, MCP config, and Home
    Assistant configured/reachable. Run it first when something seems off."""
    return await _wrap(lambda: diagnostics.run(client, settings))


# ---- ESPHome (Device Builder add-on via HA ingress) ----
def _esphome_unset() -> str | None:
    if not settings.ha_url or not settings.ha_token:
        return _err("ESPHome tools need HA_URL and HA_TOKEN to be set.")
    return None


@mcp.tool()
async def esphome_logs(configuration: str, seconds: float = 10, max_lines: int = 200) -> str:
    """Capture live device logs for `seconds` (max 60) or `max_lines` lines, then stop."""
    return _esphome_unset() or await _wrap(lambda: esphome.logs(
        settings, configuration, seconds=seconds, max_lines=max_lines))


@mcp.tool()
async def esphome_list_backups(configuration: str | None = None) -> str:
    """List local ESPHome YAML backups (newest first), saved before each confirmed config write."""
    return await _wrap(lambda: esphome.list_backups(settings, configuration))


@mcp.tool()
async def esphome_restore_backup(backup_id: str, base_sha256: str | None = None,
                                 confirm: bool = False) -> str:
    """Restore a local ESPHome backup (diff preview, backup of the current file, conflict check)."""
    return _esphome_unset() or await _wrap(lambda: esphome.restore_backup(
        settings, backup_id, base_sha256=base_sha256, confirm=confirm))


def main():
    """Console-script entry point."""
    if settings.mcp_transport == "http":
        if settings.mcp_auth_token is None:
            raise SystemExit(
                "MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        mcp.run(transport="streamable-http", host=settings.mcp_http_host,
                port=settings.mcp_http_port, json_response=True, stateless_http=True)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
