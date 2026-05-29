# `ha_ws_command` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single generic MCP tool `ha_ws_command(command, params, confirm)` that sends an arbitrary Home Assistant WebSocket command, gated by a preview→confirm safety pattern and the *shared* service floor, so the agent stops crafting throwaway Node-RED nodes to mutate the HA registry.

**Architecture:** One new function `run_command` (plus small helpers) in `src/nrha_mcp/ha_ws.py`, reusing the existing `_default_connect`/`_HAWsConn`/`_auth` transport and the injectable `connect` test seam. One thin tool in `server.py` delegating via `_wrap`. `call_service` issued over WS is routed through `ha.py`'s existing `_service_tier` so the catastrophic floor cannot be bypassed. Two new `Settings` fields back the WS read-allowlist and block-list, surfaced in `nr_diagnostics`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp` SDK), `websockets` (already a dep), pydantic-settings, pytest / pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-05-30-ha-ws-command-design.md`

---

## File Structure

- **Modify** `src/nrha_mcp/config.py` — add two `Settings` fields (`ha_ws_block_commands`, `ha_ws_read_commands`).
- **Modify** `src/nrha_mcp/ha_ws.py` — add `run_command`, classification/policy helpers (`_read_commands`, `_is_read`, `_ws_block_set`, `_preview`, `_execute`, `effective_policy`).
- **Modify** `src/nrha_mcp/diagnostics.py` — surface `ha_ws.effective_policy(settings)`.
- **Modify** `src/nrha_mcp/server.py` — add the `@mcp.tool() ha_ws_command` wrapper.
- **Modify** `tests/test_server_smoke.py` — add `ha_ws_command` to `expected`, bump `len(names)` 39→40.
- **Modify** `scripts/smoke_e2e.py` — add `ha_ws_command` to `EXPECTED_TOOLS`.
- **Modify** `tests/test_ha_ws.py` — add tests for `run_command` (reuses the existing `FakeHAWsConn`).
- **Modify** `README.md` — Tools table row + tool count, env var docs.

---

## Task 1: Settings fields for the WS policy

**Files:**
- Modify: `src/nrha_mcp/config.py` (after the existing `ha_confirm_services` line, ~line 31)
- Test: `tests/test_ha_ws.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ha_ws.py`:

```python
def test_settings_have_ws_policy_defaults():
    s = Settings(ha_url="http://ha:8123", ha_token="tok")
    assert s.ha_ws_block_commands == ""
    assert s.ha_ws_read_commands is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_settings_have_ws_policy_defaults -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ha_ws_block_commands'`

- [ ] **Step 3: Add the fields**

In `src/nrha_mcp/config.py`, immediately after the line `ha_confirm_services: str | None = None  # override-with-default: None=default, ""=gate nothing`:

```python

    # Home Assistant WebSocket command safety (ha_ws_command)
    ha_ws_block_commands: str = ""            # additive block-list of WS command types
    ha_ws_read_commands: str | None = None    # additive to the read allowlist (None=just defaults)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_settings_have_ws_policy_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/config.py tests/test_ha_ws.py
git commit -m "feat(config): add ha_ws_command policy settings"
```

---

## Task 2: `run_command` helpers + read path

**Files:**
- Modify: `src/nrha_mcp/ha_ws.py` (append after `check_nodered_integration`)
- Test: `tests/test_ha_ws.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ha_ws.py`:

```python
@pytest.mark.asyncio
async def test_run_command_read_executes_without_confirm():
    # a "/list" command is a read -> runs immediately, no confirm needed
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": True,
               "result": [{"entity_id": "button.x"}]}]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.run_command(
        _settings(), "config/entity_registry/list", connect=_connect_factory(conn))
    assert res["ok"] is True
    assert res["command"] == "config/entity_registry/list"
    assert res["result"] == [{"entity_id": "button.x"}]
    assert {"id": 1, "type": "config/entity_registry/list"} in conn.sent
    assert conn.closed is True


@pytest.mark.asyncio
async def test_run_command_read_failure_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": False, "error": {"code": "boom"}}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAWsError, match="failed"):
        await ha_ws.run_command(
            _settings(), "config/entity_registry/list", connect=_connect_factory(conn))
    assert conn.closed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_run_command_read_executes_without_confirm -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.ha_ws' has no attribute 'run_command'`

- [ ] **Step 3: Add helpers + `_execute` + read path**

Append to `src/nrha_mcp/ha_ws.py`:

```python
# --- Generic WS command (ha_ws_command) ----------------------------------

# Commands recognized as read-only (execute without confirm). Anything not here
# and not ending in /list or /get is treated as a write (preview -> confirm).
READ_COMMANDS = {
    "get_states", "get_config", "get_services",
    "render_template", "recorder/statistics_during_period",
}


def _split_csv(raw: str) -> set[str]:
    return {t.strip() for t in (raw or "").split(",") if t.strip()}


def _read_commands(settings) -> set[str]:
    return READ_COMMANDS | _split_csv(settings.ha_ws_read_commands or "")


def _is_read(command: str, settings) -> bool:
    if command.endswith("/list") or command.endswith("/get"):
        return True
    return command in _read_commands(settings)


def _ws_block_set(settings) -> set[str]:
    return _split_csv(settings.ha_ws_block_commands)


def _preview(command: str, params: dict) -> dict:
    return {
        "preview": True,
        "would_send": {"type": command, **params},
        "requires_confirm": True,
        "hint": "re-call with confirm=true to execute.",
    }


async def _execute(settings, command: str, params: dict, connect) -> dict:
    conn = await (connect or _default_connect)(settings)
    try:
        await _auth(conn, settings.ha_token)
        await conn.send({"id": 1, "type": command, **params})
        while True:
            msg = await conn.recv()
            if msg.get("id") == 1 and msg.get("type") == "result":
                if not msg.get("success"):
                    raise HAWsError(f"{command} failed: {msg.get('error')}")
                return {"ok": True, "command": command, "result": msg.get("result")}
    finally:
        await conn.close()


async def run_command(settings, command: str, params: dict | None = None,
                      *, confirm: bool = False, connect=None) -> dict:
    """Send an arbitrary HA WebSocket command. Reads run immediately; writes return a
    preview unless confirm=true. call_service obeys the shared catastrophic floor."""
    params = dict(params or {})
    if _is_read(command, settings):
        return await _execute(settings, command, params, connect)
    if not confirm:
        return _preview(command, params)
    return await _execute(settings, command, params, connect)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_run_command_read_executes_without_confirm tests/test_ha_ws.py::test_run_command_read_failure_raises -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha_ws.py tests/test_ha_ws.py
git commit -m "feat(ha_ws): run_command read path + helpers"
```

---

## Task 3: Write gate (preview → confirm) + WS block-list

**Files:**
- Modify: `src/nrha_mcp/ha_ws.py` (`run_command`)
- Test: `tests/test_ha_ws.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ha_ws.py`:

```python
@pytest.mark.asyncio
async def test_run_command_write_without_confirm_previews():
    # auth frame present but must NOT be consumed: nothing is sent past connect
    conn = FakeHAWsConn([{"type": "auth_required"}, {"type": "auth_ok"}])
    res = await ha_ws.run_command(
        _settings(), "config/entity_registry/remove",
        params={"entity_id": "button.x"}, connect=_connect_factory(conn))
    assert res["preview"] is True
    assert res["requires_confirm"] is True
    assert res["would_send"] == {"type": "config/entity_registry/remove",
                                 "entity_id": "button.x"}
    assert conn.sent == []          # nothing sent
    assert conn.closed is False     # connection never opened


@pytest.mark.asyncio
async def test_run_command_write_with_confirm_executes():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": True, "result": None}]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.run_command(
        _settings(), "config/entity_registry/remove",
        params={"entity_id": "button.x"}, confirm=True, connect=_connect_factory(conn))
    assert res["ok"] is True
    assert {"id": 1, "type": "config/entity_registry/remove",
            "entity_id": "button.x"} in conn.sent
    assert conn.closed is True


@pytest.mark.asyncio
async def test_run_command_blocklisted_raises_even_with_confirm():
    settings = Settings(ha_url="http://ha:8123", ha_token="tok",
                        ha_ws_block_commands="config/core/update")
    conn = FakeHAWsConn([{"type": "auth_required"}, {"type": "auth_ok"}])
    with pytest.raises(HAWsError, match="HA_WS_BLOCK_COMMANDS"):
        await ha_ws.run_command(
            settings, "config/core/update", confirm=True, connect=_connect_factory(conn))
    assert conn.sent == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ha_ws.py -k "write_without_confirm or write_with_confirm or blocklisted" -v`
Expected: FAIL — preview test fails (write path currently executes & errors on empty deque) and blocklist test fails (no block check yet)

- [ ] **Step 3: Add the block-list check to `run_command`**

In `src/nrha_mcp/ha_ws.py`, replace the body of `run_command` (the part after `params = dict(params or {})`) with:

```python
    if _is_read(command, settings):
        return await _execute(settings, command, params, connect)
    if command in _ws_block_set(settings):
        raise HAWsError(
            f"{command} is in HA_WS_BLOCK_COMMANDS and cannot be executed; "
            "confirm=true does not override it.")
    if not confirm:
        return _preview(command, params)
    return await _execute(settings, command, params, connect)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ha_ws.py -k "write_without_confirm or write_with_confirm or blocklisted" -v`
Expected: PASS (all three)

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha_ws.py tests/test_ha_ws.py
git commit -m "feat(ha_ws): write preview/confirm gate + WS block-list"
```

---

## Task 4: Shared service floor for `call_service` over WS

**Files:**
- Modify: `src/nrha_mcp/ha_ws.py` (`run_command`)
- Test: `tests/test_ha_ws.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ha_ws.py`:

```python
@pytest.mark.asyncio
async def test_call_service_catastrophic_blocked_even_with_confirm():
    # homeassistant.stop is in ha.py CATASTROPHIC_FLOOR; WS must not bypass it
    conn = FakeHAWsConn([{"type": "auth_required"}, {"type": "auth_ok"}])
    with pytest.raises(HAWsError, match="catastrophic"):
        await ha_ws.run_command(
            _settings(), "call_service",
            params={"domain": "homeassistant", "service": "stop"},
            confirm=True, connect=_connect_factory(conn))
    assert conn.sent == []


@pytest.mark.asyncio
async def test_call_service_sensitive_previews_without_confirm():
    # lock.unlock is in ha.py DEFAULT_CONFIRM -> preview unless confirm
    conn = FakeHAWsConn([{"type": "auth_required"}, {"type": "auth_ok"}])
    res = await ha_ws.run_command(
        _settings(), "call_service",
        params={"domain": "lock", "service": "unlock", "service_data": {"entity_id": "lock.x"}},
        connect=_connect_factory(conn))
    assert res["preview"] is True
    assert conn.sent == []


@pytest.mark.asyncio
async def test_call_service_missing_domain_raises():
    conn = FakeHAWsConn([{"type": "auth_required"}, {"type": "auth_ok"}])
    with pytest.raises(HAWsError, match="domain"):
        await ha_ws.run_command(
            _settings(), "call_service", params={"service": "turn_on"},
            confirm=True, connect=_connect_factory(conn))


@pytest.mark.asyncio
async def test_call_service_direct_executes():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": True, "result": {"context": {}}}]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.run_command(
        _settings(), "call_service",
        params={"domain": "light", "service": "turn_on"},
        connect=_connect_factory(conn))   # direct tier, no confirm needed
    assert res["ok"] is True
    assert {"id": 1, "type": "call_service",
            "domain": "light", "service": "turn_on"} in conn.sent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ha_ws.py -k "call_service" -v`
Expected: FAIL — catastrophic test fails (currently `call_service` is a write, so confirm=true executes and errors on the empty deque instead of raising "catastrophic")

- [ ] **Step 3: Add the `call_service` branch to `run_command`**

In `src/nrha_mcp/ha_ws.py`, insert this block at the very start of `run_command`'s body, right after `params = dict(params or {})`:

```python
    # call_service over WS must obey the SAME floor as ha_call_service (no bypass).
    if command == "call_service":
        from .ha import _service_tier  # reuse CATASTROPHIC_FLOOR / DEFAULT_CONFIRM
        domain, service = params.get("domain"), params.get("service")
        if not domain or not service:
            raise HAWsError("call_service requires 'domain' and 'service' in params.")
        tier = _service_tier(domain, service, settings)
        if tier == "blocked":
            raise HAWsError(
                f"{domain}.{service} is in the catastrophic block list and cannot be "
                "executed; confirm=true does not override it.")
        if tier == "confirm" and not confirm:
            return _preview(command, params)
        return await _execute(settings, command, params, connect)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ha_ws.py -k "call_service" -v`
Expected: PASS (all four)

- [ ] **Step 5: Run the full ha_ws suite**

Run: `.venv/bin/pytest tests/test_ha_ws.py -v`
Expected: PASS (all, including the pre-existing statistics/nodered tests)

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/ha_ws.py tests/test_ha_ws.py
git commit -m "feat(ha_ws): route WS call_service through the shared service floor"
```

---

## Task 5: Surface the WS policy in `nr_diagnostics`

**Files:**
- Modify: `src/nrha_mcp/ha_ws.py` (add `effective_policy`)
- Modify: `src/nrha_mcp/diagnostics.py` (~line 60)
- Test: `tests/test_ha_ws.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ha_ws.py`:

```python
def test_ws_effective_policy_reports_read_and_block():
    s = Settings(ha_url="http://ha:8123", ha_token="tok",
                 ha_ws_block_commands="config/core/update",
                 ha_ws_read_commands="my/custom_read")
    pol = ha_ws.effective_policy(s)
    assert "my/custom_read" in pol["read_commands"]
    assert "recorder/statistics_during_period" in pol["read_commands"]  # default kept
    assert pol["blocked_commands"] == ["config/core/update"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_ws_effective_policy_reports_read_and_block -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.ha_ws' has no attribute 'effective_policy'`

- [ ] **Step 3: Add `effective_policy` to `ha_ws.py`**

Append to `src/nrha_mcp/ha_ws.py`:

```python
def effective_policy(settings) -> dict:
    """Resolved WS-command policy for observability (nr_diagnostics)."""
    return {
        "read_commands": sorted(_read_commands(settings)),
        "blocked_commands": sorted(_ws_block_set(settings)),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ha_ws.py::test_ws_effective_policy_reports_read_and_block -v`
Expected: PASS

- [ ] **Step 5: Wire it into diagnostics**

First confirm `diagnostics.py` imports `ha_ws`:

Run: `grep -n "import" src/nrha_mcp/diagnostics.py | head`

If `ha_ws` is not in the imports, add it to the existing relative import line (e.g. change `from . import ha` to `from . import ha, ha_ws`, matching the file's current style).

Then in `src/nrha_mcp/diagnostics.py`, immediately after the line `out["ha_call_policy"] = ha.effective_policy(settings)`:

```python
    out["ha_ws_policy"] = ha_ws.effective_policy(settings)
```

- [ ] **Step 6: Run the diagnostics test suite**

Run: `.venv/bin/pytest tests/ -k diagnostics -v`
Expected: PASS (no regressions; if a diagnostics test asserts an exact key set, add `ha_ws_policy` to its expectation)

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/ha_ws.py src/nrha_mcp/diagnostics.py tests/test_ha_ws.py
git commit -m "feat(diagnostics): surface ha_ws_command policy"
```

---

## Task 6: Register the `ha_ws_command` MCP tool + sync the count

**Files:**
- Modify: `src/nrha_mcp/server.py` (after `ha_check_nodered_integration`, ~line 437)
- Modify: `tests/test_server_smoke.py` (`expected` set ~line 16, `len(names)` ~line 21)
- Modify: `scripts/smoke_e2e.py` (`EXPECTED_TOOLS` ~line 61)

- [ ] **Step 1: Update the count guard test first (expect failure)**

In `tests/test_server_smoke.py`, add `"ha_ws_command"` to the `expected` set on the line with the other `ha_*` tools (after `"ha_check_nodered_integration", "ha_call_service",`), and change `assert len(names) == 39` to `assert len(names) == 40`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_server_smoke.py tests/test_tool_count_sync.py -v`
Expected: FAIL — `ha_ws_command` is expected but not registered in `server.py` (count/sync mismatch)

- [ ] **Step 3: Register the tool in `server.py`**

In `src/nrha_mcp/server.py`, immediately after the `ha_check_nodered_integration` function (before `def main():`):

```python
@mcp.tool()
async def ha_ws_command(command: str, params: dict | None = None,
                        confirm: bool = False) -> str:
    """Send a raw Home Assistant WebSocket command (escape hatch for capabilities not
    covered by other ha_* tools — e.g. registry edits like config/entity_registry/remove
    or config/entity_registry/update). Requires HA_URL + HA_TOKEN.

    `command` is the WS message "type"; `params` are merged into the message (e.g.
    {"entity_id": "..."}). Reads (types ending in /list or /get, plus get_states /
    get_config / get_services / render_template / recorder statistics) run immediately.
    Everything else is a WRITE: it returns a preview and executes only when confirm=true.
    A `call_service` command obeys the same catastrophic/sensitive floor as
    ha_call_service — it cannot bypass it. See nr_diagnostics -> ha_ws_policy."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA ha_ws_command needs HA_URL and HA_TOKEN to be set.")
    return await _wrap(lambda: ha_ws.run_command(settings, command, params, confirm=confirm))
```

- [ ] **Step 4: Update `scripts/smoke_e2e.py`**

In `scripts/smoke_e2e.py`, add `"ha_ws_command",` to the `EXPECTED_TOOLS` list, right after the `"ha_call_service",` line.

- [ ] **Step 5: Run the count/sync guards to verify they pass**

Run: `.venv/bin/pytest tests/test_server_smoke.py tests/test_tool_count_sync.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add ha_ws_command tool (39->40)"
```

---

## Task 7: Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Find the Tools table and count**

Run: `grep -n "ha_call_service\|ha_check_nodered_integration\|Tools\|^| \`ha_" README.md | head -30`

- [ ] **Step 2: Add the tool row + bump the count**

In `README.md`, add a row to the Tools table next to the other `ha_*` tools:

```
| `ha_ws_command` | Send a raw HA WebSocket command (registry edits, etc.); reads run immediately, writes need `confirm=true`; `call_service` obeys the shared service floor. |
```

Bump any "N tools" / table count that currently says the old total to reflect the new total (search the README for the previous number near the Tools heading and increment it).

- [ ] **Step 3: Document the env vars**

In `README.md`, where the other `HA_*` env vars are documented (near `HA_BLOCK_SERVICES` / `HA_CONFIRM_SERVICES`), add:

```
- `HA_WS_BLOCK_COMMANDS` — comma-separated WS command types to block for `ha_ws_command` (additive; `confirm=true` cannot override).
- `HA_WS_READ_COMMANDS` — comma-separated WS command types to treat as read-only (additive to the defaults), so they run without `confirm`.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document ha_ws_command tool and HA_WS_* env vars"
```

---

## Final Verification

- [ ] Run the full unit suite: `.venv/bin/pytest -q` → all pass.
- [ ] (Optional, needs Docker) E2E smoke over real stdio:
  ```bash
  docker compose up -d nodered
  NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py
  docker compose down -v
  ```
  Expected: tool list matches `EXPECTED_TOOLS` (40 tools).
