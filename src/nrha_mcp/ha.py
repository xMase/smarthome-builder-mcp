"""Home Assistant awareness for Node-RED flows.

Recognizes node-red-contrib-home-assistant-websocket node types and provides
a minimal REST client for live entity discovery.
"""

import httpx
from datetime import datetime, timedelta, timezone

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
    """Minimal Home Assistant REST client for entity discovery."""

    def __init__(self, settings: Settings):
        self._client = httpx.AsyncClient(
            base_url=settings.ha_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=15.0,
        )

    async def get_states(self) -> list[dict]:
        r = await self._client.get("/api/states")
        r.raise_for_status()
        return r.json()

    async def get_state(self, entity_id: str) -> dict:
        r = await self._client.get(f"/api/states/{entity_id}")
        r.raise_for_status()
        return r.json()

    async def get_services(self) -> list[dict]:
        r = await self._client.get("/api/services")
        r.raise_for_status()
        return r.json()

    async def get_history(self, entity_id: str, start_iso: str, end_iso: str | None = None) -> list:
        params = {"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""}
        if end_iso:
            params["end_time"] = end_iso
        r = await self._client.get(f"/api/history/period/{start_iso}", params=params)
        r.raise_for_status()
        return r.json()

    async def ping(self) -> dict:
        r = await self._client.get("/api/")
        r.raise_for_status()
        return r.json()

    async def close(self):
        await self._client.aclose()


async def search_entities(ha_client, query: str, limit: int = 20) -> dict:
    """Find HA entities whose entity_id or friendly_name contains `query` (case-insensitive)."""
    q = query.lower()
    states = await ha_client.get_states()
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        fname = s.get("attributes", {}).get("friendly_name", "") or ""
        if q in eid.lower() or q in fname.lower():
            out.append({
                "entity_id": eid,
                "friendly_name": fname,
                "state": s.get("state"),
                "domain": eid.split(".")[0] if "." in eid else "",
            })
            if len(out) >= limit:
                break
    return {"results": out, "total": len(out), "query": query}


async def get_entity_state(ha_client, entity_id: str) -> dict:
    """Get the current state and attributes of a Home Assistant entity."""
    s = await ha_client.get_state(entity_id)
    return {
        "entity_id": s.get("entity_id", entity_id),
        "state": s.get("state"),
        "attributes": s.get("attributes", {}),
    }


def _trim_field(f: dict) -> dict:
    return {
        "description": f.get("description", ""),
        "example": f.get("example"),
        "required": f.get("required", False),
    }


async def get_services(ha_client, domain: str | None = None) -> dict:
    """List HA services. No domain -> compact index; a domain -> trimmed detail."""
    data = await ha_client.get_services()  # list of {domain, services}
    if domain is None:
        index = {d["domain"]: sorted((d.get("services") or {}).keys()) for d in data if d.get("domain")}
        return {"domains": index, "total_domains": len(index)}
    entry = next((d for d in data if d.get("domain") == domain), None)
    if entry is None:
        available = sorted(d["domain"] for d in data if d.get("domain"))
        return {"domain": domain, "found": False, "available_domains": available}
    services = {}
    for name, svc in (entry.get("services") or {}).items():
        services[name] = {
            "name": svc.get("name", ""),
            "description": svc.get("description", ""),
            "fields": {fn: _trim_field(f) for fn, f in (svc.get("fields") or {}).items()},
        }
    return {"domain": domain, "services": services, "total_services": len(services)}


_RETENTION_NOTE = (
    "HA keeps raw history for a limited period (recorder default ~10 days), so a "
    "wide window returns only what is retained (possibly nothing). For weeks/months "
    "use long-term statistics, not this tool."
)


async def get_entity_history(ha_client, entity_id: str, hours: int = 24, max_points: int = 200) -> dict:
    """Recent state-change history for one entity over the last `hours`."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    data = await ha_client.get_history(entity_id, start.isoformat())
    series = data[0] if data else []
    points = [{"state": p.get("state"),
               "last_changed": p.get("last_changed") or p.get("last_updated")} for p in series]
    total = len(points)
    kept = points[-max_points:] if total > max_points else points
    out = {
        "entity_id": entity_id,
        "hours": hours,
        "total_changes": total,
        "points": kept,
        "first": points[0] if points else None,
        "last": points[-1] if points else None,
    }
    if total > max_points:
        out["truncated"] = True
        out["truncated_note"] = f"Showing the most recent {max_points} of {total} state changes."
    if not points or hours > 240:
        out["note"] = _RETENTION_NOTE
    return out
