# Runtime Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface Node-RED runtime/system events (deploy outcomes, runtime-state, node/module problems) by extending `capture_debug` with the `notification` channel and adding a `nr_get_runtime_events` tool.

**Architecture:** Extend the existing `/comms` capture in `runtime.py` to support the `notification/#` topic, normalizing those frames generically. Add a thin `get_runtime_events` wrapper that captures only the notification channel, and register it as an MCP tool. No new transport — reuses everything from `feat/runtime-observation`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), `websockets`, pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-runtime-events-design.md`

---

## File structure

- Modify `src/nrha_mcp/runtime.py` — add a subscribe-topic map, a `notification` branch in `_normalize`, and a `get_runtime_events` wrapper.
- Modify `src/nrha_mcp/server.py` — update the `nr_capture_debug` docstring; add the `nr_get_runtime_events` tool.
- Modify `tests/test_runtime.py` — tests for notification capture and the wrapper.
- Modify `tests/test_server_smoke.py` — tool count 23 → 24.
- Modify `scripts/smoke_e2e.py` — add the new tool to `EXPECTED_TOOLS`.

All commands run from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv-created venv, no `pip`). Branch: `feat/runtime-events`.

---

## Task 1: Support the `notification` topic in `capture_debug`

**Files:**
- Modify: `src/nrha_mcp/runtime.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_runtime.py`, add a frame helper next to the existing `_debug_frame` helper:

```python
def _notification_frame(subtopic="runtime-deploy", data=None):
    return {"topic": f"notification/{subtopic}", "data": data if data is not None else {"revision": "abc"}}
```

And add these two tests at the end of the file:

```python
@pytest.mark.asyncio
async def test_capture_notification(fake_client):
    comms = FakeComms([_notification_frame("runtime-deploy", {"revision": "r1"})])
    res = await runtime.capture_debug(
        fake_client, topics=["notification"], window_ms=500, max_messages=1,
        connect=_connect_factory(comms))
    assert res["count"] == 1
    ev = res["captured"][0]
    assert ev["topic"] == "notification"
    assert ev["subtopic"] == "runtime-deploy"
    assert ev["data"] == {"revision": "r1"}
    assert {"subscribe": "notification/#"} in comms.sent


@pytest.mark.asyncio
async def test_notification_dropped_by_node_filter(fake_client):
    comms = FakeComms([_notification_frame()])
    res = await runtime.capture_debug(
        fake_client, topics=["notification"], window_ms=50, node_id="someNode",
        connect=_connect_factory(comms))
    assert res["count"] == 0
```

(`FakeComms` and `_connect_factory` already exist in this file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_runtime.py -k "notification" -v`
Expected: FAIL — `test_capture_notification` fails because the subscribe message is `{"subscribe": "notification"}` (not `notification/#`) and `_normalize` returns `None` for the frame, so `count == 0`.

- [ ] **Step 3: Add the subscribe-topic map**

In `src/nrha_mcp/runtime.py`, add a module-level constant right after `_EMPTY_HINT` (near the top):

```python
# Topic shorthands -> the actual /comms subscription string.
_SUBSCRIBE_TOPIC = {"status": "status/#", "notification": "notification/#"}
```

Then change the subscribe loop inside `capture_debug` from:

```python
        for t in topics:
            await conn.send({"subscribe": "status/#" if t == "status" else t})
```

to:

```python
        for t in topics:
            await conn.send({"subscribe": _SUBSCRIBE_TOPIC.get(t, t)})
```

- [ ] **Step 4: Add the `notification` branch to `_normalize`**

In `src/nrha_mcp/runtime.py`, in `_normalize`, add this branch immediately before the final `return None`:

```python
    if topic.startswith("notification/"):
        return {
            "topic": "notification",
            "subtopic": topic.split("/", 1)[1],
            "node_id": None,
            "flow_id": None,
            "data": data,
            "ts": time.time(),
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_runtime.py -v`
Expected: all pass (the two new tests plus the existing ones).

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/runtime.py tests/test_runtime.py
git commit -m "feat(runtime): support notification topic in capture_debug"
```

---

## Task 2: `get_runtime_events` wrapper

**Files:**
- Modify: `src/nrha_mcp/runtime.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_runtime.py`, add at the end of the file:

```python
@pytest.mark.asyncio
async def test_get_runtime_events_returns_events(fake_client):
    comms = FakeComms([_notification_frame("runtime-state", {"state": "start"})])
    res = await runtime.get_runtime_events(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert res["count"] == 1
    assert res["events"][0]["subtopic"] == "runtime-state"
    assert "hint" not in res


@pytest.mark.asyncio
async def test_get_runtime_events_empty_has_hint(fake_client):
    comms = FakeComms([])
    res = await runtime.get_runtime_events(
        fake_client, window_ms=50, connect=_connect_factory(comms))
    assert res["count"] == 0
    assert "hint" in res
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_runtime.py -k "runtime_events" -v`
Expected: FAIL with `AttributeError: module 'nrha_mcp.runtime' has no attribute 'get_runtime_events'`

- [ ] **Step 3: Implement the wrapper**

In `src/nrha_mcp/runtime.py`, add this function at the end of the file (after `capture_debug`):

```python
async def get_runtime_events(
    client: NRClient,
    *,
    window_ms: int = 2000,
    max_messages: int = 100,
    connect: Callable[[NRClient], Awaitable] | None = None,
) -> dict:
    """Capture only the /comms notification channel (runtime/system events)."""
    res = await capture_debug(
        client, topics=["notification"], window_ms=window_ms,
        max_messages=max_messages, connect=connect)
    events = res["captured"]
    out = {"events": events, "count": len(events), "window_ms": window_ms}
    if not events:
        out["hint"] = (
            "No runtime events in the window. That is normal when nothing happened "
            "(no deploy, no state change, no module/node error). Trigger the action "
            "during the window, or widen window_ms."
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_runtime.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/runtime.py tests/test_runtime.py
git commit -m "feat(runtime): get_runtime_events wrapper (notification channel only)"
```

---

## Task 3: Register `nr_get_runtime_events` and update the tool-set assertions

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"nr_get_runtime_events",` to the `expected` set (e.g. right after `"nr_capture_debug",`) and change the count assertion. The two changed lines become:

```python
        "nr_capture_debug", "nr_get_runtime_events", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
```

```python
    assert len(names) == 24
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `nr_get_runtime_events` not registered and `len(names)` is 23, not 24.

- [ ] **Step 3: Update the `nr_capture_debug` docstring**

In `src/nrha_mcp/server.py`, change the last line of the `nr_capture_debug` docstring from:

```python
    Without `inject_node_id` it passively listens for the window."""
```

to:

```python
    Without `inject_node_id` it passively listens for the window. `topics` selects
    channels: "debug", "status", "notification" (default ["debug", "status"])."""
```

- [ ] **Step 4: Add the new tool**

In `src/nrha_mcp/server.py`, immediately after the `nr_capture_debug` function (after its closing `))`), add:

```python


@mcp.tool()
async def nr_get_runtime_events(window_ms: int = 2000, max_messages: int = 100) -> str:
    """Capture Node-RED runtime/system events from the /comms `notification/#` channel.

    Surfaces deploy outcomes, runtime-state changes (flows started/stopped, safe
    mode) and node/module problems (failed loads, unregistered types). Use it right
    after a deploy to check nothing broke. It listens passively for `window_ms`, so
    trigger the action during the window. (Does not filter by severity yet; returns
    events by source.)"""
    return await _wrap(lambda: runtime.get_runtime_events(
        client, window_ms=window_ms, max_messages=max_messages))
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 6: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"nr_get_runtime_events",` to the `EXPECTED_TOOLS` set (e.g. right after `"nr_capture_debug",`). The script asserts `len(EXPECTED_TOOLS)` tools are present, so adding the name keeps it consistent.

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add nr_get_runtime_events tool"
```

---

## Task 4: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('runtime_events:', 'nr_get_runtime_events' in n); print('count:', len(n))"`
Expected: `runtime_events: True` and `count: 24`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the new tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack with the rebuilt image). This restarts the MCP, so do it when ready.

- [ ] **Step 4: Live shape check (after redeploy)**

Call `nr_get_runtime_events(window_ms=4000)` and, during the window, deploy any small change. Confirm a `notification/runtime-deploy` (or similar) event appears, and note the real payload shape — this feeds the deferred severity follow-up.

---

## Follow-up (deferred, per spec)

Once the live `notification/*` and `debug` frame shapes are confirmed, add optional severity filtering (error/warning) and optionally surface error-level `debug` entries; an honest `nr_get_runtime_errors` alias becomes possible then.

---

## Self-review

- **Spec coverage:** Part 1 (extend capture with `notification`) → Task 1. Part 2 (`get_runtime_events`, notification-only) → Task 2. Part 3 (register tool, docstring, 23→24 in both assertion sites) → Task 3. Non-goal (no severity) respected — `_normalize` keeps `data` raw, wrapper does no level filtering. Deploy + live shape check → Task 4. Severity follow-up → Follow-up section. No gaps.
- **Placeholders:** none — every code/test step has full code and exact commands.
- **Type consistency:** the normalized notification dict (`topic`/`subtopic`/`node_id`/`flow_id`/`data`/`ts`) is identical in Task 1's `_normalize`, Task 1's tests, and consumed unchanged by Task 2's wrapper (which reads `res["captured"]`). `get_runtime_events(client, *, window_ms, max_messages, connect)` signature matches between Task 2's definition, its tests, and Task 3's `server.py` wrapper. Tool count 24 consistent across Task 3 and Task 4.
