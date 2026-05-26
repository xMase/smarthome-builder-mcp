# HA Entity History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ha_get_entity_history` tool that returns the recent state-change history of one HA entity over the last N hours, bounded and honest about HA's retention limit.

**Architecture:** Add `HARestClient.get_history()` (GET `/api/history/period`) and `ha.get_entity_history(ha_client, entity_id, hours=24, max_points=200)` that normalizes points, keeps the most-recent `max_points`, summarizes (first/last/total), and adds a retention `note` when empty or when the window is wide. Expose as `ha_get_entity_history`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), httpx, pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-ha-entity-history-design.md`

---

## File structure

- Modify `src/nrha_mcp/ha.py` — add a datetime import, `HARestClient.get_history()`, a `_RETENTION_NOTE` constant, and `get_entity_history(...)`.
- Modify `src/nrha_mcp/server.py` — add the `ha_get_entity_history` tool.
- Modify `tests/test_ha.py` — `FAKE_HISTORY`, `FakeHAClient.get_history`, and tests.
- Modify `tests/test_server_smoke.py` — tool count 27 → 28.
- Modify `scripts/smoke_e2e.py` — add the tool to `EXPECTED_TOOLS`.

All commands from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv venv, no `pip`). Branch: `feat/ha-entity-history`.

---

## Task 1: `ha.get_entity_history` + client method

**Files:**
- Modify: `src/nrha_mcp/ha.py`
- Test: `tests/test_ha.py`

- [ ] **Step 1: Add fixtures + failing tests**

In `tests/test_ha.py`, add a `FAKE_HISTORY` constant after the existing `FAKE_SERVICES` constant:

```python
FAKE_HISTORY = [
    {"entity_id": "sensor.x", "state": "0", "last_changed": "2026-05-26T08:00:00+00:00"},
    {"entity_id": "sensor.x", "state": "1.2", "last_changed": "2026-05-26T09:00:00+00:00"},
    {"entity_id": "sensor.x", "state": "0", "last_changed": "2026-05-26T10:00:00+00:00"},
]
```

Extend `FakeHAClient` to accept `history` and add a `get_history` method. The class becomes:

```python
class FakeHAClient:
    """Minimal fake HA REST client for unit tests."""

    def __init__(self, states=None, state_map=None, services=None, history=None):
        self._states = states or FAKE_STATES
        self._state_map = state_map or {s["entity_id"]: s for s in self._states}
        self._services = services if services is not None else FAKE_SERVICES
        self._history = history if history is not None else FAKE_HISTORY

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
```

Add these tests at the END of `tests/test_ha.py`:

```python
@pytest.mark.asyncio
async def test_get_entity_history_basic():
    fake = FakeHAClient()
    res = await ha.get_entity_history(fake, "sensor.x", hours=24)
    assert res["total_changes"] == 3
    assert res["first"]["state"] == "0"
    assert res["last"]["state"] == "0"
    assert len(res["points"]) == 3
    assert "note" not in res


@pytest.mark.asyncio
async def test_get_entity_history_truncates_to_recent():
    many = [{"state": str(i), "last_changed": f"2026-05-26T00:{i:02d}:00+00:00"} for i in range(60)]
    fake = FakeHAClient(history=many)
    res = await ha.get_entity_history(fake, "sensor.x", hours=24, max_points=10)
    assert res["total_changes"] == 60
    assert res["truncated"] is True
    assert len(res["points"]) == 10
    assert res["points"][-1]["state"] == "59"   # most recent kept
    assert res["points"][0]["state"] == "50"


@pytest.mark.asyncio
async def test_get_entity_history_empty_has_note():
    fake = FakeHAClient(history=[])
    res = await ha.get_entity_history(fake, "sensor.x", hours=24)
    assert res["total_changes"] == 0
    assert res["points"] == []
    assert res["first"] is None
    assert "note" in res


@pytest.mark.asyncio
async def test_get_entity_history_large_window_has_note():
    fake = FakeHAClient()
    res = await ha.get_entity_history(fake, "sensor.x", hours=9000)
    assert "note" in res
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_ha.py -k "entity_history" -v`
Expected: FAIL with `AttributeError: module 'nrha_mcp.ha' has no attribute 'get_entity_history'`

- [ ] **Step 3: Add the datetime import**

In `src/nrha_mcp/ha.py`, add at the top with the other imports (after `import httpx`):

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 4: Add the client method**

In `src/nrha_mcp/ha.py`, in the `HARestClient` class, add this method right after `get_services` (before `close`):

```python
    async def get_history(self, entity_id: str, start_iso: str, end_iso: str | None = None) -> list:
        params = {"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""}
        if end_iso:
            params["end_time"] = end_iso
        r = await self._client.get(f"/api/history/period/{start_iso}", params=params)
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 5: Add the module function**

In `src/nrha_mcp/ha.py`, append at the END of the file:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_ha.py -v`
Expected: all pass (existing HA tests + the 4 new ones).

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat(ha): get_entity_history (recent, bounded, retention-honest)"
```

---

## Task 2: Register the `ha_get_entity_history` tool

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"ha_get_entity_history",` to the `expected` set (e.g. on the HA line, after `"ha_get_services",`) and change the count. The HA line becomes:

```python
        "ha_search_entities", "ha_get_state", "ha_get_services", "ha_get_entity_history",
```

and:

```python
    assert len(names) == 28
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `ha_get_entity_history` not registered; `len(names)` is 27, not 28.

- [ ] **Step 3: Add the tool**

In `src/nrha_mcp/server.py`, immediately AFTER the `ha_get_services` function (after its `finally:` block), add:

```python


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
```

- [ ] **Step 4: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 5: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"ha_get_entity_history",` to the `EXPECTED_TOOLS` set (e.g. right after `"ha_get_services",`). The script asserts `len(EXPECTED_TOOLS)` tools are present.

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add ha_get_entity_history tool"
```

---

## Task 3: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('history:', 'ha_get_entity_history' in n); print('count:', len(n))"`
Expected: `history: True` and `count: 28`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack). The running MCP needs `HA_URL`/`HA_TOKEN`. Do it when ready.

---

## Follow-up (deferred, per spec)

- `ha_get_entity_statistics`: wide-span comparison via HA Long-Term Statistics (`recorder/statistics_during_period` over the HA WebSocket API) — the right tool for month/year ranges.

---

## Self-review

- **Spec coverage:** client method (`HARestClient.get_history`) → Task 1 Step 4; function with normalize + most-recent `max_points` + first/last/total + retention note → Task 1 Step 5; honesty note (empty or `hours>240`) → Task 1 (covered by `test_..._empty_has_note` and `test_..._large_window_has_note`); truncation → `test_..._truncates_to_recent`; tool + env guard + 27→28 in both sites → Task 2; error handling via `raise_for_status`/`_wrap` (inherited); verify + deploy → Task 3; statistics → Follow-up. No gaps.
- **Placeholders:** none — full code and exact commands throughout.
- **Type consistency:** `get_entity_history(ha_client, entity_id, hours=24, max_points=200)` signature identical in Task 1 (def), its tests, and the Task 2 `server.py` wrapper. `FakeHAClient.get_history(entity_id, start_iso, end_iso=None)` matches `HARestClient.get_history`. Output keys (`entity_id`/`hours`/`total_changes`/`points`/`first`/`last`/optional `truncated`/`truncated_note`/`note`) consistent between function, tests, and tool docstring. Tool count 28 consistent across Task 2 and Task 3.
