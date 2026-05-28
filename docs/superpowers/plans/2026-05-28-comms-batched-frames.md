# `/comms` Batched Frame Handling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `capture_debug` handle Node-RED's batched `/comms` array frames (`[{topic,data}, …]`) so `nr_capture_debug` stops failing with `AttributeError: 'list' object has no attribute 'get'`.

**Architecture:** Iterate over each item of a received frame (a non-list frame is treated as a one-element list) in `capture_debug`; add a non-dict guard to `_normalize`. Both array (data) and single-dict (control) frames are real on the wire.

**Tech Stack:** Python 3.11+, pytest (`asyncio_mode=auto`), `FakeComms` test double in `tests/test_runtime.py`. No new tool — count stays 37.

---

## File Structure

- **Modify:** `src/nrha_mcp/runtime.py` — `capture_debug` loop (~line 123) + `_normalize` guard (~line 54).
- **Test:** `tests/test_runtime.py` — `_batch` helper + 2 batch tests.

No change to `tests/test_server_smoke.py` / `scripts/smoke_e2e.py`.

---

## Task 1: Iterate batched array frames

**Files:**
- Test: `tests/test_runtime.py`
- Modify: `src/nrha_mcp/runtime.py` (`capture_debug` @~115-130, `_normalize` @~54)

- [ ] **Step 1: Write the failing batch test**

Add a helper near the other frame helpers in `tests/test_runtime.py`:

```python
def _batch(*frames):
    return list(frames)
```

Append the test:

```python
@pytest.mark.asyncio
async def test_capture_handles_batched_array_frame(fake_client):
    comms = FakeComms([_batch(_debug_frame(msg="a"), _debug_frame(msg="b"))])
    res = await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=10, connect=_connect_factory(comms))
    assert res["count"] == 2
    assert [m["msg"] for m in res["captured"]] == ["a", "b"]
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `.venv/bin/pytest tests/test_runtime.py::test_capture_handles_batched_array_frame -v`
Expected: FAIL with `AttributeError: 'list' object has no attribute 'get'` (the list frame hits `_normalize`'s `frame.get`).

- [ ] **Step 3: Implement — iterate items in `capture_debug`**

In `src/nrha_mcp/runtime.py`, the capture loop currently reads (after the `recv()` /
`TimeoutError` block):

```python
            msg = _normalize(frame)
            if msg is None:
                continue
            if node_id and msg["node_id"] != node_id:
                continue
            if flow_id and msg["flow_id"] != flow_id:
                continue
            captured.append(msg)
```

Replace those lines with a loop over the frame's items:

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

(The `while len(captured) < max_messages:` loop and the `recv()`/deadline code above it
stay exactly as they are.)

- [ ] **Step 4: Add the `_normalize` non-dict guard**

In `_normalize` (top of the function, currently starts with `topic = frame.get("topic", "")`), add the guard as the first line:

```python
def _normalize(frame: dict) -> dict | None:
    """Map a single /comms message to our dict, or None if not a debug/status/notification frame."""
    if not isinstance(frame, dict):
        return None
    topic = frame.get("topic", "")
    data = frame.get("data", {}) or {}
```

(Leave the rest of `_normalize` unchanged.)

- [ ] **Step 5: Run the batch test, confirm it passes**

Run: `.venv/bin/pytest tests/test_runtime.py::test_capture_handles_batched_array_frame -v` → PASS (`count == 2`, msgs `["a","b"]`).

- [ ] **Step 6: Add the node_id-within-batch test**

Append to `tests/test_runtime.py`:

```python
@pytest.mark.asyncio
async def test_capture_filters_node_id_within_batch(fake_client):
    comms = FakeComms([_batch(_debug_frame(node_id="d1", msg="keep"),
                              _debug_frame(node_id="d2", msg="drop"))])
    res = await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=10, node_id="d1",
        connect=_connect_factory(comms))
    assert res["count"] == 1
    assert res["captured"][0]["msg"] == "keep"
```

- [ ] **Step 7: Run the full suite (no regressions)**

Run: `.venv/bin/pytest -q`
Expected: all PASS. The existing dict-frame tests (`test_capture_collects_debug`,
`test_capture_filters_by_node_id`, `test_capture_notification`, etc.) still pass via the
one-element path; the two new batch tests pass.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "fix: handle batched /comms array frames in capture_debug

Node-RED sends /comms data messages as arrays; iterate each item (a
non-list frame is treated as one element) and guard _normalize against
non-dict input. Fixes AttributeError: 'list' object has no attribute 'get'.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- Keep the existing dict-frame tests; they model single/control frames (e.g. the
  `{"auth":"ok"}` ack) which are also real on the wire. Do NOT convert them to arrays.
- The inner `break` on `max_messages` is intentional — it caps mid-batch (the rest of
  that array is dropped once the limit is reached).
- Only `src/nrha_mcp/runtime.py` and `tests/test_runtime.py` change. No new tool, no
  `server.py`/smoke edits, and do NOT touch the `{auth}`/`{subscribe}` send path.
