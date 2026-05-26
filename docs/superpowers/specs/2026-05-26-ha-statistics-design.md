# HA Long-Term Statistics for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/ha-statistics`

## Problem

`ha_get_entity_history` (feature #5) covers recent fine-grained history but, by
design, cannot cover wide spans — Home Assistant's recorder keeps raw history only
~10 days. For "compare this sensor over months / a year" an agent needs Home
Assistant's **Long-Term Statistics** (downsampled aggregates kept for years), which
are exposed only over the HA **WebSocket** API, not REST.

## Goals

- An `ha_get_entity_statistics` tool returning downsampled aggregate buckets
  (mean/min/max per hour/day/week/month) for one entity over the last N days —
  the right tool for wide-span comparison.

## Non-goals

- Raw fine-grained history (that is `ha_get_entity_history`).
- `sum`/`state` statistic types (mean/min/max cover sensor comparison; `sum` is for
  metered/energy entities — out of scope for v1).
- Multiple entities per call (one `entity_id`; keep it simple).

## Design

Confirmed via a live WS probe against `sensor.openweathermap_rain`:

- WS endpoint `ws(s)://<ha_host>/api/websocket`.
- Handshake: server sends `{"type": "auth_required", ...}`; client sends
  `{"type": "auth", "access_token": "<token>"}`; server replies
  `{"type": "auth_ok"}` (or `auth_invalid`).
- Command: `{"id": 1, "type": "recorder/statistics_during_period",
  "start_time": "<iso>", "statistic_ids": ["<entity>"], "period": "day",
  "types": ["mean", "min", "max"]}`.
- Reply: `{"id": 1, "type": "result", "success": true, "result":
  {"<entity>": [{"start": <epoch_ms>, "end": <epoch_ms>, "mean", "min", "max"}, ...]}}`.
  **`start`/`end` are epoch milliseconds**, converted to ISO for output.

### A. New module — `src/nrha_mcp/ha_stats.py`

WebSocket transport kept separate from the REST `HARestClient` in `ha.py`, with an
injectable connector for testability (mirrors `runtime.py`).

- `_ws_url(ha_url)`: `http→ws`, `https→wss`, strip trailing `/`, append
  `/api/websocket`.
- `_HAWsConn`: thin adapter over a `websockets` connection (`send(obj)` json-dumps,
  `recv()` json-loads, `close()`).
- `_default_connect(settings)`: lazily `import websockets`, connect, return `_HAWsConn`.
- `_query_statistics(conn, token, *, statistic_id, start_iso, period, types) -> dict`:
  await `auth_required`; send auth; require `auth_ok` (else raise); send the
  command with `id=1`; read frames until one with `id==1` and `type=="result"`;
  raise on `success: false`; return its `result`.
- `_ms_to_iso(ms)`: `datetime.fromtimestamp(ms/1000, tz=UTC).isoformat()`, or `None`.
- `get_entity_statistics(settings, entity_id, *, days=30, period="day", max_buckets=200, connect=None) -> dict`:
  - `start = now(UTC) - days`; open conn via `connect or _default_connect`;
    query; **always** `await conn.close()` in `finally`.
  - `series = result.get(entity_id, [])`; normalize each bucket to
    `{"start": _ms_to_iso(b["start"]), "mean", "min", "max"}`.
  - keep the most-recent `max_buckets` (`truncated: true` + note when exceeded);
    `first`/`last` from the full list; `total_buckets`.
  - return `{entity_id, period, days, total_buckets, buckets, first, last}`; if no
    buckets, add `note`: "No long-term statistics for this entity in the window —
    it may lack a numeric state_class, or have no data in the range."

### B. Tool — `server.py`

```python
@mcp.tool()
async def ha_get_entity_statistics(entity_id: str, days: int = 30,
                                   period: str = "day", max_buckets: int = 200) -> str:
    """Long-term statistics (downsampled mean/min/max) for an HA entity. Requires HA_URL + HA_TOKEN.

    Returns up to `max_buckets` aggregate buckets over the last `days`, bucketed by
    `period` ("hour"|"day"|"week"|"month"). This is the tool for WIDE spans
    (weeks/months/year) where raw history (ha_get_entity_history) is retention-limited."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA get_entity_statistics needs HA_URL and HA_TOKEN to be set.")
    return await _wrap(lambda: ha_stats.get_entity_statistics(
        settings, entity_id, days=days, period=period, max_buckets=max_buckets))
```

Add `ha_stats` to the `from . import ...` line. The WS connection is managed inside
`get_entity_statistics` (no `HARestClient` here). **Tool count goes 29 → 30.**
Update both hardcoded sites: `tests/test_server_smoke.py` (`expected` +
`len(names) == 30`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

### C. Parameters

- **Window:** `days` back (agent-friendly, consistent with #5's `hours`). For a
  year, `days=365`.
- **`period`:** default `"day"`; also `"hour"|"week"|"month"` (month is best for a
  year). Passed through to HA.
- **Types:** fixed `mean/min/max` (no parameter).

## Error handling

WS connect/auth failure or a `success: false` result raises a clear exception →
`_wrap` returns `{"error": ...}`. Missing `HA_URL`/`HA_TOKEN` returns `_err` and
never opens a socket. The socket is always closed (`finally`).

## Testing

`tests/test_ha_stats.py` (new) — drive `get_entity_statistics` with an injected
`connect` returning a `FakeHAWsConn` that scripts the handshake + a result frame
(no network; `websockets` not imported in tests):
- normal: buckets normalized (`start` is ISO, `mean/min/max` present), `first`/
  `last`/`total_buckets`; the sent frames include the `auth` message and the
  `recorder/statistics_during_period` command with the right `statistic_ids`/`period`.
- `> max_buckets` → `truncated: true`, kept length == `max_buckets`, most recent.
- empty result (`result == {}` or empty list) → `note`.
- `auth_invalid` handshake → raises.

`_ws_url` unit test: http→ws, https→wss, trailing slash → `/api/websocket`.

`tests/test_server_smoke.py`: count 30; `ha_get_entity_statistics` registered.

`scripts/smoke_e2e.py`: add `"ha_get_entity_statistics"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild and redeploy the Docker image; the running MCP needs `HA_URL`/`HA_TOKEN`
and network access to the HA WebSocket endpoint. Local `.venv` is for tests.

## Follow-ups

- A `sum`/state-type variant for metered/energy entities, if needed.
