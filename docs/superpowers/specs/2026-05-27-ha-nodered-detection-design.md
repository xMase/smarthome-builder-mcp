# HA "Node-RED Companion" integration detection for the nrha MCP — Design

- **Date:** 2026-05-27
- **Status:** Approved (pending spec review)
- **Branch:** `feat/ha-nodered-detection`

## Problem

The `node-red-contrib-home-assistant-websocket` palette can *publish* entities back
into Home Assistant — `ha-switch`, `ha-button`, `ha-sensor`, etc. — but only when the
**"Node-RED Companion"** integration (HA config-entry domain `nodered`) is set up in
Home Assistant. That integration is a one-time, manual HA step (Settings → Devices &
Services); no Node-RED or MCP action can create it.

Today an agent building a flow cannot tell whether that precondition is met, so it has
to ask the human ("do you see the Node-RED integration in HA?"). The agent should be
able to check this itself before attempting to author entity-publishing nodes.

This is the first of a possible series (detection now; a structure-aware
`nr_create_ha_entity` assembler is a separate, later decision). It does **not** create
anything — it is a read-only probe.

## Goals

- One read-only tool, `ha_check_nodered_integration`, that answers: is the Node-RED
  Companion integration present in HA, and is it loaded?
- Reuse the existing HA WebSocket transport (config-entries are **not** on the REST
  API; only the WS API exposes them).
- Robust across HA versions and clear about the one failure mode unique to this query
  (it requires an **admin** token).

## Non-goals

- **Creating the integration.** Manual HA step; nothing here installs it.
- **Creating entities** (`ha-switch`/`ha-button`/…). That is a separate, later
  `nr_create_ha_entity` assembler, not part of this spec.
- **Device/entity registry inspection.** Those only reflect what NR has *already*
  created (a false negative for "installed but unused"), so they are not the signal.
- **Per-entity readiness beyond the integration's load state.**

## Design

### A. Module rename — `ha_stats.py` → `ha_ws.py`

There are now **two** HA-WebSocket consumers (long-term statistics + integration
detection), so the module named "Long-Term Statistics" no longer describes its
contents. Rename it and broaden its purpose to "HA WebSocket queries".

- `git mv src/nrha_mcp/ha_stats.py src/nrha_mcp/ha_ws.py`.
- Broaden the module docstring to cover both queries.
- Rename the exception `HAStatsError` → `HAWsError` (still `class HAWsError(NRError)`),
  update both existing raise sites.
- `git mv tests/test_ha_stats.py tests/test_ha_ws.py`; update its imports
  (`from nrha_mcp import ha_ws`, `from nrha_mcp.ha_ws import HAWsError`) and references
  (`ha_ws._ws_url`, `ha_ws.get_entity_statistics`).
- `src/nrha_mcp/server.py`: update the import (`…, ha_ws, subflow_edit`) and the
  `get_entity_statistics` call site (`ha_ws.get_entity_statistics(...)`).

Reference sites that must change (full list): `server.py` import line + the
`ha_stats.get_entity_statistics` call; the module file itself (class + 2 raises +
docstring); `tests/test_ha_stats.py` (import + `HAStatsError` import + `_ws_url` and
`get_entity_statistics` references).

### B. Shared auth helper — `ha_ws.py`

The auth handshake is currently inlined at the top of `_query_statistics`. Extract it
so both queries share it:

```python
async def _auth(conn, token) -> None:
    await conn.recv()  # auth_required greeting
    await conn.send({"type": "auth", "access_token": token})
    auth = await conn.recv()
    if auth.get("type") != "auth_ok":
        raise HAWsError(f"HA WebSocket auth failed: {auth.get('type')}")
```

`_query_statistics` is refactored to call `_auth(conn, token)` in place of its inlined
handshake (the on-the-wire frame order is unchanged, so existing stats tests stay
green).

### C. `check_nodered_integration(settings, *, connect=None)` — `ha_ws.py`

```python
async def check_nodered_integration(settings, *, connect=None) -> dict:
    """Whether the 'Node-RED Companion' integration (config-entry domain 'nodered')
    is set up in Home Assistant. Read-only; requires an admin HA token."""
    conn = await (connect or _default_connect)(settings)
    try:
        await _auth(conn, settings.ha_token)
        await conn.send({"id": 1, "type": "config_entries/get"})
        while True:
            msg = await conn.recv()
            if msg.get("id") == 1 and msg.get("type") == "result":
                if not msg.get("success"):
                    err = msg.get("error") or {}
                    if err.get("code") == "unauthorized":
                        raise HAWsError(
                            "config_entries/get was refused (unauthorized): the HA token "
                            "likely lacks admin rights, which this check requires.")
                    raise HAWsError(f"config_entries/get failed: {err}")
                entries = msg.get("result") or []
                break
    finally:
        await conn.close()

    nodered = [e for e in entries if e.get("domain") == "nodered"]
    out_entries = [{"entry_id": e.get("entry_id"), "title": e.get("title"),
                    "state": e.get("state")} for e in nodered]
    installed = bool(nodered)
    loaded = any(e.get("state") == "loaded" for e in nodered)
    if not installed:
        note = ("The 'Node-RED Companion' integration is not set up in Home Assistant "
                "(Settings → Devices & Services). It is a manual HA step — no tool can "
                "create it — and it must be present before ha-switch/ha-button/ha-sensor "
                "nodes can publish entities to HA.")
    elif not loaded:
        note = ("Integration present but not loaded (state is not 'loaded'); entities "
                "from Node-RED won't appear until it loads. Check it in HA.")
    else:
        note = "Integration present and loaded; Node-RED can publish entities to HA."
    return {"installed": installed, "entries": out_entries, "note": note}
```

**Design decisions baked in (from review):**

1. **Client-side domain filter.** Send `config_entries/get` with **no** `domain`
   parameter and filter `e.get("domain") == "nodered"` in Python. The server-side
   `domain` filter only exists on newer HA; filtering client-side is version-agnostic
   and the payload is small.
2. **Admin requirement is explicit.** Unlike `recorder/statistics_during_period`,
   `config_entries/get` is admin-only. A `success: false` with `error.code ==
   "unauthorized"` is surfaced as a clear "token likely lacks admin rights" message
   rather than a generic failure. There is no reliable non-admin signal for "installed
   but unused", so this requirement is inherent to the only correct probe.
3. **`installed` vs `loaded`.** `installed` reflects presence of any `nodered`
   config-entry; each entry's `state` is surfaced; the `note` distinguishes
   present-but-not-loaded so the agent does not proceed on an errored integration.
4. **`connect` injectable**, mirroring `get_entity_statistics`, so unit tests feed
   frames without a live server.

### D. Tool — `server.py`

```python
@mcp.tool()
async def ha_check_nodered_integration() -> str:
    """Check whether the 'Node-RED Companion' integration is set up in Home Assistant.
    ha-switch/ha-button/ha-sensor nodes can only publish entities to HA when this
    integration is installed (Settings → Devices & Services) — a manual HA step no tool
    can perform. Read-only; requires an admin HA token. Returns
    {installed, entries:[{entry_id,title,state}], note}."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA_URL and HA_TOKEN must be set to query Home Assistant.")
    return await _wrap(lambda: ha_ws.check_nodered_integration(settings))
```

The `HA_URL`/`HA_TOKEN` guard mirrors `ha_get_state` exactly (use the same
`_err(...)` form already used by the other `ha_*` tools).

**Tool count 36 → 37.** Update `tests/test_server_smoke.py` (add
`"ha_check_nodered_integration"` to `expected`, set `len(names) == 37`) and
`scripts/smoke_e2e.py` (add `"ha_check_nodered_integration"` to `EXPECTED_TOOLS`).
`smoke_e2e.py` only asserts the tool is *registered* (it never invokes `ha_*` tools,
which need a live HA), so no e2e HA dependency is introduced.

## Error handling

Read-only + a pure-ish probe. `HAWsError` (an `NRError` subclass) for: WS auth failure,
`config_entries/get` returning `success: false` (with a dedicated message for
`unauthorized`). All propagate through `_wrap` to a structured `{"error": …}`. The
`HA_URL`/`HA_TOKEN` guard short-circuits in the tool before any connection.

## Testing

`tests/test_ha_ws.py` (the renamed stats test file, with the stats tests intact after
the `ha_ws`/`HAWsError` rename). Add detection tests using an injected fake `connect`
that feeds frames (mirror the existing stats fake):

- **present + loaded:** frames = `auth_required` → `auth_ok` → result whose list
  contains `{"domain":"nodered","entry_id":"e1","title":"Node-RED","state":"loaded"}`
  → `installed: true`, the entry surfaced, note says present-and-loaded.
- **present but not loaded:** the `nodered` entry has `state: "setup_error"` →
  `installed: true`, `loaded` reflected in note (present-but-not-loaded message).
- **absent:** result list has only non-`nodered` entries → `installed: false`,
  `entries: []`, note says not-set-up.
- **unauthorized:** result is `{"id":1,"type":"result","success":false,
  "error":{"code":"unauthorized"}}` → raises `HAWsError` mentioning admin rights.
- **auth failure:** greeting then `auth_invalid` → raises `HAWsError` (covered by the
  shared `_auth`; one direct test).

`tests/test_server_smoke.py`: count **37**; `ha_check_nodered_integration` in
`expected`. `scripts/smoke_e2e.py`: `ha_check_nodered_integration` in `EXPECTED_TOOLS`.

## Deployment

Rebuild/redeploy the Docker image to expose the tool live; local `.venv` for tests.
Note for the operator: the check needs the configured `HA_TOKEN` to belong to an
**admin** HA user; with a non-admin token the tool returns a clear "lacks admin
rights" error and the human fallback (eyeball the integrations list) still applies.

## Follow-ups

- **`nr_create_ha_entity`** — a structure-aware assembler that builds
  `ha-entity-config` + the entity node, reusing the HA server config and an existing
  entity-config when present (option ② from the analysis). Gated on this detection
  saying "present" and on deciding the NR-published-entity workflow is wanted.
- **A non-admin fallback signal** (e.g. detecting NR-created devices) if the admin
  requirement proves limiting in practice.
