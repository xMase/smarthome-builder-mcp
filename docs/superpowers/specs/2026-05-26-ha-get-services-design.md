# HA Service Discovery for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/ha-get-services`

## Problem

The MCP can discover HA entities (`ha_search_entities`, `ha_get_state`) but not HA
**services**. To author a `call-service` node correctly an agent needs the right
`domain`, `service`, and the service's data fields — otherwise it guesses. Home
Assistant exposes this at `GET /api/services`, but that payload is large (dozens of
domains × services × full field schemas), so it must be surfaced in a bounded way.

## Goals

- An `ha_get_services` tool that lets an agent discover services for authoring
  `call-service` nodes, with bounded output: a compact index of domains→services,
  and on-demand detail (description + fields) for one domain.

## Non-goals

- Returning the entire `/api/services` payload at once (firehose — wasteful tokens,
  contradicts the bounded pattern of the other HA tools).
- Passing through raw `selector` field schemas. Per field we keep only
  `description`/`example`/`required`.
- Calling services (side effects). This is read-only discovery.

## Design

### A. Client method — `ha.HARestClient`

```python
async def get_services(self) -> list[dict]:
    r = await self._client.get("/api/services")
    r.raise_for_status()
    return r.json()
```

`/api/services` returns a list of `{"domain": str, "services": {name: {"name",
"description", "fields": {field: {...}}, "target": {...}}}}`.

### B. Module function — `ha.get_services(ha_client, domain=None)`

- **No domain → compact index:**

  ```json
  {"domains": {"light": ["turn_off", "turn_on"], "switch": ["toggle", "turn_off", "turn_on"]},
   "total_domains": 2}
  ```

  (service names sorted; no field schemas)

- **Domain found → detail:**

  ```json
  {"domain": "light",
   "services": {"turn_on": {"name": "Turn on", "description": "...",
                            "fields": {"brightness_pct": {"description": "...", "example": 50, "required": false}}}},
   "total_services": 1}
  ```

  Per service: `name`, `description`, and `fields` where each field keeps only
  `description`, `example`, `required` (drops verbose `selector`/etc.).

- **Domain not found → actionable:**

  ```json
  {"domain": "nope", "found": false, "available_domains": ["light", "switch", ...]}
  ```

  (so the agent can correct without another call; `available_domains` is just the
  domain names, sorted)

### C. Tool — `server.py`

```python
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
```

Same create-use-close + `HA_URL`/`HA_TOKEN` guard pattern as `ha_search_entities`
/`ha_get_state`. `ha` is already imported in `server.py`.

**Tool count goes 26 → 27.** Update both hardcoded sites: `tests/test_server_smoke.py`
(`expected` + `len(names) == 27`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

HA REST errors propagate via `raise_for_status()` and surface through `_wrap`
(consistent with the existing HA tools). A missing `HA_URL`/`HA_TOKEN` returns a
clear `_err` message and never opens a client.

## Testing

`tests/test_ha.py` (extend) — the existing `FakeHAClient` gains a `get_services()`
method returning a small sample `/api/services` payload (a couple of domains, each
with services that include a `fields` dict with `selector` to prove it is dropped):
- no domain → `domains` index with sorted service names; `total_domains` correct.
- a known domain → `services` with `name`/`description` and `fields` trimmed to
  `description`/`example`/`required` (selector dropped).
- unknown domain → `found: false` and `available_domains` listing the real domains.

`tests/test_server_smoke.py`: count 27; `ha_get_services` registered.

`scripts/smoke_e2e.py`: add `"ha_get_services"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild and redeploy the Docker image to expose the tool live; the running MCP
must have `HA_URL`/`HA_TOKEN` set for it to function. Local `.venv` is for tests.

## Follow-ups

- A `ha_get_services(domain, service)` single-service form could be added if even a
  domain's detail proves too large in practice. YAGNI for now.
