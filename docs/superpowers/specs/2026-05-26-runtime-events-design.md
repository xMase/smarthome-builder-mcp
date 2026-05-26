# Runtime Events for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/runtime-events`

## Problem

The `nr_capture_debug` tool (added in `feat/runtime-observation`) observes the `debug`
and `status` channels of Node-RED's `/comms` WebSocket. It does **not** surface
**runtime/system events**: the outcome of a deploy, runtime-state changes
(flows started/stopped, safe mode), and node/module problems (a module that
failed to load, an unregistered node type). These flow on a separate `/comms`
topic, `notification/#`. Without them, an agent that deploys a flow cannot tell
whether the deploy or the runtime actually went wrong — it has to guess.

## Goals

- Let an agent capture Node-RED `notification/#` events over a time window.
- Provide an intent-revealing convenience tool for "show me what happened at
  runtime" (e.g. right after a deploy).

## Non-goals

- **Severity filtering (error vs warning vs info) is out of scope for v1.** The
  `/comms` frames do not carry a uniform, documented severity field across
  Node-RED versions, and the contents of `notification/#` subtopics are
  version-dependent. Promising a severity-filtered "errors only" view we cannot
  reliably produce would give false confidence (the same trap as the old
  `nr_get_debug_output`). v1 filters by **source/topic only**; severity is a
  later increment after the real frames are confirmed live.
- Surfacing error-level entries from the `debug` topic — also deferred to the
  severity increment, since identifying them requires the same severity marker.

## Design

### Part 1 — Extend `capture_debug` to support the `notification` topic

`src/nrha_mcp/runtime.py`:

- Allow `"notification"` as a value in `topics`. The subscribe step maps topic
  names to Node-RED subscription strings: `status` → `status/#`,
  **`notification` → `notification/#`**, anything else → itself.
- Extend `_normalize` to handle frames whose topic starts with `notification/`,
  mapped **generically** (no guessed level):

  ```json
  {
    "topic": "notification",
    "subtopic": "runtime-deploy",      // the part after "notification/"
    "node_id": null,
    "flow_id": null,
    "data": { ... },                    // raw payload, kept as-is (shape varies)
    "ts": 1716724800.123
  }
  ```

- The existing `node_id` / `flow_id` filters drop notification frames (they have
  no node/flow). This is intended and documented — those filters target
  debug/status.

The tool signature of `nr_capture_debug` does **not** change (`topics` already
exists); only its docstring is updated to list `notification` as an option.

### Part 2 — `nr_get_runtime_events` (convenience wrapper)

`src/nrha_mcp/runtime.py`:

```python
async def get_runtime_events(client, *, window_ms=2000, max_messages=100, connect=None) -> dict:
    res = await capture_debug(client, topics=["notification"],
                              window_ms=window_ms, max_messages=max_messages, connect=connect)
    events = res["captured"]
    out = {"events": events, "count": len(events), "window_ms": window_ms}
    if not events:
        out["hint"] = (
            "No runtime events in the window. That is normal when nothing happened "
            "(no deploy, no state change, no module/node error). Trigger the action "
            "(e.g. a deploy) during the window, or widen window_ms."
        )
    return out
```

It captures **only** the `notification/#` channel — the one fully deterministic
source for v1.

### Part 3 — Register the tool

`src/nrha_mcp/server.py` (already imports `runtime`):

- Update the `nr_capture_debug` docstring to mention `notification` as a `topics`
  value.
- Add:

  ```python
  @mcp.tool()
  async def nr_get_runtime_events(window_ms: int = 2000, max_messages: int = 100) -> str:
      """Capture Node-RED runtime/system events from the /comms `notification/#` channel.

      Surfaces deploy outcomes, runtime-state changes (flows started/stopped, safe
      mode) and node/module problems (failed loads, unregistered types). Use it
      right after a deploy to check nothing broke. It listens passively for
      `window_ms`, so trigger the action during the window. (Does not filter by
      severity yet; returns events by source.)"""
      return await _wrap(lambda: runtime.get_runtime_events(
          client, window_ms=window_ms, max_messages=max_messages))
  ```

- **Tool count goes 23 → 24.** The tool set is asserted in TWO places — update
  both: `tests/test_server_smoke.py` (add `"nr_get_runtime_events"` to `expected`
  and change `len(names) == 23` to `24`) and `scripts/smoke_e2e.py`
  (`EXPECTED_TOOLS`, which asserts `len(EXPECTED_TOOLS)` present).

### Naming

The tool is `nr_get_runtime_events`, not `nr_get_runtime_errors`: in v1 it returns
runtime events by source, not a severity-filtered error list, so "events" is the
honest name. The docstring explicitly mentions deploy failures and node/module
errors so an agent searching for "errors" still matches it. The name still fits
once severity filtering is added later.

## Error handling

Inherited from `capture_debug`: a `/comms` connect/auth failure raises a clear
error; the socket is always closed (try/finally); an empty capture is not an
error (returns `hint`).

## Testing

`tests/test_runtime.py` (extend):
- `capture_debug` with `topics=["notification"]` subscribes to `notification/#`
  and `_normalize` maps a `notification/runtime-deploy` frame to
  `{topic: "notification", subtopic: "runtime-deploy", data: {...}}`.
- `get_runtime_events` returns the captured events with `count`, and an empty
  window yields `hint`.
- A notification frame is dropped when a `node_id` filter is set (documents the
  filter interaction).

`tests/test_server_smoke.py` (update): count is 24; both `nr_capture_debug` and
`nr_get_runtime_events` are registered.

`scripts/smoke_e2e.py` (update): add `"nr_get_runtime_events"` to `EXPECTED_TOOLS`.

## Deployment

Same as the prior feature: the live MCP runs from the Docker image; rebuild and
redeploy the container to expose the new tool. The local `.venv` is for tests.

## Follow-ups

- **Severity increment:** once the real `notification/#` and `debug` frames from
  this Node-RED are observed live, add optional severity filtering (error/warning)
  and optionally include error-level `debug` entries. Then a thin
  `nr_get_runtime_errors` alias filtering for errors becomes honest.
- Confirm the live shapes of `notification/*` payloads (same live-verification
  step already pending for `status/*` frames).
