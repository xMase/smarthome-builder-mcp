# HA Long-Term Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ha_get_entity_statistics` tool that returns Home Assistant Long-Term Statistics (downsampled mean/min/max buckets) for one entity over the last N days, via the HA WebSocket API.

**Architecture:** A new `ha_stats.py` module owns the HA WebSocket transport (auth handshake + `recorder/statistics_during_period`), behind an injectable `connect` for testing (mirrors `runtime.py`). `get_entity_statistics` normalizes buckets (epoch-ms → ISO) and bounds output. Exposed as `ha_get_entity_statistics`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), `websockets`, pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-ha-statistics-design.md`

---

## File structure

- Create `src/nrha_mcp/ha_stats.py` — `_ws_url`, `_ms_to_iso`, `_HAWsConn`, `_default_connect`, `HAStatsError`, `_query_statistics`, `get_entity_statistics`.
- Modify `src/nrha_mcp/server.py` — import `ha_stats`; add the `ha_get_entity_statistics` tool.
- Create `tests/test_ha_stats.py`.
- Modify `tests/test_server_smoke.py` — tool count 29 → 30.
- Modify `scripts/smoke_e2e.py` — add the tool to `EXPECTED_TOOLS`.

All commands from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv venv, no `pip`; `websockets` already installed). Branch: `feat/ha-statistics`.

---

## Task 1: `ha_stats` module

**Files:**
- Create: `src/nrha_mcp/ha_stats.py`
- Test: `tests/test_ha_stats.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ha_stats.py`:

```python
import collections

import pytest

from nrha_mcp import ha_stats
from nrha_mcp.ha_stats import HAStatsError
from nrha_mcp.config import Settings


class FakeHAWsConn:
    """In-memory HA WebSocket: yields scripted frames, records sent messages."""

    def __init__(self, recv_frames):
        self._recv = collections.deque(recv_frames)
        self.sent = []
        self.closed = False

    async def send(self, obj):
        self.sent.append(obj)

    async def recv(self):
        return self._recv.popleft()

    async def close(self):
        self.closed = True


def _connect_factory(conn):
    async def _connect(settings):
        return conn
    return _connect


def _bucket(start_ms, mean, mn, mx):
    return {"start": start_ms, "end": start_ms + 1000, "mean": mean, "min": mn, "max": mx}


def _result_frame(entity, buckets):
    return {"id": 1, "type": "result", "success": True, "result": {entity: buckets}}


def _settings():
    return Settings(ha_url="http://ha:8123", ha_token="tok")


def test_ws_url():
    assert ha_stats._ws_url("http://ha:8123") == "ws://ha:8123/api/websocket"
    assert ha_stats._ws_url("https://ha:8123/") == "wss://ha:8123/api/websocket"


@pytest.mark.asyncio
async def test_statistics_normalizes_buckets():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _result_frame("sensor.x", [_bucket(1746050400000, 0.13, 0.0, 13.3),
                                         _bucket(1748728800000, 0.02, 0.0, 2.7)])]
    conn = FakeHAWsConn(frames)
    res = await ha_stats.get_entity_statistics(
        _settings(), "sensor.x", days=365, period="month", connect=_connect_factory(conn))
    assert res["total_buckets"] == 2
    assert res["period"] == "month"
    assert res["buckets"][0]["start"].startswith("2025")   # epoch-ms -> ISO
    assert res["buckets"][0]["mean"] == 0.13
    assert res["first"]["max"] == 13.3
    assert res["last"]["max"] == 2.7
    assert {"type": "auth", "access_token": "tok"} in conn.sent
    cmd = next(m for m in conn.sent if m.get("type") == "recorder/statistics_during_period")
    assert cmd["statistic_ids"] == ["sensor.x"]
    assert cmd["period"] == "month"
    assert conn.closed is True


@pytest.mark.asyncio
async def test_statistics_truncates_to_recent():
    many = [_bucket(1700000000000 + i * 1000, i, i, i) for i in range(60)]
    frames = [{"type": "auth_required"}, {"type": "auth_ok"}, _result_frame("sensor.x", many)]
    conn = FakeHAWsConn(frames)
    res = await ha_stats.get_entity_statistics(
        _settings(), "sensor.x", max_buckets=10, connect=_connect_factory(conn))
    assert res["total_buckets"] == 60
    assert res["truncated"] is True
    assert len(res["buckets"]) == 10
    assert res["buckets"][-1]["mean"] == 59
    assert res["first"]["mean"] == 0     # first/last from the FULL series
    assert res["last"]["mean"] == 59


@pytest.mark.asyncio
async def test_statistics_empty_has_note():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"}, _result_frame("sensor.x", [])]
    conn = FakeHAWsConn(frames)
    res = await ha_stats.get_entity_statistics(
        _settings(), "sensor.x", connect=_connect_factory(conn))
    assert res["total_buckets"] == 0
    assert res["buckets"] == []
    assert "note" in res


@pytest.mark.asyncio
async def test_statistics_auth_invalid_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_invalid"}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAStatsError):
        await ha_stats.get_entity_statistics(
            _settings(), "sensor.x", connect=_connect_factory(conn))
    assert conn.closed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_ha_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nrha_mcp.ha_stats'`

- [ ] **Step 3: Implement the module**

Create `src/nrha_mcp/ha_stats.py`:

```python
"""Home Assistant Long-Term Statistics over the HA WebSocket API.

LTS is not on the REST API; it is served by `recorder/statistics_during_period`
over ws(s)://<ha>/api/websocket. The transport sits behind an injectable `connect`
so tests feed frames without a live server (mirrors runtime.py).
"""
import json
from datetime import datetime, timedelta, timezone


class HAStatsError(Exception):
    """HA WebSocket auth or statistics-command failure."""


def _ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https"):
        base = "wss" + base[len("https"):]
    elif base.startswith("http"):
        base = "ws" + base[len("http"):]
    return base + "/api/websocket"


def _ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


class _HAWsConn:
    """Adapter over a `websockets` connection (json on the wire)."""

    def __init__(self, ws):
        self._ws = ws

    async def send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    async def recv(self) -> dict:
        return json.loads(await self._ws.recv())

    async def close(self) -> None:
        await self._ws.close()


async def _default_connect(settings):
    import websockets  # lazy import so unit tests need no network lib

    ws = await websockets.connect(_ws_url(settings.ha_url))
    return _HAWsConn(ws)


async def _query_statistics(conn, token, *, statistic_id, start_iso, period, types) -> dict:
    await conn.recv()  # auth_required greeting
    await conn.send({"type": "auth", "access_token": token})
    auth = await conn.recv()
    if auth.get("type") != "auth_ok":
        raise HAStatsError(f"HA WebSocket auth failed: {auth.get('type')}")
    await conn.send({
        "id": 1,
        "type": "recorder/statistics_during_period",
        "start_time": start_iso,
        "statistic_ids": [statistic_id],
        "period": period,
        "types": list(types),
    })
    while True:
        msg = await conn.recv()
        if msg.get("id") == 1 and msg.get("type") == "result":
            if not msg.get("success"):
                raise HAStatsError(f"statistics query failed: {msg.get('error')}")
            return msg.get("result", {})


async def get_entity_statistics(settings, entity_id, *, days: int = 30, period: str = "day",
                                max_buckets: int = 200, connect=None) -> dict:
    """Long-term statistics (mean/min/max buckets) for one entity over the last `days`."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    conn = await (connect or _default_connect)(settings)
    try:
        result = await _query_statistics(
            conn, settings.ha_token, statistic_id=entity_id,
            start_iso=start.isoformat(), period=period, types=("mean", "min", "max"))
    finally:
        await conn.close()
    series = result.get(entity_id, []) or []
    buckets = [{"start": _ms_to_iso(b.get("start")), "mean": b.get("mean"),
                "min": b.get("min"), "max": b.get("max")} for b in series]
    total = len(buckets)
    kept = buckets[-max_buckets:] if total > max_buckets else buckets
    out = {
        "entity_id": entity_id,
        "period": period,
        "days": days,
        "total_buckets": total,
        "buckets": kept,
        "first": buckets[0] if buckets else None,
        "last": buckets[-1] if buckets else None,
    }
    if total > max_buckets:
        out["truncated"] = True
        out["truncated_note"] = f"Showing the most recent {max_buckets} of {total} buckets."
    if not buckets:
        out["note"] = ("No long-term statistics for this entity in the window — it may "
                       "lack a numeric state_class, or have no data in the range.")
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_ha_stats.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha_stats.py tests/test_ha_stats.py
git commit -m "feat(ha_stats): long-term statistics over the HA WebSocket"
```

---

## Task 2: Register the `ha_get_entity_statistics` tool

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"ha_get_entity_statistics",` to the `expected` set (e.g. on the HA line, after `"ha_get_entity_history",`) and change the count. The HA line becomes:

```python
        "ha_search_entities", "ha_get_state", "ha_get_services", "ha_get_entity_history", "ha_get_entity_statistics",
```

and:

```python
    assert len(names) == 30
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `ha_get_entity_statistics` not registered; `len(names)` is 29, not 30.

- [ ] **Step 3: Import `ha_stats` in `server.py`**

In `src/nrha_mcp/server.py`, change:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics
```

to:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics, ha_stats
```

- [ ] **Step 4: Add the tool**

In `src/nrha_mcp/server.py`, immediately AFTER the `nr_diagnostics` function, add:

```python


@mcp.tool()
async def ha_get_entity_statistics(entity_id: str, days: int = 30,
                                   period: str = "day", max_buckets: int = 200) -> str:
    """Long-term statistics (downsampled mean/min/max) for a Home Assistant entity.
    Requires HA_URL + HA_TOKEN.

    Returns up to `max_buckets` aggregate buckets over the last `days`, bucketed by
    `period` ("hour"|"day"|"week"|"month"). This is the tool for WIDE spans
    (weeks/months/year) where raw history (ha_get_entity_history) is retention-limited."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA get_entity_statistics needs HA_URL and HA_TOKEN to be set.")
    return await _wrap(lambda: ha_stats.get_entity_statistics(
        settings, entity_id, days=days, period=period, max_buckets=max_buckets))
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 6: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"ha_get_entity_statistics",` to the `EXPECTED_TOOLS` set (e.g. right after `"ha_get_entity_history",`). The script asserts `len(EXPECTED_TOOLS)` tools are present.

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add ha_get_entity_statistics tool"
```

---

## Task 3: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('stats:', 'ha_get_entity_statistics' in n); print('count:', len(n))"`
Expected: `stats: True` and `count: 30`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack). The running MCP needs `HA_URL`/`HA_TOKEN` and network access to the HA WebSocket endpoint. Do it when ready.

---

## Self-review

- **Spec coverage:** WS transport (`_ws_url`, `_HAWsConn`, `_default_connect`, injectable `connect`) → Task 1; auth handshake + command + result parsing (`_query_statistics`, `HAStatsError`) → Task 1; epoch-ms→ISO + bounded buckets + first/last/total + note (`get_entity_statistics`, `_ms_to_iso`) → Task 1; the four behaviors (normalize, truncate, empty→note, auth_invalid→raise) + `_ws_url` → Task 1 tests; tool + env guard + import + 29→30 in both sites → Task 2; verify + deploy → Task 3; parameters (days/period default day/fixed types) → Task 1 signature + tests. No gaps.
- **Placeholders:** none — full code and exact commands throughout.
- **Type consistency:** `get_entity_statistics(settings, entity_id, *, days, period, max_buckets, connect)` identical in Task 1 (def), its tests, and the Task 2 `server.py` wrapper. `FakeHAWsConn.send/recv/close` matches `_HAWsConn`'s contract used by `_query_statistics`. Output keys (`entity_id`/`period`/`days`/`total_buckets`/`buckets`/`first`/`last`/optional `truncated`/`truncated_note`/`note`) consistent between module, tests, and tool docstring. Tool count 30 consistent across Task 2 and Task 3.
