"""Read-only health snapshot of Node-RED plus the MCP's HA/config wiring."""

from .client import NRError
from . import ha


async def _default_ha_ping(settings) -> dict:
    client = ha.HARestClient(settings)
    try:
        return await client.ping()
    finally:
        await client.close()


async def run(client, settings, *, ha_ping=None) -> dict:
    """Gather a read-only diagnostics snapshot; each external read is guarded."""
    try:
        s = await client.get_settings()
    except NRError as e:
        return {"node_red": {"reachable": False, "error": str(e)}}

    ctx = s.get("context") or {}
    out = {
        "node_red": {
            "reachable": True,
            "version": s.get("version"),
            "runtime_state": s.get("runtimeState"),
            "context_stores": ctx.get("stores"),
            "context_default": ctx.get("default"),
        }
    }

    try:
        flows, _ = await client.get_flows()
        out["flows"] = {
            "tabs": sum(1 for f in flows if f.get("type") == "tab"),
            "total_objects": len(flows),
        }
    except NRError:
        out["flows"] = None

    try:
        nodes = await client.get_nodes()
        out["node_sets"] = len(nodes) if isinstance(nodes, list) else None
    except NRError:
        out["node_sets"] = None

    out["config"] = {"deploy_mode": settings.deploy_mode, "backup_dir": settings.backup_dir}

    ha_block = {"configured": bool(settings.ha_url and settings.ha_token)}
    if ha_block["configured"]:
        ping = ha_ping or _default_ha_ping
        try:
            await ping(settings)
            ha_block["reachable"] = True
        except Exception as e:  # noqa: BLE001 - report any failure as not-reachable
            ha_block["reachable"] = False
            ha_block["error"] = type(e).__name__
    out["home_assistant"] = ha_block
    out["ha_call_policy"] = ha.effective_policy(settings)

    return out
