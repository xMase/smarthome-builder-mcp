# HA Entity History for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/ha-entity-history`

## Problem

An agent often needs to understand how an HA entity has behaved recently — e.g.
"when did `sensor.ri_salotto_rainwater` last report a value / go unavailable?",
which is exactly the situation that motivated this whole effort. The MCP can read
the *current* state (`ha_get_state`) but not the recent history.

## Goals

- An `ha_get_entity_history` tool that returns the recent state-change history of
  one entity over the last N hours, with bounded output and an honest note about
  Home Assistant's retention limit.

## Non-goals

- **Wide-span aggregate comparison (weeks/months/year).** HA's `recorder` keeps
  raw history only for a limited period (default ~10 days); long ranges live in
  **Long-Term Statistics**, accessed via a different (WebSocket) API. That is a
  separate future tool, `ha_get_entity_statistics`. This tool stays recent +
  fine-grained and is *honest* about the retention limit rather than returning a
  misleading partial slice.
- Returning per-state attributes (we use `no_attributes`).

## Design

### A. Client method — `ha.HARestClient`

```python
async def get_history(self, entity_id: str, start_iso: str, end_iso: str | None = None) -> list:
    params = {"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""}
    if end_iso:
        params["end_time"] = end_iso
    r = await self._client.get(f"/api/history/period/{start_iso}", params=params)
    r.raise_for_status()
    return r.json()
```

`minimal_response` + `no_attributes` keep each point to roughly `{entity_id,
state, last_changed, last_updated}`. The response is a list with one inner list
per entity (`[[ {…}, … ]]`); filtered to one entity it has at most one inner list.

### B. Module function — `ha.get_entity_history(ha_client, entity_id, hours=24, max_points=200)`

- `start = now(UTC) - hours`; call `get_history(entity_id, start.isoformat())`.
- Take the first inner series (`data[0]` if `data` else `[]`); normalize each
  point to `{"state": p.get("state"), "last_changed": p.get("last_changed") or p.get("last_updated")}`.
- `total_changes = len(points)`. If it exceeds `max_points`, keep the **most
  recent** `max_points` (`points[-max_points:]`) and set `truncated: true` + a note.
- Return:

  ```json
  {"entity_id": "...", "hours": 24, "total_changes": 37,
   "points": [{"state": "0", "last_changed": "..."}, ...],
   "first": {"state": "...", "last_changed": "..."},
   "last":  {"state": "...", "last_changed": "..."}}
  ```

- **Honesty about retention** (`note` field):
  - no points → `note`: "No history in the window. HA keeps raw history for a
    limited period (recorder default ~10 days); for wider spans use long-term
    statistics."
  - else if `hours > 240` (beyond the typical ~10-day retention) → same note
    (the window likely exceeds what HA retains as raw history).
  - truncation note (when applicable) is separate: "Showing the most recent
    {max_points} of {total} state changes."

### C. Tool — `server.py`

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

Same env-guard + create-use-close pattern as the other HA tools. `ha` is already
imported. **Tool count goes 27 → 28.** Update both hardcoded sites:
`tests/test_server_smoke.py` (`expected` + `len(names) == 28`) and
`scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

HA REST errors propagate via `raise_for_status()` → `_wrap`. Missing
`HA_URL`/`HA_TOKEN` returns `_err` and never opens a client.

## Testing

`tests/test_ha.py` (extend) — `FakeHAClient` gains a `get_history(entity_id,
start_iso, end_iso=None)` method (configurable via a `history` param) returning
`[list-of-points]`:
- a normal series → `points`, `first`, `last`, `total_changes` correct; no note.
- `> max_points` points → `truncated: true`, `points` length == `max_points`, and
  the kept points are the most recent.
- empty series → `note` mentioning retention.
- large `hours` (e.g. 9000) → `note` mentioning retention/statistics.

`tests/test_server_smoke.py`: count 28; `ha_get_entity_history` registered.

`scripts/smoke_e2e.py`: add `"ha_get_entity_history"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild and redeploy the Docker image; the running MCP needs `HA_URL`/`HA_TOKEN`.
Local `.venv` is for tests.

## Follow-ups

- **`ha_get_entity_statistics`** (new feature): wide-span comparison via HA
  Long-Term Statistics (`recorder/statistics_during_period` over the HA WebSocket
  API) returning hourly/daily aggregates (mean/min/max). This is the right tool
  for month/year ranges and is tracked as the next HA item.
