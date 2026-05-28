# HA Service Actuation for the nrha MCP — Design

- **Date:** 2026-05-28
- **Status:** Approved (pending spec review)
- **Branch:** `feat/ha-call-service`

## Problem

Every HA tool in the server is read-only (`ha_get_state`, `ha_search_entities`,
`ha_get_services`, history/statistics). To perform any *active* action against Home
Assistant — toggle a helper, press a button, nudge a cover, set an `input_number`
for a test — the agent's only lever today is to author an `api-call-service` node in
a flow and fire it via `nr_inject`. That requires the node to exist (or be created +
deployed), pollutes flows with test nodes, and couples a simple actuation to the
Node-RED deploy cycle.

The HA REST API already exposes `POST /api/services/{domain}/{service}`, and the
server's `HARestClient` is already authenticated against it (it does the read-only
GETs). The missing piece is a single write verb.

### Why not the official HA MCP Server, or a third-party one?

The official `mcp_server` integration exposes Home Assistant's **Assist API** —
*intent-based* (HassTurnOn/Off…), gated by which entities are *exposed to Assist*. It
does **not** offer arbitrary `domain.service` calls. That is the wrong shape for
deterministic test actuation of helpers / buttons / covers / scripts-with-params, and
fragments the agent across a second server/token. Third-party HA MCP servers do
expose raw service calls, but place a Home Assistant **admin token in a third-party
process** with no risk model suited to an autonomous agent. Building it into nrha
keeps the token in the server we already run and trust, integrates with the Node-RED
workflow, and lets us tailor the safety model (below). The official MCP can still be
added to the client *alongside* nrha for general conversational home control — a
different job.

## Goals

- An `ha_call_service` tool that calls an arbitrary HA service with a precise
  `domain`/`service`, an optional `entity_id` target, and a free-form `data` dict for
  extra params / non-entity targets.
- A tiered safety model (see *Safety policy*) that is frictionless on ordinary
  control, gates genuinely sensitive services behind an explicit confirm, and hard-
  blocks a small set of catastrophic ones.
- A trimmed, stable return shape that gives the agent the changed-states as proof of
  effect without flooding context.
- The effective policy is observable via `nr_diagnostics`.

## Non-goals

- `return_response` services (e.g. `weather.get_forecasts`, `calendar.get_events`):
  they require `?return_response` and return *data*, not state changes. The agent
  actuates, it does not query data this way. If one is called, HA returns a clear 400
  that surfaces as `NRError`. A `return_response: bool` param is a documented
  follow-up, not built now.
- Rich target fields (`area_id`/`device_id`/`label_id` as explicit params). Anything
  they enable, `data` already enables; surfacing them is schema noise that can
  mislead an LLM into filling them. YAGNI.
- A security sandbox. See *Security limitation*.
- Returning previous state (old→new). HA's synchronous array gives only the new
  state; capturing old would need an extra pre-POST GET. YAGNI.

## Design

### A. Client method — `ha.HARestClient`

```python
async def call_service(self, domain: str, service: str, body: dict) -> list[dict]:
    r = await self._client.post(f"/api/services/{domain}/{service}", json=body)
    r.raise_for_status()
    return r.json()  # array of state objects that changed (often [])
```

`POST /api/services/{domain}/{service}` takes the service_data as a flat JSON body
(`entity_id` and any params at the same level) and returns **200 with an array of the
states that changed**. The array is frequently empty (async services, or no
synchronous change) — that is *not* a failure.

### B. Module function — `ha.call_service(...)`

```python
async def call_service(ha_client, settings, domain, service,
                       entity_id=None, data=None, confirm=False) -> dict
```

Orchestrates four steps:

1. **Build body.** `body = {**(data or {})}`; if `entity_id` is given,
   `body["entity_id"] = entity_id`. If `data` already carried an `entity_id` that
   *differs* from the named param → raise `NRError` (collision). `entity_id` accepts
   `str | list[str]` and is optional (no-target services are valid).
2. **Classify tier** via `_service_tier(domain, service, settings)` →
   `"blocked" | "confirm" | "direct"`. Precedence: **block > confirm > direct**.
3. **Apply policy:**
   - `blocked` → raise `NRError` ("`{domain}.{service}` is in the catastrophic block
     list and cannot be executed; `confirm=true` does not override it").
   - `confirm` **and** `confirm is False` → return a **preview** dict, no POST:
     ```json
     {"preview": true, "would_call": "lock.unlock", "entity_id": "lock.porta",
      "data": {}, "tier": "sensitive", "requires_confirm": true,
      "hint": "re-call with confirm=true to execute."}
     ```
   - `confirm` **and** `confirm is True`, or `direct` → call
     `ha_client.call_service(domain, service, body)`.
4. **Shape return** (trimmed):
   ```json
   {"ok": true, "called": "light.turn_on", "entity_id": "light.kitchen",
    "changed": [{"entity_id": "light.kitchen", "state": "on"}], "changed_count": 1}
   ```
   When the changed array is empty, add a steering note (the call was accepted):
   ```json
   {"ok": true, "called": "...", "entity_id": "...", "changed": [], "changed_count": 0,
    "note": "No state change reported synchronously; verify with ha_get_state."}
   ```
   `changed` keeps only `entity_id` + `state` per entity (consistent with the repo's
   trimming convention). For settling entities (cover position, climate temp) the
   synchronous snapshot is transitional anyway — the note steers the agent to verify.

### C. Tier classification & lists — `ha.py`

```python
CATASTROPHIC_FLOOR = {          # hardcoded; additive only; confirm cannot override
    "homeassistant.stop", "homeassistant.restart",
    "hassio.host_reboot", "hassio.host_shutdown", "hassio.supervisor_restart",
    "hassio.restore_full", "hassio.restore_partial",
    "recorder.purge", "recorder.purge_entities",
}
DEFAULT_CONFIRM = {             # used when HA_CONFIRM_SERVICES is unset
    "lock.unlock", "lock.open",
    "alarm_control_panel.alarm_disarm", "alarm_control_panel.alarm_trigger",
    "update.install",
}
```

`_service_tier(domain, service, settings)`:

- `key = f"{domain}.{service}".lower()`
- **block set** = `CATASTROPHIC_FLOOR ∪ parse(settings.ha_block_services)` (additive).
- **confirm set** = `DEFAULT_CONFIRM` if `settings.ha_confirm_services is None` else
  `parse(settings.ha_confirm_services)` (override-with-default; `""` → empty set).
- match a set if `key` is in it **or** `f"{domain}.*"` is in it (wildcard).
- return `"blocked"` if in block set; else `"confirm"` if in confirm set; else
  `"direct"`.

`_parse_service_list(raw: str) -> set[str]`: split on commas, strip, lowercase, drop
empties. Lives in `ha.py` so `config.py` stays a plain data holder.

Covers stay **direct** by default — the target install's covers are non-dangerous
roller shutters (tapparelle). A user with a powered gate/garage adds `cover.*` via
`HA_CONFIRM_SERVICES`.

> **Floor finalized for Home Assistant OS** (the target install): the `hassio.*`
> services are present, and the destructive restore surface is the Supervisor's
> `hassio.restore_full`/`hassio.restore_partial`. The core `backup` integration's
> restore is a UI/onboarding flow, **not** a callable service, so it is omitted; if a
> future HA version exposes `backup.restore` as a service, add it via
> `HA_BLOCK_SERVICES`. Blocking an absent service is a harmless no-op.

### D. Tool — `server.py`

```python
@mcp.tool()
async def ha_call_service(domain: str, service: str,
                          entity_id: str | list[str] | None = None,
                          data: dict | None = None, confirm: bool = False) -> str:
    """Call a Home Assistant service (turn things on/off, press buttons, nudge covers,
    set helpers). Requires HA_URL + HA_TOKEN.

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

Same create-use-close + creds-guard pattern as `ha_get_services`. `ha` is already
imported.

**Tool count goes 37 → 38.** Update both hardcoded sites: `tests/test_server_smoke.py`
(`expected` set + `len(names) == 38`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

### E. Config — `config.py`

```python
# Home Assistant service-call safety
ha_block_services: str = ""          # additive to the catastrophic floor; csv
ha_confirm_services: str | None = None  # override-with-default; None=default, ""=none; csv
```

Both accept csv of `domain.service` or `domain.*`. The `None` default on
`ha_confirm_services` is what distinguishes *unset* (use `DEFAULT_CONFIRM`) from *set
empty* (no confirm gating) — must stay `str | None`, not `str`.

### F. Observability — `diagnostics.py`

`nr_diagnostics` gains an `ha_call_policy` block reporting the **effective** policy:

```json
{"ha_call_policy": {
   "blocked": ["hassio.restore_full", "homeassistant.restart", ...],
   "confirm": ["alarm_control_panel.alarm_disarm", "lock.unlock", ...],
   "confirm_source": "default" }}
```

`confirm_source` is `"default"` when `ha_confirm_services` is unset, else
`"configured"`. This makes a typo in `HA_CONFIRM_SERVICES` (which would silently drop
the `unlock`/`disarm` gate) visible instead of silent.

## Error handling

- Missing `HA_URL`/`HA_TOKEN` → clear `_err`, no client opened (as other `ha_*`).
- `entity_id` collision (named ≠ value in `data`) → `NRError`.
- Blocked service → `NRError`, distinct message naming the floor and stating
  `confirm=true` will not override.
- HA `400` (unknown service / service requires `return_response`) → `NRError`
  surfacing HA's message (via `raise_for_status` → `_wrap`).
- HA `401`/`403` → `NRError` (token lacks permission); `404` → wrong domain/service
  path. Network/timeout → `_wrap`'s generic handler.
- **`200` with `changed: []` is success, not error** — returns `ok` + the steering
  note. The error/empty distinction is the key correctness boundary: the agent must
  tell "rejected" from "accepted, no synchronous change".

## Security limitation (documented, by design)

Tier classification matches the service **at the call boundary**. `script.*` and
`automation.trigger` execute arbitrary user-authored logic that can *indirectly* reach
a floor service (e.g. a script that calls `homeassistant.restart`). **D+C is a
guardrail against the agent's direct mistakes, not a sandbox against indirection.**
Scripts/automations are user-authored and already trusted. Gating `script.*` would add
friction without closing the hole (it just moves it). Stated here so the floor is not
mistaken for hermetic.

## Testing

`tests/test_ha.py` (extend) — `FakeHAClient` gains a `call_service(domain, service,
body)` that records the call and returns a configurable changed-states array
(default `[]`).

- **Body building:** `entity_id` merges into body; `entity_id` + `data` with the same
  value is fine; conflicting `entity_id` in `data` → `NRError`.
- **Tier classification:** floor service → `blocked` (and still blocked with
  `confirm=True`); `DEFAULT_CONFIRM` service with `confirm=False` → `preview` (no call
  recorded); same with `confirm=True` → call recorded; ordinary service → call
  recorded directly; `domain.*` wildcard matches; `HA_BLOCK_SERVICES` adds to the
  floor; `HA_CONFIRM_SERVICES` overrides default (incl. `""` → nothing gated).
- **Return shaping:** non-empty changed → trimmed `entity_id`+`state`,
  `changed_count`; empty changed → `note` present, `changed_count == 0`.
- **Diagnostics:** `ha_call_policy` reports floor ∪ configured block, effective
  confirm list, and correct `confirm_source`.

`tests/test_server_smoke.py`: count 38; `ha_call_service` registered.
`scripts/smoke_e2e.py`: add `"ha_call_service"` to `EXPECTED_TOOLS` (no real HA call
exercised — the tool no-ops without creds, so smoke only asserts registration).

## Deployment

Rebuild and redeploy the Docker image to expose the tool live; the running MCP must
have `HA_URL`/`HA_TOKEN` set (and an HA token with permission to call the targeted
services). Local `.venv` is for tests. Document `HA_BLOCK_SERVICES` /
`HA_CONFIRM_SERVICES` in the README/`.env` example.

## Follow-ups

- `return_response: bool` param for data-returning services. YAGNI now.
- `HA_BLOCK_OVERRIDE=true` escape hatch to switch the block set from additive to full
  override, for the rare user who wants e.g. agent-triggered restart. Not built now;
  the additive floor is the safe default.
- `verbose: bool` opt-in to return full attributes in `changed`, if a use case for
  attribute-in-return appears. Default stays trimmed.
