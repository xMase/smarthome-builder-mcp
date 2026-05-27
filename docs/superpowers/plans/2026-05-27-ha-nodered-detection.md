# HA Node-RED-Companion Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `ha_check_nodered_integration` tool that reports whether the "Node-RED Companion" HA integration (config-entry domain `nodered`) is set up, reusing the HA WebSocket transport.

**Architecture:** Rename `ha_stats.py` → `ha_ws.py` (now two WS consumers), extract a shared `_auth` handshake, add `check_nodered_integration` beside `get_entity_statistics`, register one MCP tool. Read-only WS probe via `config_entries/get`, filtered client-side; admin-token failure surfaced explicitly.

**Tech Stack:** Python 3.11+, `mcp` SDK (FastMCP), pytest (`asyncio_mode=auto`), `websockets` (lazy import; tests inject a fake `connect`).

---

## File Structure

- **Rename:** `src/nrha_mcp/ha_stats.py` → `src/nrha_mcp/ha_ws.py` — HA WebSocket queries (statistics + integration detection). `HAStatsError` → `HAWsError`.
- **Rename:** `tests/test_ha_stats.py` → `tests/test_ha_ws.py` — stats tests (kept) + new detection tests.
- **Modify:** `src/nrha_mcp/server.py` — import + `get_entity_statistics` call site → `ha_ws`; add `ha_check_nodered_integration` tool.
- **Modify:** `tests/test_server_smoke.py` — add tool name; count 36 → 37.
- **Modify:** `scripts/smoke_e2e.py` — add tool name to `EXPECTED_TOOLS`.

---

## Task 1: Rename `ha_stats` → `ha_ws`, rename exception, extract `_auth`

**Refactor under existing coverage.** No behavior change: the renamed stats tests are the safety net. Frame order on the wire is unchanged, so they stay green.

**Files:**
- Rename: `src/nrha_mcp/ha_stats.py` → `src/nrha_mcp/ha_ws.py`
- Rename: `tests/test_ha_stats.py` → `tests/test_ha_ws.py`
- Modify: `src/nrha_mcp/server.py:10` (import), `src/nrha_mcp/server.py:380` (call site)

- [ ] **Step 1: Rename the module and test file (preserve history)**

```bash
git mv src/nrha_mcp/ha_stats.py src/nrha_mcp/ha_ws.py
git mv tests/test_ha_stats.py tests/test_ha_ws.py
```

- [ ] **Step 2: In `ha_ws.py` — broaden docstring, rename exception, extract `_auth`, refactor `_query_statistics`**

Replace the module docstring (lines 1-6) with:

```python
"""Home Assistant queries over the WebSocket API at ws(s)://<ha>/api/websocket.

Two consumers live here: long-term statistics (recorder/statistics_during_period,
which is not on the REST API) and Node-RED Companion integration detection
(config_entries/get). The transport sits behind an injectable `connect` so tests feed
frames without a live server (mirrors runtime.py).
"""
```

Rename the exception class:

```python
class HAWsError(NRError):
    """HA WebSocket auth or command failure (an expected, surfaced error)."""
```

Add the shared `_auth` helper (place it just above `_query_statistics`):

```python
async def _auth(conn, token) -> None:
    await conn.recv()  # auth_required greeting
    await conn.send({"type": "auth", "access_token": token})
    auth = await conn.recv()
    if auth.get("type") != "auth_ok":
        raise HAWsError(f"HA WebSocket auth failed: {auth.get('type')}")
```

Refactor `_query_statistics` to call `_auth` and use the renamed exception:

```python
async def _query_statistics(conn, token, *, statistic_id, start_iso, period, types) -> dict:
    await _auth(conn, token)
    await conn.send({
        "id": 1,
        "type": "recorder/statistics_during_period",
        "start_time": start_iso,
        "statistic_ids": [statistic_id],
        "period": period,
        "types": list(types),
    })
    while True:
        msg = await conn.recv()
        if msg.get("id") == 1 and msg.get("type") == "result":
            if not msg.get("success"):
                raise HAWsError(f"statistics query failed: {msg.get('error')}")
            return msg.get("result", {})
```

- [ ] **Step 3: Update `server.py` import and call site**

`src/nrha_mcp/server.py:10` — change `ha_stats` to `ha_ws`:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics, ha_ws, subflow_edit
```

`src/nrha_mcp/server.py:380` — change the call:

```python
    return await _wrap(lambda: ha_ws.get_entity_statistics(
```

(only the module prefix changes; leave the rest of the call unchanged.)

- [ ] **Step 4: Update `tests/test_ha_ws.py` imports and references**

Lines 5-6 become:

```python
from nrha_mcp import ha_ws
from nrha_mcp.ha_ws import HAWsError
```

Then replace every remaining `ha_stats.` with `ha_ws.` and `HAStatsError` with `HAWsError` in the file (the `_ws_url` test, the four `get_entity_statistics` calls, and the `pytest.raises(HAStatsError)`).

- [ ] **Step 5: Run the full suite to verify the refactor is green at 36 tools**

Run: `.venv/bin/pytest -q`
Expected: PASS (all existing tests, including the renamed `test_ha_ws.py`; `test_server_smoke.py` still asserts `len(names) == 36`).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: rename ha_stats -> ha_ws, extract shared _auth

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `check_nodered_integration` + unit tests

**Files:**
- Modify: `src/nrha_mcp/ha_ws.py` (add function)
- Test: `tests/test_ha_ws.py` (add detection tests + helpers)

- [ ] **Step 1: Write the first failing test (present + loaded)**

Add to `tests/test_ha_ws.py` (helpers first, then the test):

```python
def _entry(domain, entry_id, title, state):
    return {"domain": domain, "entry_id": entry_id, "title": title, "state": state}


def _entries_result(entries):
    return {"id": 1, "type": "result", "success": True, "result": entries}


@pytest.mark.asyncio
async def test_nodered_present_and_loaded():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _entries_result([_entry("sun", "s1", "Sun", "loaded"),
                               _entry("nodered", "n1", "Node-RED", "loaded")])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert res["installed"] is True
    assert res["entries"] == [{"entry_id": "n1", "title": "Node-RED", "state": "loaded"}]
    assert "loaded" in res["note"].lower()
    assert {"id": 1, "type": "config_entries/get"} in conn.sent
    assert conn.closed is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_nodered_present_and_loaded -v`
Expected: FAIL with `AttributeError: module 'nrha_mcp.ha_ws' has no attribute 'check_nodered_integration'`.

- [ ] **Step 3: Implement `check_nodered_integration` in `ha_ws.py`**

Add at the end of `ha_ws.py`:

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
                "(Settings -> Devices & Services). It is a manual HA step - no tool can "
                "create it - and it must be present before ha-switch/ha-button/ha-sensor "
                "nodes can publish entities to HA.")
    elif not loaded:
        note = ("Integration present but not loaded (state is not 'loaded'); entities "
                "from Node-RED won't appear until it loads. Check it in HA.")
    else:
        note = "Integration present and loaded; Node-RED can publish entities to HA."
    return {"installed": installed, "entries": out_entries, "note": note}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_nodered_present_and_loaded -v`
Expected: PASS.

- [ ] **Step 5: Add the remaining detection tests**

Append to `tests/test_ha_ws.py`:

```python
@pytest.mark.asyncio
async def test_nodered_present_not_loaded():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _entries_result([_entry("nodered", "n1", "Node-RED", "setup_error")])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert res["installed"] is True
    assert res["entries"][0]["state"] == "setup_error"
    assert "not loaded" in res["note"].lower()


@pytest.mark.asyncio
async def test_nodered_absent():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _entries_result([_entry("sun", "s1", "Sun", "loaded")])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert res["installed"] is False
    assert res["entries"] == []
    assert "not set up" in res["note"].lower()


@pytest.mark.asyncio
async def test_nodered_unauthorized_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": False, "error": {"code": "unauthorized"}}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAWsError, match="admin"):
        await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert conn.closed is True
```

- [ ] **Step 6: Run all `ha_ws` tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ha_ws.py -v`
Expected: PASS (stats tests + four detection tests).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: check_nodered_integration WS probe in ha_ws

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Register the `ha_check_nodered_integration` tool

**Files:**
- Modify: `tests/test_server_smoke.py` (expected set + count)
- Modify: `src/nrha_mcp/server.py` (new `@mcp.tool()`)
- Modify: `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`)

- [ ] **Step 1: Update the smoke test to expect the new tool (failing first)**

In `tests/test_server_smoke.py`, add `"ha_check_nodered_integration"` to the `expected` set (next to the other `ha_*` names) and change the count:

```python
    assert len(names) == 37
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `expected.issubset(names)` is False (tool not registered) and/or `len(names) == 37` is False (still 36).

- [ ] **Step 3: Register the tool in `server.py`**

Add next to the other `ha_*` tools (e.g. after `ha_get_state`):

```python
@mcp.tool()
async def ha_check_nodered_integration() -> str:
    """Check whether the 'Node-RED Companion' integration is set up in Home Assistant.
    ha-switch/ha-button/ha-sensor nodes can only publish entities to HA when this
    integration is installed (Settings -> Devices & Services) - a manual HA step no tool
    can perform. Read-only; requires an admin HA token. Returns
    {installed, entries:[{entry_id,title,state}], note}."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA integration check needs HA_URL and HA_TOKEN to be set.")
    return await _wrap(lambda: ha_ws.check_nodered_integration(settings))
```

- [ ] **Step 4: Run the smoke test to verify it passes**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: PASS (`len(names) == 37`, tool present).

- [ ] **Step 5: Add the tool to the e2e inventory**

In `scripts/smoke_e2e.py`, add to the `EXPECTED_TOOLS` set (next to the other `ha_*` entries):

```python
    "ha_check_nodered_integration",
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (all tests).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: register ha_check_nodered_integration tool (37 tools)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **Do not** add a `domain` parameter to the `config_entries/get` payload — filter
  client-side (`e.get("domain") == "nodered"`). This is deliberate for HA-version
  robustness.
- The `_err`/`_wrap`/`settings` names already exist in `server.py`; the new tool
  mirrors `ha_get_state`'s guard exactly (but does **not** open a `HARestClient` — it
  uses the WS transport via `ha_ws`).
- After Task 1, the suite must be green at **36** tools; the count moves to **37** only
  in Task 3.
