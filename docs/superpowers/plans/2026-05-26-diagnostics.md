# Diagnostics / Healthcheck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `nr_diagnostics` tool returning a health snapshot (Node-RED reachability/version/runtime-state + context store names, flow counts, node-set count, MCP config, and Home Assistant configured/reachable).

**Architecture:** Add two thin client GETs (`NRClient.get_settings`, `HARestClient.ping`), then a `diagnostics.run(client, settings, *, ha_ping=None)` that composes them with `get_flows`/`get_nodes`, guarding each read. Expose as `nr_diagnostics`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), httpx, pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-diagnostics-design.md`

---

## File structure

- Modify `src/nrha_mcp/client.py` — add `NRClient.get_settings()`.
- Modify `src/nrha_mcp/ha.py` — add `HARestClient.ping()`.
- Create `src/nrha_mcp/diagnostics.py` — `run()` + `_default_ha_ping`.
- Modify `tests/conftest.py` — `FakeNRClient.get_settings` + `settings_payload`.
- Create `tests/test_diagnostics.py`.
- Modify `src/nrha_mcp/server.py` — add the `nr_diagnostics` tool.
- Modify `tests/test_server_smoke.py` — tool count 28 → 29.
- Modify `scripts/smoke_e2e.py` — add the tool to `EXPECTED_TOOLS`.

All commands from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv venv, no `pip`). Branch: `feat/diagnostics`.

---

## Task 1: Thin client GETs

These two methods are thin GET wrappers exercised in production and via fakes/e2e
(consistent with `get_state`/`get_services`, which likewise have no dedicated
respx test). No new unit test here; they are driven by Task 2's fakes.

**Files:**
- Modify: `src/nrha_mcp/client.py`
- Modify: `src/nrha_mcp/ha.py`

- [ ] **Step 1: Add `NRClient.get_settings`**

In `src/nrha_mcp/client.py`, add this method to `NRClient`, right after `get_nodes`:

```python
    async def get_settings(self) -> dict:
        r = await self._do("GET", "/settings", headers={"Accept": "application/json"})
        self._check_response(r)
        return r.json()
```

- [ ] **Step 2: Add `HARestClient.ping`**

In `src/nrha_mcp/ha.py`, add this method to `HARestClient`, right after `get_history` (before `close`):

```python
    async def ping(self) -> dict:
        r = await self._client.get("/api/")
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 3: Sanity check (imports resolve)**

Run: `./.venv/bin/python -c "import nrha_mcp.client, nrha_mcp.ha; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/nrha_mcp/client.py src/nrha_mcp/ha.py
git commit -m "feat(client,ha): get_settings + HA ping GETs for diagnostics"
```

---

## Task 2: `diagnostics.run`

**Files:**
- Create: `src/nrha_mcp/diagnostics.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_diagnostics.py` (create)

- [ ] **Step 1: Extend the fake client**

In `tests/conftest.py`, inside `FakeNRClient.__init__`, add (after `self.post_calls = []`):

```python
        self.settings_payload = {
            "version": "4.1.10",
            "runtimeState": {"enabled": True, "state": "start"},
            "context": {"default": "default", "stores": ["store", "default"]},
        }
```

And add this method to `FakeNRClient` (e.g. after `get_nodes`):

```python
    async def get_settings(self):
        return self.settings_payload
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_diagnostics.py`:

```python
import pytest

from nrha_mcp import diagnostics
from nrha_mcp.client import NRError
from nrha_mcp.config import Settings


@pytest.mark.asyncio
async def test_diagnostics_full_snapshot(fake_client):
    settings = Settings(ha_url="", ha_token="")   # no HA configured
    res = await diagnostics.run(fake_client, settings)
    assert res["node_red"]["reachable"] is True
    assert res["node_red"]["version"] == "4.1.10"
    assert res["node_red"]["context_stores"] == ["store", "default"]
    assert res["node_red"]["context_default"] == "default"
    assert res["flows"]["tabs"] == 1                 # SAMPLE_FLOWS has one tab
    assert res["config"]["deploy_mode"] == settings.deploy_mode
    assert res["home_assistant"] == {"configured": False}


@pytest.mark.asyncio
async def test_diagnostics_nr_unreachable(fake_client):
    async def _raise(*a, **k):
        raise NRError("connection refused")
    fake_client.get_settings = _raise
    settings = Settings(ha_url="", ha_token="")
    res = await diagnostics.run(fake_client, settings)
    assert res["node_red"]["reachable"] is False
    assert "error" in res["node_red"]
    assert "flows" not in res                         # early return


@pytest.mark.asyncio
async def test_diagnostics_ha_reachable(fake_client):
    settings = Settings(ha_url="http://ha:8123", ha_token="tok")

    async def ping_ok(s):
        return {"message": "API running."}

    res = await diagnostics.run(fake_client, settings, ha_ping=ping_ok)
    assert res["home_assistant"]["configured"] is True
    assert res["home_assistant"]["reachable"] is True


@pytest.mark.asyncio
async def test_diagnostics_ha_unreachable(fake_client):
    settings = Settings(ha_url="http://ha:8123", ha_token="tok")

    async def ping_fail(s):
        raise RuntimeError("bad token")

    res = await diagnostics.run(fake_client, settings, ha_ping=ping_fail)
    assert res["home_assistant"]["reachable"] is False
    assert res["home_assistant"]["error"] == "RuntimeError"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_diagnostics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nrha_mcp.diagnostics'`

- [ ] **Step 4: Implement the module**

Create `src/nrha_mcp/diagnostics.py`:

```python
"""Read-only health snapshot of Node-RED plus the MCP's HA/config wiring."""

from .client import NRError
from . import ha


async def _default_ha_ping(settings) -> dict:
    client = ha.HARestClient(settings)
    try:
        return await client.ping()
    finally:
        await client.close()


async def run(client, settings, *, ha_ping=None) -> dict:
    """Gather a read-only diagnostics snapshot; each external read is guarded."""
    try:
        s = await client.get_settings()
    except NRError as e:
        return {"node_red": {"reachable": False, "error": str(e)}}

    ctx = s.get("context") or {}
    out = {
        "node_red": {
            "reachable": True,
            "version": s.get("version"),
            "runtime_state": s.get("runtimeState"),
            "context_stores": ctx.get("stores"),
            "context_default": ctx.get("default"),
        }
    }

    flows, _ = await client.get_flows()
    out["flows"] = {
        "tabs": sum(1 for f in flows if f.get("type") == "tab"),
        "total_objects": len(flows),
    }

    try:
        nodes = await client.get_nodes()
        out["node_sets"] = len(nodes) if isinstance(nodes, list) else None
    except NRError:
        out["node_sets"] = None

    out["config"] = {"deploy_mode": settings.deploy_mode, "backup_dir": settings.backup_dir}

    ha_block = {"configured": bool(settings.ha_url and settings.ha_token)}
    if ha_block["configured"]:
        ping = ha_ping or _default_ha_ping
        try:
            await ping(settings)
            ha_block["reachable"] = True
        except Exception as e:  # noqa: BLE001 - report any failure as not-reachable
            ha_block["reachable"] = False
            ha_block["error"] = type(e).__name__
    out["home_assistant"] = ha_block

    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_diagnostics.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/diagnostics.py tests/conftest.py tests/test_diagnostics.py
git commit -m "feat(diagnostics): read-only health snapshot (run)"
```

---

## Task 3: Register the `nr_diagnostics` tool

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"nr_diagnostics",` to the `expected` set (e.g. after `"nr_inject",`) and change the count:

```python
        "nr_inject", "nr_diagnostics", "ha_list_ha_nodes", "ha_get_server_config",
```

```python
    assert len(names) == 29
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `nr_diagnostics` not registered; `len(names)` is 28, not 29.

- [ ] **Step 3: Import `diagnostics` in `server.py`**

In `src/nrha_mcp/server.py`, change:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy
```

to:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics
```

(`NRError` is already imported in `server.py`.)

- [ ] **Step 4: Add the tool**

In `src/nrha_mcp/server.py`, immediately AFTER the `ha_get_entity_history` function (after its `finally:` block), add:

```python


@mcp.tool()
async def nr_diagnostics() -> str:
    """Read-only health snapshot: Node-RED reachability/version/runtime-state,
    context store names, flow counts, installed node sets, MCP config, and Home
    Assistant configured/reachable. Run it first when something seems off."""
    return await _wrap(lambda: diagnostics.run(client, settings))
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 6: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"nr_diagnostics",` to the `EXPECTED_TOOLS` set (e.g. right after `"nr_inject",`). The script asserts `len(EXPECTED_TOOLS)` tools are present.

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add nr_diagnostics tool"
```

---

## Task 4: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('diagnostics:', 'nr_diagnostics' in n); print('count:', len(n))"`
Expected: `diagnostics: True` and `count: 29`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack). Do it when ready.

---

## Self-review

- **Spec coverage:** client GETs (`get_settings`, `ping`) → Task 1; `diagnostics.run` with all blocks + guards + injectable ha_ping → Task 2; the four block behaviors (full snapshot, NR-unreachable early return, HA reachable, HA unreachable) → Task 2 tests; tool + import + 28→29 in both sites → Task 3; verify + deploy → Task 4. No gaps.
- **Placeholders:** none — full code and exact commands throughout.
- **Type consistency:** `diagnostics.run(client, settings, *, ha_ping=None)` identical in Task 2 (def), its tests, and the Task 3 `server.py` wrapper. `FakeNRClient.get_settings` returns the same dict shape (`version`/`runtimeState`/`context`) that `run` reads. Output keys (`node_red`/`flows`/`node_sets`/`config`/`home_assistant`) consistent between module, tests, and tool docstring. Tool count 29 consistent across Task 3 and Task 4. Test `Settings(ha_url="", ha_token="")` explicitly forces no-HA so it never triggers a real ping regardless of the local `.env`.
