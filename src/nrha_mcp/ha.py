"""Home Assistant awareness for Node-RED flows.

Recognizes node-red-contrib-home-assistant-websocket node types and, optionally
(when HA_URL/HA_TOKEN are set), validates service/entity references against a live HA.
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
    """Minimal Home Assistant REST client (entities + services)."""

    def __init__(self, settings: Settings):
        self._client = httpx.AsyncClient(
            base_url=settings.ha_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=15.0,
        )

    async def list_entities(self) -> set[str]:
        r = await self._client.get("/api/states")
        r.raise_for_status()
        return {s["entity_id"] for s in r.json()}

    async def list_services(self) -> set[str]:
        r = await self._client.get("/api/services")
        r.raise_for_status()
        out = set()
        for dom in r.json():
            domain = dom["domain"]
            for svc in dom.get("services", {}):
                out.add(f"{domain}.{svc}")
        return out

    async def close(self):
        await self._client.aclose()


async def validate_calls(client: NRClient, ha_client) -> dict:
    """Flag api-call-service nodes whose service or entity does not exist in live HA."""
    entities = await ha_client.list_entities()
    services = await ha_client.list_services()
    listing = await list_ha_nodes(client)
    problems = []
    for n in listing["ha_nodes"]:
        if n["type"] != "api-call-service":
            continue
        prob = {}
        if n.get("domain") and n.get("service"):
            svc = f"{n['domain']}.{n['service']}"
            if svc not in services:
                prob["missing_service"] = svc
        ent = n.get("entity")
        if ent and ent not in entities:
            prob["missing_entity"] = ent
        if prob:
            problems.append({"node_id": n["id"], "name": n.get("name", ""), **prob})
    return {"problems": problems, "checked": len(listing["ha_nodes"])}
