# `/comms` batched (array) frame handling — Design

- **Date:** 2026-05-28
- **Status:** Approved (pending spec review)
- **Branch:** `feat/comms-batched-frames`

## Problem

With the basic-auth handshake fixed, `nr_capture_debug` now connects to `/comms` live —
but every call fails server-side with `AttributeError: 'list' object has no attribute
'get'`.

Root cause: `capture_debug` (`runtime.py:120-123`) calls `_normalize(frame)` once per
`recv()`, and `_normalize` does `frame.get("topic")`. But Node-RED **batches** its
`/comms` data messages into a JSON **array** — a single WS frame is `[{topic,data}, …]`,
not a dict. `_normalize` receives the list and `.get()` blows up. The unit tests never
caught this because `FakeComms` feeds **dict** frames, not the arrays Node-RED actually
sends.

Both shapes genuinely occur on the wire:
- **arrays** `[{topic,data}, …]` — batched data messages (debug/status);
- **single dicts** — control frames, e.g. the auth ack `{"auth":"ok"}` (which
  `_normalize` already maps to `None` because its topic is empty).

So the handler must accept both — this is correctness, not just defensiveness.

## Goals

- `capture_debug` correctly captures **every** message in a batched array frame.
- Single-dict frames (control acks, or a non-batched message) keep working.
- A malformed/non-dict item never crashes the capture.

## Non-goals

- **The client→server send path** (`{"auth": token}`, `{"subscribe": …}`) is unchanged.
- **Token-mode auth-ack race:** `capture_debug` sends `{auth}` then subscribes without
  waiting for `{"auth":"ok"}` — a possible race on vanilla Node-RED with `adminAuth`.
  Not the HA add-on case (basic mode sends no auth message); pre-existing → follow-up.
- **No new MCP tool.** Tool count stays 37; no smoke/e2e changes.

## Design

### A. `capture_debug` — iterate frame items (`src/nrha_mcp/runtime.py`)

Replace the single normalize/filter/append (currently `runtime.py:123-130`) with a loop
over the frame's items, treating a non-list frame as a one-element list:

```python
            for item in (frame if isinstance(frame, list) else [frame]):
                msg = _normalize(item)
                if msg is None:
                    continue
                if node_id and msg["node_id"] != node_id:
                    continue
                if flow_id and msg["flow_id"] != flow_id:
                    continue
                captured.append(msg)
                if len(captured) >= max_messages:
                    break
```

The outer `while len(captured) < max_messages` loop and the `recv()`/deadline handling
above it are unchanged. The inner `break` honors `max_messages` mid-batch (the tail of
that array is intentionally dropped once the cap is hit). An empty array `[]` iterates
zero times (no-op).

### B. `_normalize` — defensive non-dict guard (`src/nrha_mcp/runtime.py:54`)

Make per-item normalization total so a stray non-dict item is skipped, not fatal:

```python
def _normalize(frame: dict) -> dict | None:
    """Map a single /comms message to our dict, or None if not a debug/status/notification frame."""
    if not isinstance(frame, dict):
        return None
    topic = frame.get("topic", "")
    data = frame.get("data", {}) or {}
    ...
```

(Rest of `_normalize` unchanged.) This is the second layer against the
`'X' object has no attribute 'get'` class of error.

## Error handling

Read-only capture. The fix removes the `AttributeError`; any genuinely unparseable item
yields `None` and is skipped. No exception path changes.

## Testing

`tests/test_runtime.py` (extend; it has `FakeComms(frames)`, `_connect_factory`,
`_debug_frame`, `_notification_frame`). Add a `_batch(*frames)` helper and:

- **batched array, multiple messages:** `FakeComms([_batch(_debug_frame(msg="a"),
  _debug_frame(msg="b"))])`, `max_messages=10` → `count == 2`, captured msgs `["a","b"]`
  (proves the list no longer crashes AND every item in the batch is captured).
- **node_id filter within a batch:** one array frame with `_debug_frame(node_id="d1",
  msg="keep")` + `_debug_frame(node_id="d2", msg="drop")`, `node_id="d1"` → `count == 1`,
  msg `"keep"`.

Keep the existing dict-frame tests (they model single/control frames — still real and
still pass via the one-element path). No change to `tests/test_server_smoke.py` /
`scripts/smoke_e2e.py`.

## Deployment

Rebuild/redeploy the Docker image so the fix is live; local `.venv` for tests. After
deploy, `nr_capture_debug` should return captured debug/status messages instead of the
`AttributeError` — completing live `/comms` observability (handshake fix + this).

## Follow-ups

- **Token-mode auth-ack race** (wait for `{"auth":"ok"}` before subscribing) — for
  vanilla Node-RED with `adminAuth`.
- **`wss://` + `NR_VERIFY_SSL`** in `_connect_ws` (carried over from the prior fix).
