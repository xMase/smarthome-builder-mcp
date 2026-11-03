"""JSON-RPC transport to the Device Builder's own MCP endpoint (/api/mcp) through HA ingress."""
import asyncio
import itertools

import httpx

from .client import NRAuthError
from .esphome import CONNECT_TIMEOUT, REPLY_TIMEOUT, ESPHomeError, _open_ingress

_ids = itertools.count(1)


async def rpc(settings, method: str, params: dict | None = None, *, ha_connect=None,
              transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """POST one JSON-RPC request; return its `result`. A fresh ingress session per call (no cache)."""
    try:
        ingress_url, session_id = await asyncio.wait_for(
            _open_ingress(settings, ha_connect), CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        raise ESPHomeError(f"timed out after {CONNECT_TIMEOUT}s opening the HA ingress session") from None
    url = f"{settings.ha_url.rstrip('/')}/{ingress_url.strip('/')}/api/mcp"
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        body["params"] = params
    try:
        async with httpx.AsyncClient(timeout=REPLY_TIMEOUT, transport=transport,
                                       cookies={"ingress_session": session_id}) as http:
            resp = await http.post(url, json=body)
    except httpx.HTTPError as e:
        raise ESPHomeError(
            f"cannot reach the Device Builder MCP endpoint: {type(e).__name__}: {e}") from None
    if resp.status_code in (401, 403):
        raise NRAuthError(f"HA ingress refused the Device Builder MCP endpoint (HTTP {resp.status_code}); "
                          "the HA token must belong to an admin user")
    if resp.status_code == 404:
        raise ESPHomeError("ESPHome Device Builder >= 1.15 is required for esphome_* tools "
                           "(no /api/mcp endpoint)")
    try:
        resp.raise_for_status()
        reply = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ESPHomeError(f"bad reply from the Device Builder MCP endpoint: {type(e).__name__}: {e}") from None
    if "error" in reply:
        message = (reply["error"] or {}).get("message", "unknown error")
        if message.startswith("Unknown tool"):
            message += (": the ESPHome tool list changed (add-on updated?); "
                        "reconnect this MCP server (/mcp) or start a new session.")
        raise ESPHomeError(f"Device Builder MCP error: {message}")
    return reply.get("result") or {}
