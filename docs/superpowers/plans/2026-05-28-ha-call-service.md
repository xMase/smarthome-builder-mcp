# ha_call_service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ha_call_service` MCP tool that calls arbitrary Home Assistant services through the existing `HARestClient`, with a tiered safety model (catastrophic floor blocked, sensitive services gated behind `confirm`, everything else direct).

**Architecture:** Thin tool in `server.py` → orchestrator `ha.call_service` in `ha.py` that builds the POST body, classifies the service into a tier, applies policy, and shapes a trimmed return. Tier lists are a hardcoded catastrophic floor (additive via `HA_BLOCK_SERVICES`) plus a configurable confirm set (`HA_CONFIRM_SERVICES`, override-with-default). The effective policy is surfaced in `nr_diagnostics`.

**Tech Stack:** Python 3.11+, FastMCP, httpx, pydantic-settings, pytest/pytest-asyncio (`asyncio_mode=auto`), respx for HTTP mocking.

**Spec:** `docs/superpowers/specs/2026-05-28-ha-call-service-design.md`

---

## File Structure

- `src/nrha_mcp/config.py` — add two `Settings` fields (`ha_block_services`, `ha_confirm_services`).
- `src/nrha_mcp/ha.py` — tier constants + classification helpers + `effective_policy` (pure) + `HARestClient.call_service` (HTTP) + `ha.call_service` (orchestrator).
- `src/nrha_mcp/server.py` — register the `ha_call_service` tool.
- `src/nrha_mcp/diagnostics.py` — add `ha_call_policy` to the snapshot.
- `tests/test_ha.py` — extend `FakeHAClient`; tests for policy, client method, orchestrator.
- `tests/test_diagnostics.py` — assert the policy block appears.
- `tests/test_server_smoke.py` + `scripts/smoke_e2e.py` — register the tool, bump count 37 → 38.

Run all tests with: `.venv/bin/pytest -q`

---

## Task 1: Config fields

**Files:**
- Modify: `src/nrha_mcp/config.py:26` (after `ha_token`)
- Test: `tests/test_ha.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ha.py`:

```python
# --- ha_call_service: config defaults ---

def test_call_service_settings_defaults():
    s = Settings()
    assert s.ha_block_services == ""
    assert s.ha_confirm_services is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ha.py::test_call_service_settings_defaults -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ha_block_services'`

- [ ] **Step 3: Add the fields**

In `src/nrha_mcp/config.py`, immediately after the `ha_token: str = ""` line:

```python
    # Home Assistant service-call safety (ha_call_service)
    # csv of "domain.service" or "domain.*"
    ha_block_services: str = ""            # additive to the catastrophic floor
    ha_confirm_services: str | None = None  # override-with-default: None=default, ""=gate nothing
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ha.py::test_call_service_settings_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/config.py tests/test_ha.py
git commit -m "feat: add ha_block_services / ha_confirm_services settings"
```

---

## Task 2: Tier classification (pure functions)

**Files:**
- Modify: `src/nrha_mcp/ha.py` (add a section after the `HA_NODE_TYPES` constant, before `_entity_of`)
- Test: `tests/test_ha.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ha.py`:

```python
# --- ha_call_service: tier classification ---

def test_tier_floor_blocked():
    s = Settings()
    assert ha._service_tier("homeassistant", "restart", s) == "blocked"
    assert ha._service_tier("hassio", "restore_full", s) == "blocked"

def test_tier_default_confirm_and_direct():
    s = Settings()
    assert ha._service_tier("lock", "unlock", s) == "confirm"
    assert ha._service_tier("alarm_control_panel", "alarm_trigger", s) == "confirm"
    assert ha._service_tier("light", "turn_on", s) == "direct"
    assert ha._service_tier("cover", "open_cover", s) == "direct"

def test_tier_block_is_additive_and_wildcard():
    s = Settings(ha_block_services="cover.*")
    assert ha._service_tier("cover", "open_cover", s) == "blocked"
    assert ha._service_tier("homeassistant", "stop", s) == "blocked"   # floor still applies

def test_tier_confirm_override_replaces_default():
    s = Settings(ha_confirm_services="climate.set_temperature")
    assert ha._service_tier("lock", "unlock", s) == "direct"           # default replaced
    assert ha._service_tier("climate", "set_temperature", s) == "confirm"

def test_tier_confirm_empty_gates_nothing_but_floor_holds():
    s = Settings(ha_confirm_services="")
    assert ha._service_tier("lock", "unlock", s) == "direct"
    assert ha._service_tier("homeassistant", "stop", s) == "blocked"

def test_effective_policy_source():
    assert ha.effective_policy(Settings())["confirm_source"] == "default"
    cfg = ha.effective_policy(Settings(ha_confirm_services="lock.unlock"))
    assert cfg["confirm_source"] == "configured"
    assert cfg["confirm"] == ["lock.unlock"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ha.py -k "tier or effective_policy" -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.ha' has no attribute '_service_tier'`

- [ ] **Step 3: Implement the constants and functions**

In `src/nrha_mcp/ha.py`, add this block right after the `HA_NODE_TYPES = { ... }` constant (around line 20):

```python
# --- Service-call safety (ha_call_service) -------------------------------

# Catastrophic services: hardcoded floor, ADDITIVE only (HA_BLOCK_SERVICES can
# extend it, never shrink it), and confirm=true cannot override a floor block.
# Finalized for Home Assistant OS (Supervisor restore services present).
CATASTROPHIC_FLOOR = {
    "homeassistant.stop", "homeassistant.restart",
    "hassio.host_reboot", "hassio.host_shutdown", "hassio.supervisor_restart",
    "hassio.restore_full", "hassio.restore_partial",
    "recorder.purge", "recorder.purge_entities",
}

# Sensitive services gated behind confirm=true when HA_CONFIRM_SERVICES is unset.
DEFAULT_CONFIRM = {
    "lock.unlock", "lock.open",
    "alarm_control_panel.alarm_disarm", "alarm_control_panel.alarm_trigger",
    "update.install",
}


def _parse_service_list(raw: str) -> set[str]:
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


def _block_set(settings) -> set[str]:
    return CATASTROPHIC_FLOOR | _parse_service_list(settings.ha_block_services)


def _confirm_set(settings) -> set[str]:
    if settings.ha_confirm_services is None:
        return set(DEFAULT_CONFIRM)
    return _parse_service_list(settings.ha_confirm_services)


def _in_set(domain: str, service: str, s: set[str]) -> bool:
    return f"{domain}.{service}".lower() in s or f"{domain}.*".lower() in s


def _service_tier(domain: str, service: str, settings) -> str:
    """Return "blocked" | "confirm" | "direct"; precedence block > confirm > direct."""
    if _in_set(domain, service, _block_set(settings)):
        return "blocked"
    if _in_set(domain, service, _confirm_set(settings)):
        return "confirm"
    return "direct"


def effective_policy(settings) -> dict:
    """Resolved policy for observability (nr_diagnostics)."""
    return {
        "blocked": sorted(_block_set(settings)),
        "confirm": sorted(_confirm_set(settings)),
        "confirm_source": "default" if settings.ha_confirm_services is None else "configured",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ha.py -k "tier or effective_policy" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat: ha_call_service tier classification and effective_policy"
```

---

## Task 3: HARestClient.call_service (HTTP)

**Files:**
- Modify: `src/nrha_mcp/ha.py` — add a method to the `HARestClient` class (after `get_services`, around line 77)
- Test: `tests/test_ha.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ha.py` (`respx`, `httpx`, `json` are already importable; add `import json` at the top of the file if not present):

```python
# --- ha_call_service: HARestClient.call_service ---

@pytest.mark.asyncio
@respx.mock
async def test_harestclient_call_service_posts_body_and_returns_changed():
    import json as _json
    route = respx.post("http://ha.local/api/services/light/turn_on").mock(
        return_value=httpx.Response(
            200, json=[{"entity_id": "light.k", "state": "on", "attributes": {"brightness": 5}}]))
    c = ha.HARestClient(Settings(ha_url="http://ha.local", ha_token="t"))
    try:
        out = await c.call_service("light", "turn_on", {"entity_id": "light.k"})
    finally:
        await c.close()
    assert route.called
    assert _json.loads(route.calls.last.request.content) == {"entity_id": "light.k"}
    assert out == [{"entity_id": "light.k", "state": "on", "attributes": {"brightness": 5}}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ha.py::test_harestclient_call_service_posts_body_and_returns_changed -v`
Expected: FAIL — `AttributeError: 'HARestClient' object has no attribute 'call_service'`

- [ ] **Step 3: Implement the method**

In `src/nrha_mcp/ha.py`, inside class `HARestClient`, immediately after the `get_services` method:

```python
    async def call_service(self, domain: str, service: str, body: dict) -> list[dict]:
        r = await self._client.post(f"/api/services/{domain}/{service}", json=body)
        r.raise_for_status()
        return r.json()  # array of state objects that changed (often [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ha.py::test_harestclient_call_service_posts_body_and_returns_changed -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat: HARestClient.call_service POSTs to /api/services"
```

---

## Task 4: ha.call_service orchestrator

**Files:**
- Modify: `src/nrha_mcp/ha.py:10` (import `NRError`) and add the `call_service` module function (after `get_entity_state`, around line 123)
- Modify: `tests/test_ha.py` — extend `FakeHAClient` (around line 63) with `call_service`
- Test: `tests/test_ha.py` (append)

- [ ] **Step 1: Extend `FakeHAClient` and write the failing tests**

In `tests/test_ha.py`, change the `FakeHAClient.__init__` signature and body, and add a `call_service` method:

```python
class FakeHAClient:
    """Minimal fake HA REST client for unit tests."""

    def __init__(self, states=None, state_map=None, services=None, history=None, call_result=None):
        self._states = states or FAKE_STATES
        self._state_map = state_map or {s["entity_id"]: s for s in self._states}
        self._services = services if services is not None else FAKE_SERVICES
        self._history = history if history is not None else FAKE_HISTORY
        self._call_result = call_result if call_result is not None else []
        self.calls = []

    async def get_states(self):
        return list(self._states)

    async def get_state(self, entity_id: str):
        if entity_id not in self._state_map:
            raise KeyError(entity_id)
        return self._state_map[entity_id]

    async def get_services(self):
        return list(self._services)

    async def get_history(self, entity_id, start_iso, end_iso=None):
        return [list(self._history)]

    async def call_service(self, domain, service, body):
        self.calls.append((domain, service, body))
        return list(self._call_result)
```

Then append the tests:

```python
# --- ha_call_service: orchestrator ---

@pytest.mark.asyncio
async def test_call_service_direct_executes_and_trims():
    fake = FakeHAClient(call_result=[{"entity_id": "light.k", "state": "on",
                                      "attributes": {"brightness": 5}}])
    out = await ha.call_service(fake, Settings(), "light", "turn_on", entity_id="light.k")
    assert fake.calls == [("light", "turn_on", {"entity_id": "light.k"})]
    assert out["ok"] is True and out["called"] == "light.turn_on"
    assert out["changed"] == [{"entity_id": "light.k", "state": "on"}]
    assert out["changed_count"] == 1 and "note" not in out

@pytest.mark.asyncio
async def test_call_service_empty_changed_adds_note():
    fake = FakeHAClient(call_result=[])
    out = await ha.call_service(fake, Settings(), "script", "test", entity_id="script.test")
    assert out["changed"] == [] and out["changed_count"] == 0
    assert "verify with ha_get_state" in out["note"]

@pytest.mark.asyncio
async def test_call_service_blocked_raises_even_with_confirm():
    fake = FakeHAClient()
    with pytest.raises(ha.NRError):
        await ha.call_service(fake, Settings(), "homeassistant", "restart", confirm=True)
    assert fake.calls == []

@pytest.mark.asyncio
async def test_call_service_confirm_tier_previews_without_confirm():
    fake = FakeHAClient()
    out = await ha.call_service(fake, Settings(), "lock", "unlock", entity_id="lock.porta")
    assert out["preview"] is True and out["requires_confirm"] is True
    assert out["would_call"] == "lock.unlock"
    assert fake.calls == []

@pytest.mark.asyncio
async def test_call_service_confirm_tier_executes_with_confirm():
    fake = FakeHAClient(call_result=[{"entity_id": "lock.porta", "state": "unlocked"}])
    out = await ha.call_service(fake, Settings(), "lock", "unlock",
                                entity_id="lock.porta", confirm=True)
    assert out["ok"] is True
    assert fake.calls == [("lock", "unlock", {"entity_id": "lock.porta"})]

@pytest.mark.asyncio
async def test_call_service_entity_id_collision_raises():
    fake = FakeHAClient()
    with pytest.raises(ha.NRError):
        await ha.call_service(fake, Settings(), "light", "turn_on",
                              entity_id="light.a", data={"entity_id": "light.b"})
    assert fake.calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ha.py -k "test_call_service_" -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.ha' has no attribute 'call_service'` (the `ha.NRError` reference also requires the import added in Step 3)

- [ ] **Step 3: Implement the orchestrator**

In `src/nrha_mcp/ha.py`, change the import on line 10:

```python
from .client import NRClient, NRError
```

Then add this function after `get_entity_state` (around line 123):

```python
async def call_service(ha_client, settings, domain, service,
                       entity_id=None, data=None, confirm=False) -> dict:
    """Call a HA service with tiered safety. Returns a trimmed result, or a preview
    dict when a sensitive service is called without confirm=true."""
    body = dict(data or {})
    if entity_id is not None:
        if "entity_id" in body and body["entity_id"] != entity_id:
            raise NRError(
                f"entity_id conflict: param {entity_id!r} but data has {body['entity_id']!r}")
        body["entity_id"] = entity_id

    called = f"{domain}.{service}"
    tier = _service_tier(domain, service, settings)

    if tier == "blocked":
        raise NRError(
            f"{called} is in the catastrophic block list and cannot be executed; "
            "confirm=true does not override it.")

    if tier == "confirm" and not confirm:
        return {
            "preview": True,
            "would_call": called,
            "entity_id": entity_id,
            "data": dict(data or {}),
            "tier": "sensitive",
            "requires_confirm": True,
            "hint": "re-call with confirm=true to execute.",
        }

    changed = await ha_client.call_service(domain, service, body)
    trimmed = [{"entity_id": c.get("entity_id"), "state": c.get("state")} for c in changed]
    out = {
        "ok": True,
        "called": called,
        "entity_id": entity_id,
        "changed": trimmed,
        "changed_count": len(trimmed),
    }
    if not trimmed:
        out["note"] = "No state change reported synchronously; verify with ha_get_state."
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ha.py -k "test_call_service_" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat: ha.call_service orchestrator (body, tier policy, trimmed return)"
```

---

## Task 5: Register the `ha_call_service` tool

**Files:**
- Modify: `src/nrha_mcp/server.py` — add the tool after the `ha_get_services` definition (after line ~341)
- Modify: `tests/test_server_smoke.py:8-21` — add to `expected`, bump count to 38
- Modify: `scripts/smoke_e2e.py:27` — add to `EXPECTED_TOOLS`

- [ ] **Step 1: Update the smoke test (failing first)**

In `tests/test_server_smoke.py`, add `"ha_call_service"` to the `expected` set (e.g. on the HA line) and change the count assertion:

```python
        "ha_search_entities", "ha_get_state", "ha_get_services", "ha_get_entity_history", "ha_get_entity_statistics",
        "ha_check_nodered_integration", "ha_call_service",
```

```python
    assert len(names) == 38
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `assert 37 == 38` (tool not registered yet)

- [ ] **Step 3: Add the tool**

In `src/nrha_mcp/server.py`, immediately after the `ha_get_services` tool (after its closing `await ha_client.close()`):

```python
@mcp.tool()
async def ha_call_service(domain: str, service: str,
                          entity_id: str | list[str] | None = None,
                          data: dict | None = None, confirm: bool = False) -> str:
    """Call a Home Assistant service (turn on/off, press buttons, nudge covers, set
    helpers). Requires HA_URL + HA_TOKEN.

    `entity_id` targets one or more entities; `data` carries extra params or non-entity
    targets (area_id, etc.). Tiered safety: ordinary services run directly; sensitive
    ones (e.g. lock.unlock, alarm disarm) require confirm=true and otherwise return a
    preview; a small catastrophic set (e.g. homeassistant.restart) is always blocked.
    Use ha_get_services to find the right domain/service/fields."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA call_service needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.call_service(
            ha_client, settings, domain, service,
            entity_id=entity_id, data=data, confirm=confirm))
    finally:
        await ha_client.close()
```

- [ ] **Step 4: Add the tool to the e2e expected set**

In `scripts/smoke_e2e.py`, inside `EXPECTED_TOOLS`, add a line:

```python
    "ha_call_service",
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat: register ha_call_service tool (37 -> 38)"
```

---

## Task 6: Surface the effective policy in diagnostics

**Files:**
- Modify: `src/nrha_mcp/diagnostics.py:59` (after the `home_assistant` block, before `return out`)
- Test: `tests/test_diagnostics.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diagnostics.py`:

```python
@pytest.mark.asyncio
async def test_diagnostics_reports_call_policy(fake_client):
    settings = Settings(ha_url="", ha_token="")
    res = await diagnostics.run(fake_client, settings)
    pol = res["ha_call_policy"]
    assert "homeassistant.restart" in pol["blocked"]
    assert "lock.unlock" in pol["confirm"]
    assert pol["confirm_source"] == "default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_diagnostics.py::test_diagnostics_reports_call_policy -v`
Expected: FAIL — `KeyError: 'ha_call_policy'`

- [ ] **Step 3: Wire the policy into the snapshot**

In `src/nrha_mcp/diagnostics.py`, add this line just before `return out` (after `out["home_assistant"] = ha_block`):

```python
    out["ha_call_policy"] = ha.effective_policy(settings)
```

(`ha` is already imported at the top of `diagnostics.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_diagnostics.py::test_diagnostics_reports_call_policy -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: report effective ha_call_service policy in nr_diagnostics"
```

---

## Task 7: Full suite + docs

**Files:**
- Modify: `README.md` and `.env.example` (if present) — document the two env vars
- Test: whole suite

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, no failures. (Confirms no regressions across the 38-tool surface.)

- [ ] **Step 2: Document the env vars**

Add to `README.md` (HA configuration section) and `.env.example` if it exists:

```
# HA service-call safety (ha_call_service)
# Additive to the hardcoded catastrophic floor (homeassistant.stop/restart,
# hassio host/supervisor reboot+restore, recorder.purge*). csv of domain.service / domain.*
HA_BLOCK_SERVICES=
# Services requiring confirm=true. Unset = default (lock.unlock/open,
# alarm disarm/trigger, update.install). Empty string = gate nothing. csv of domain.service / domain.*
HA_CONFIRM_SERVICES=
```

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example
git commit -m "docs: document HA_BLOCK_SERVICES / HA_CONFIRM_SERVICES"
```

- [ ] **Step 4 (manual, optional): E2E over real stdio transport**

The unit smoke test already asserts registration. The full e2e needs Docker and does
not exercise a real HA call (the tool no-ops without creds):

```bash
docker compose up -d nodered
NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py
docker compose down -v
```

Expected: `OK: 38 tools registered, all 38 expected present.`

---

## Deployment (after merge)

Rebuild and redeploy the Docker image so the running MCP exposes the tool live; the
running server must have `HA_URL`/`HA_TOKEN` set, with an HA token permitted to call
the targeted services. The session-attached MCP is currently an older build (it lacks
`ha_get_services`), so a rebuild is required for the agent to see `ha_call_service`.

## Self-Review notes

- **Spec coverage:** §A→Task 3; §B→Task 4; §C→Task 2; §D→Task 5; §E→Task 1; §F→Task 6; error handling→Tasks 3-4 (NRError raises, `_wrap` mapping) + smoke; security limitation→docstring/spec (no code); testing→Tasks 2-6.
- **Type consistency:** `_service_tier`/`_block_set`/`_confirm_set`/`_in_set`/`_parse_service_list`/`effective_policy`/`call_service` names used identically across tasks. `FakeHAClient.call_service(domain, service, body)` matches `HARestClient.call_service(domain, service, body)` and the orchestrator's call `ha_client.call_service(domain, service, body)`.
- **`return_response` / `HA_BLOCK_OVERRIDE` / `verbose`** are spec follow-ups, intentionally not in this plan.
