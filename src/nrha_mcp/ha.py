"""Home Assistant awareness for Node-RED flows.

Recognizes node-red-contrib-home-assistant-websocket node types and provides
a minimal REST client used for the HA reachability ping.
"""

import httpx

from .client import NRClient
from .config import Settings

# Node types provided by node-red-contrib-home-assistant-websocket
HA_NODE_TYPES = {
    "server", "ha-entity-config", "ha-device-config",
    "api-call-service", "api-current-state", "api-get-history", "api-render-template",
    "server-state-changed", "poll-state", "trigger-state", "server-events",
    "ha-webhook", "ha-fire-event", "ha-get-entities", "ha-wait-until", "ha-sensor",
    "ha-button", "ha-switch", "ha-binary-sensor", "ha-number", "ha-select", "ha-text",
}


def _entity_of(node: dict) -> str | None:
    # HA nodes store the entity under different keys across versions
    for k in ("entityId", "entity_id", "entityid"):
        if node.get(k):
            return node[k]
    data = node.get("data")
    if isinstance(data, dict) and data.get("entity_id"):
        return data["entity_id"]
    return None


async def list_ha_nodes(client: NRClient) -> dict:
    flows, _ = await client.get_flows()
    out = []
    for n in flows:
        if n.get("type") in HA_NODE_TYPES and n.get("type") not in ("server", "ha-entity-config", "ha-device-config"):
            out.append({
                "id": n["id"], "type": n.get("type"), "name": n.get("name", ""),
                "flow_id": n.get("z", ""),
                "domain": n.get("domain"), "service": n.get("service"),
                "entity": _entity_of(n),
            })
    return {"ha_nodes": out, "total": len(out)}


async def get_server_config(client: NRClient) -> dict:
    flows, _ = await client.get_flows()
    servers = [{"id": f["id"], "name": f.get("name", "")} for f in flows if f.get("type") == "server"]
    return {"servers": servers, "total": len(servers)}


class HARestClient:
    """Minimal Home Assistant REST client (reachability ping only)."""

    def __init__(self, settings: Settings):
        self._client = httpx.AsyncClient(
            base_url=settings.ha_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=15.0,
        )

    async def ping(self) -> dict:
        r = await self._client.get("/api/")
        r.raise_for_status()
        return r.json()

    async def close(self):
        await self._client.aclose()
