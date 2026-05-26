# Runtime Observation for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/runtime-observation`

## Problem

An agent built a rain-monitoring flow and then could not verify it worked. It
fell into a loop trying to observe the flow's runtime output through MCP tools
that returned empty results. Investigation found **two distinct root causes**,
plus a misleading tool that set the trap.

1. **`nr_get_debug_output` cannot capture debug output by design.** It is a
   plain alias of `get_flow_context` (`flows.py:154-155`). Debug-node messages
   are **not stored in flow context** — Node-RED pushes them over the `/comms`
   WebSocket to the editor sidebar. Reading context will never surface them.

2. **The context tools read the wrong store.** `get_flow_context` /
   `get_global_context` do `data.get("memory", {})` (`flows.py:134-151`), but
   this Node-RED exposes its context under stores named **`default`** (real
   data) and `store` (empty) — there is no `"memory"` store. So the tools
   always returned empty, even though `flow.set`/`global.set` worked fine.
   Verified live: the global context still holds `rain_test_out` (a write from
   the looping session) under the `default` store.

The earlier conclusion that "context-read tools are blind to the runtime" was
**wrong**: they were reading a non-existent store name. Only the debug-sidebar
output genuinely requires a new channel (the WebSocket).

## Goals

- Give the MCP a way to **observe live debug/status messages** from the Node-RED
  runtime, deterministically (trigger + capture in one call).
- **Fix the context tools** to read all stores so they return real data.
- **Remove the misleading `nr_get_debug_output`** so agents stop falling into the
  same trap.

## Non-goals

- Always-on background buffering of runtime messages (the "Option 3" from
  brainstorming). The WS helper built here is the building block for it later,
  but it is out of scope now (YAGNI — the driving use case has no live events to
  passively catch).
- Changing Node-RED's own configuration (context storage, adminAuth).

## Design

### Part 1 — `nr_capture_debug` (new tool)

A single tool that subscribes to the Node-RED `/comms` WebSocket, optionally
fires an inject, and collects the messages that arrive during a time window.
Subscribing **before** injecting removes the race that would otherwise drop the
message (Node-RED does not buffer `/comms` messages server-side; only currently
subscribed clients receive them).

**Signature**

```python
nr_capture_debug(
    window_ms: int = 2000,              # how long to listen
    inject_node_id: str | None = None,  # if set, trigger this inject AFTER subscribing
    topics: list[str] | None = None,    # default ["debug", "status"]
    node_id: str | None = None,         # filter: only messages from this node id
    flow_id: str | None = None,         # filter: only messages from this tab/flow id
    max_messages: int = 100,            # stop early once this many collected
) -> str  # JSON
```

**Sequence (the part that kills the loop)**

1. Resolve auth (reuse the client's `_ensure_auth`); obtain a bearer token if one
   is available.
2. Open the WebSocket. Derive its URL from `nr_url`: `http`→`ws`, `https`→`wss`,
   strip trailing `/`, append `/comms`
   (e.g. `ws://homeassistant.localdomain:1880/comms`).
3. Authenticate the socket: if a token is available, send `{"auth": "<token>"}`
   and await `{"auth": "ok"}`; if the admin API is open (current setup — see Auth
   below), skip and subscribe directly. Pass the same `Authorization` header used
   by the HTTP client on the handshake to cover a reverse-proxy case.
4. Subscribe: send `{"subscribe": "debug"}` and `{"subscribe": "status/#"}` (one
   message per requested topic). Briefly yield (~100 ms) so the subscription is
   registered before injecting.
5. If `inject_node_id` is set, `POST /inject/{id}` via the existing client.
6. Collect frames until `window_ms` elapses or `max_messages` is reached.
   Ignore ping/keepalive frames. Apply `node_id` / `flow_id` filters.
7. Close the socket; return the collected messages.

**Message shapes** (from Node-RED `/comms`)

- debug: `{"topic": "debug", "data": {"id", "z", "name", "topic", "msg", "format", ...}}`
  — `id` = debug node id, `z` = flow id, `msg` = formatted payload.
- status: `{"topic": "status/<nodeid>", "data": {...}}` — node status (text, fill,
  shape). Shape varies by Node-RED version; the implementation normalizes to
  `{text, fill, shape}` and confirms the exact nesting during implementation.

**Return shape**

```json
{
  "captured": [
    {"topic": "debug", "node_id": "...", "node_name": "Pioggia rilevata",
     "flow_id": "...", "msg": "🌧️ Pioggia rilevata ...", "format": "string[70]",
     "ts": 1716724800.123}
  ],
  "count": 1,
  "window_ms": 2000,
  "injected": "<node_id or null>"
}
```

When `captured` is empty, include a `hint` field explaining the likely causes so
an empty result is not misread as "broken": debug node not active / not sending
to sidebar; window too short; the inject produced no output (e.g. filtered by a
function); wrong `node_id`/`flow_id` filter.

### Part 2 — Fix the context tools

Rewrite `get_flow_context` / `get_global_context` (`flows.py`) to stop
hardcoding `"memory"`:

- Iterate **all** store names present in the response (`default`, `store`, and
  any others) and union their keys.
- Listing (no `key`): return keys grouped by store, e.g.
  `{"scope": "global", "stores": {"default": ["rain_test_out", "homeassistant"], "store": []}}`.
- Lookup (with `key`): search across stores (prefer `default`), return the value
  and which store it came from; raise `NRNotFoundError` only if absent from all.

This removes the false-empty results that contributed to the loop.

### Removal

Delete the `nr_get_debug_output` tool (`server.py:101-104`) and
`flows.get_debug_output` (`flows.py:154-155`). It never did what its name
implied; `nr_capture_debug` replaces it.

### Code structure

- New module `src/nrha_mcp/runtime.py`: the capture logic. To stay testable
  without a live Node-RED, the WebSocket transport sits behind a small injectable
  async message-source interface; the default implementation uses the
  `websockets` library, and tests feed fake frames.
- `client.py`: add a helper for the `/comms` WS URL and expose the resolved
  bearer token (via the existing `_ensure_auth`) so `runtime.py` can authenticate
  the socket. The inject reuses the existing `client.inject`.
- `server.py`: register `@mcp.tool() nr_capture_debug(...)` following the
  existing `_wrap(...)` pattern.
- `pyproject.toml`: add dependency `websockets>=12` (httpx does not speak WS).

## Auth (WebSocket)

This Node-RED answers the admin REST API with `nr_auth=basic` and **no bearer
token** (probe: `auth_mode = basic | token set: False`), which indicates the
admin API is effectively open (Node-RED `adminAuth` only issues bearer tokens, so
basic credentials are ignored). Therefore `/comms` most likely needs **no auth**.
The implementation must still handle all three cases and let tests confirm:

1. open admin API → subscribe directly (current setup);
2. token mode → send `{"auth": "<token>"}` first;
3. reverse-proxy basic auth → send the `Authorization` header on the handshake.

## Error handling

- WS connect/auth failure → a clear error mirroring `NRAuthError`/`NRError`,
  naming the `/comms` URL.
- Inject of a missing node → existing `NRNotFoundError` from `client.inject`.
- No messages → empty `captured` + `hint` (not an error).
- Always close the socket (try/finally), even on timeout/exception.

## Testing

- `tests/test_runtime.py` (new): drive `nr_capture_debug` with a fake message
  source. Cases: debug-only capture; debug+status; `node_id`/`flow_id` filtering;
  `max_messages` cap; window timeout with zero messages (asserts `hint`); inject
  ordering (inject fires only after subscribe); auth message sent when a token is
  present, skipped when open.
- `tests/test_flows.py` (update): multi-store context — data under `default`,
  empty `store`; listing groups keys by store; key lookup finds a key in
  `default`; missing key raises `NRNotFoundError`.
- Existing `nr_get_debug_output` tests (if any) removed.

## Deployment

The live MCP on `:8090` runs from the Docker image (`pip install .` per the
Dockerfile). To ship: add the dependency + code, **rebuild the image** and
redeploy the container. The local `.venv` is for dev/tests only.

## Follow-ups

- Correct the auto-memory note `nrha-context-read-tools-blind`: context reads
  are not blind to the runtime — they read the wrong store name. The
  debug-sidebar caveat (debug output is not in context) remains true.
```
