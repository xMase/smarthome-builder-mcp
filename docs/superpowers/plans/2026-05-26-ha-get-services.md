# HA Service Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ha_get_services` tool that discovers Home Assistant services (a compact domain→services index, or one domain's detail with trimmed fields) for authoring `call-service` nodes.

**Architecture:** Add `HARestClient.get_services()` (GET `/api/services`) and a pure-ish `ha.get_services(ha_client, domain=None)` that returns an index when no domain is given and trimmed detail when one is. Expose it as `ha_get_services` with the existing HA create-use-close + env-guard pattern.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), httpx, pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-ha-get-services-design.md`

---

## File structure

- Modify `src/nrha_mcp/ha.py` — add `HARestClient.get_services()`, `_trim_field`, and `get_services(ha_client, domain=None)`.
- Modify `src/nrha_mcp/server.py` — add the `ha_get_services` tool.
- Modify `tests/test_ha.py` — `FAKE_SERVICES` sample, `FakeHAClient.get_services`, and tests.
- Modify `tests/test_server_smoke.py` — tool count 26 → 27.
- Modify `scripts/smoke_e2e.py` — add the tool to `EXPECTED_TOOLS`.

All commands from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv venv, no `pip`). Branch: `feat/ha-get-services`.

---

## Task 1: `ha.get_services` function + client method

**Files:**
- Modify: `src/nrha_mcp/ha.py`
- Test: `tests/test_ha.py`

- [ ] **Step 1: Add the test fixtures + failing tests**

In `tests/test_ha.py`, add a `FAKE_SERVICES` constant right after the existing `FAKE_STATES` list:

```python
FAKE_SERVICES = [
    {"domain": "light", "services": {
        "turn_on": {"name": "Turn on", "description": "Turn the light on",
                    "fields": {"brightness_pct": {"description": "Brightness %", "example": 50,
                                                  "required": False,
                                                  "selector": {"number": {"min": 0, "max": 100}}}}},
        "turn_off": {"name": "Turn off", "description": "Turn the light off", "fields": {}},
    }},
    {"domain": "switch", "services": {
        "toggle": {"name": "Toggle", "description": "Toggle the switch", "fields": {}},
    }},
]
```

In the same file, extend `FakeHAClient.__init__` to accept services and add a `get_services` method. Change the class to:

```python
class FakeHAClient:
    """Minimal fake HA REST client for unit tests."""

    def __init__(self, states=None, state_map=None, services=None):
        self._states = states or FAKE_STATES
        self._state_map = state_map or {s["entity_id"]: s for s in self._states}
        self._services = services if services is not None else FAKE_SERVICES

    async def get_states(self):
        return list(self._states)

    async def get_state(self, entity_id: str):
        if entity_id not in self._state_map:
            raise KeyError(entity_id)
        return self._state_map[entity_id]

    async def get_services(self):
        return list(self._services)
```

Then add these tests at the end of `tests/test_ha.py`:

```python
@pytest.mark.asyncio
async def test_get_services_index():
    fake = FakeHAClient()
    res = await ha.get_services(fake)
    assert res["total_domains"] == 2
    assert res["domains"]["light"] == ["turn_off", "turn_on"]   # names sorted
    assert res["domains"]["switch"] == ["toggle"]


@pytest.mark.asyncio
async def test_get_services_domain_detail_trims_fields():
    fake = FakeHAClient()
    res = await ha.get_services(fake, "light")
    assert res["domain"] == "light"
    assert res["total_services"] == 2
    svc = res["services"]["turn_on"]
    assert svc["name"] == "Turn on"
    assert svc["fields"]["brightness_pct"] == {
        "description": "Brightness %", "example": 50, "required": False,
    }  # selector dropped


@pytest.mark.asyncio
async def test_get_services_unknown_domain():
    fake = FakeHAClient()
    res = await ha.get_services(fake, "nope")
    assert res["found"] is False
    assert res["available_domains"] == ["light", "switch"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_ha.py -k "get_services" -v`
Expected: FAIL with `AttributeError: module 'nrha_mcp.ha' has no attribute 'get_services'`

- [ ] **Step 3: Add the client method**

In `src/nrha_mcp/ha.py`, in the `HARestClient` class, add this method right after `get_state` (before `close`):

```python
    async def get_services(self) -> list[dict]:
        r = await self._client.get("/api/services")
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Add the module function**

In `src/nrha_mcp/ha.py`, append at the end of the file:

```python
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
        index = {d.get("domain"): sorted((d.get("services") or {}).keys()) for d in data}
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_ha.py -v`
Expected: all pass (existing HA tests + the 3 new ones).

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat(ha): get_services (domain index + trimmed per-domain detail)"
```

---

## Task 2: Register the `ha_get_services` tool

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"ha_get_services",` to the `expected` set (e.g. on the HA line, after `"ha_get_state",`) and change the count. The HA line becomes:

```python
        "ha_search_entities", "ha_get_state", "ha_get_services",
```

and:

```python
    assert len(names) == 27
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `ha_get_services` not registered; `len(names)` is 26, not 27.

- [ ] **Step 3: Add the tool**

In `src/nrha_mcp/server.py`, immediately AFTER the `ha_get_state` function (after its closing `))` / `finally` block), add:

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

- [ ] **Step 4: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 5: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"ha_get_services",` to the `EXPECTED_TOOLS` set (e.g. right after `"ha_get_state",`). The script asserts `len(EXPECTED_TOOLS)` tools are present.

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add ha_get_services tool"
```

---

## Task 3: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('services:', 'ha_get_services' in n); print('count:', len(n))"`
Expected: `services: True` and `count: 27`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack). The running MCP needs `HA_URL`/`HA_TOKEN` for the tool to function. Do it when ready.

---

## Follow-up (deferred, per spec)

- A `ha_get_services(domain, service)` single-service form if a domain's detail proves too large.

---

## Self-review

- **Spec coverage:** client method (`HARestClient.get_services`) → Task 1 Step 3; index/detail/not-found function (`ha.get_services` + `_trim_field`) → Task 1 Step 4; field trimming (selector dropped) → Task 1 (`_trim_field` + `test_get_services_domain_detail_trims_fields`); tool + env guard + create-use-close + 26→27 in both sites → Task 2; error handling via `raise_for_status`/`_wrap` (inherited, unchanged); verify + deploy → Task 3; single-service form → Follow-up. No gaps.
- **Placeholders:** none — full code and exact commands throughout.
- **Type consistency:** `get_services(ha_client, domain=None)` signature identical in Task 1 (def), its tests, and the Task 2 `server.py` wrapper. Return keys differ by branch by design: index (`domains`/`total_domains`), detail (`domain`/`services`/`total_services`), not-found (`domain`/`found`/`available_domains`) — each asserted in its matching test. `FakeHAClient.get_services` matches `HARestClient.get_services` (both return a list). Tool count 27 consistent across Task 2 and Task 3.
