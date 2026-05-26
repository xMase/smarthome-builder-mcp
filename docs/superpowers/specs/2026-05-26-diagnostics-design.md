# Diagnostics / Healthcheck for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/diagnostics`

## Problem

When something is off, an agent has no single place to check the basics: is
Node-RED reachable and authenticated? what version? what context **store names**
exist (the `default`-vs-`memory` mismatch that caused the original loop would have
been obvious here)? is the runtime started? is Home Assistant configured and is the
token actually working? Today these require several disparate calls or are not
exposed at all.

## Goals

- A single `nr_diagnostics` tool returning a read-only health snapshot:
  Node-RED reachability/version/runtime-state + context store names, flow counts,
  installed node-set count, MCP config, and Home Assistant configured/reachable.

## Non-goals

- Changing any state (purely read-only).
- Deep per-subsystem diagnostics; this is a quick snapshot.

## Design

Confirmed via a live probe of `GET /settings`: it returns `version` (e.g.
`4.1.10`), `runtimeState`, and `context: {"default": "default", "stores":
["store", "default"]}` — the authoritative source for context store names.

### A. Client method — `NRClient.get_settings()`

```python
async def get_settings(self) -> dict:
    r = await self._do("GET", "/settings", headers={"Accept": "application/json"})
    self._check_response(r)
    return r.json()
```

### B. New module — `src/nrha_mcp/diagnostics.py`

```python
async def run(client, settings, *, ha_ping=None) -> dict:
    ...
```

Gathers, with each external read guarded so one failure doesn't sink the rest:

- **node_red**: from `get_settings()` — `reachable: true`, `version`,
  `runtime_state` (the `runtimeState` field), `context_stores`
  (`context.stores`), `context_default` (`context.default`). If `get_settings`
  raises `NRError`, set `{"reachable": false, "error": str(e)}` and return early
  (nothing else is meaningful when NR is down).
- **flows**: `{"tabs": <count of type==tab>, "total_objects": len(flows)}` from
  `get_flows()`.
- **node_sets**: `len(nodes)` from `get_nodes()` (best-effort; `null` on error).
- **config**: `{"deploy_mode": settings.deploy_mode, "backup_dir": settings.backup_dir}`.
- **home_assistant**: `{"configured": bool(settings.ha_url and settings.ha_token)}`;
  if configured, an active light ping (`GET /api/` via `HARestClient`) sets
  `reachable: true` or `{"reachable": false, "error": <ExceptionName>}`. The ping
  is injectable as `ha_ping` (an async callable taking `settings`); the default
  (`_default_ha_ping`) creates an `HARestClient`, calls a new lightweight
  `ping()`, and closes it.

Return one dict with these blocks.

### C. Client method — `ha.HARestClient.ping()`

```python
async def ping(self) -> dict:
    r = await self._client.get("/api/")
    r.raise_for_status()
    return r.json()
```

`GET /api/` returns `{"message": "API running."}` and validates the token — a
cheap reachability check (vs. fetching all states).

### D. Tool — `server.py`

```python
@mcp.tool()
async def nr_diagnostics() -> str:
    """Read-only health snapshot: Node-RED reachability/version/runtime-state,
    context store names, flow counts, installed node sets, MCP config, and Home
    Assistant configured/reachable. Run it first when something seems off."""
    return await _wrap(lambda: diagnostics.run(client, settings))
```

Add `diagnostics` to the `from . import ...` line. **Tool count goes 28 → 29.**
Update both hardcoded sites: `tests/test_server_smoke.py` (`expected` +
`len(names) == 29`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

Each external read is individually guarded so a partial failure still returns a
useful snapshot. NR unreachable → `node_red.reachable: false` + early return. HA
ping failure → `home_assistant.reachable: false` + error name (never raises). The
tool itself is wrapped by `_wrap`.

## Testing

`tests/test_diagnostics.py` (new) — `diagnostics.run` with `FakeNRClient`:
- `FakeNRClient` gains `get_settings()` returning a sample payload with
  `version`, `runtimeState`, and `context: {"default": "default", "stores":
  ["store", "default"]}`.
- full snapshot (no HA configured) → `node_red.reachable` true, `version`,
  `context_stores == ["store", "default"]`, `flows.tabs`, `config.deploy_mode`,
  `home_assistant == {"configured": false}`.
- NR unreachable: make `get_settings` raise `NRError` → `node_red.reachable
  false` and no other blocks required.
- HA configured + injected `ha_ping` that succeeds → `home_assistant.reachable
  true`; injected `ha_ping` that raises → `reachable false` + `error`.

`tests/conftest.py`: add `get_settings` (and a settable `settings_payload`) to
`FakeNRClient`.

`tests/test_server_smoke.py`: count 29; `nr_diagnostics` registered.

`scripts/smoke_e2e.py`: add `"nr_diagnostics"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild and redeploy the Docker image to expose the tool live. Local `.venv` is
for tests.

## Follow-ups

- None required.
