# Runtime Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `nr_capture_debug` tool that observes live Node-RED debug/status over the `/comms` WebSocket, fix the context tools to read the real store names, and remove the misleading `nr_get_debug_output`.

**Architecture:** A new `runtime.py` module owns the WebSocket capture, with the transport behind an injectable `connect` callable so it is unit-testable without a live server. `flows.py` stops hardcoding the `"memory"` store and reads all stores. `server.py` registers the new tool and drops the old one.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), httpx (existing), `websockets` (new), pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-runtime-observation-design.md`

---

## File structure

- Create `src/nrha_mcp/runtime.py` — `/comms` capture logic (`capture_debug`, frame normalization, default WS connector).
- Modify `src/nrha_mcp/client.py` — add `comms_ws_url()` and `get_ws_token()`.
- Modify `src/nrha_mcp/flows.py` — rewrite `get_flow_context`/`get_global_context` for multi-store; delete `get_debug_output`.
- Modify `src/nrha_mcp/server.py` — register `nr_capture_debug`; remove `nr_get_debug_output`.
- Modify `pyproject.toml` — add `websockets>=12`.
- Modify `tests/conftest.py` — multi-store context shape; add `get_ws_token`/`ws_token` to the fake client.
- Create `tests/test_client_ws.py`, `tests/test_runtime.py`.
- Modify `tests/test_flows.py`, `tests/test_server_smoke.py`.

All commands run from the project root `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests use the project venv: `./.venv/bin/pytest`.

---

## Task 1: Add the `websockets` dependency

**Files:**
- Modify: `pyproject.toml:22-27`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, change the `dependencies` list to include `websockets`:

```toml
dependencies = [
    "mcp>=1.0.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "websockets>=12",
]
```

- [ ] **Step 2: Install it into the dev venv**

Run: `./.venv/bin/pip install "websockets>=12"`
Expected: ends with `Successfully installed websockets-<ver>`

- [ ] **Step 3: Verify the import**

Run: `./.venv/bin/python -c "import websockets; print(websockets.__version__)"`
Expected: prints a version `>= 12`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add websockets dependency for /comms capture"
```

---

## Task 2: Client WebSocket helpers

**Files:**
- Modify: `src/nrha_mcp/client.py` (add two methods to `NRClient`)
- Test: `tests/test_client_ws.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_client_ws.py`:

```python
import pytest

from nrha_mcp.client import NRClient
from nrha_mcp.config import Settings


def _client(url, **kw):
    return NRClient(Settings(nr_url=url, **kw))


def test_comms_ws_url_http():
    assert _client("http://host:1880").comms_ws_url() == "ws://host:1880/comms"


def test_comms_ws_url_https_and_trailing_slash():
    assert _client("https://host:1880/").comms_ws_url() == "wss://host:1880/comms"


@pytest.mark.asyncio
async def test_get_ws_token_open_admin_returns_none():
    # basic/open mode, no token, no user -> no token exchange, no network call
    c = _client("http://host:1880")
    assert await c.get_ws_token() is None
    await c.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_client_ws.py -v`
Expected: FAIL with `AttributeError: 'NRClient' object has no attribute 'comms_ws_url'`

- [ ] **Step 3: Implement the helpers**

In `src/nrha_mcp/client.py`, add these two methods to the `NRClient` class (place them right after `_ensure_auth`, before `_do`):

```python
    def comms_ws_url(self) -> str:
        """Derive the /comms WebSocket URL from the admin base URL."""
        base = self.url.rstrip("/")
        if base.startswith("https"):
            base = "wss" + base[len("https"):]
        elif base.startswith("http"):
            base = "ws" + base[len("http"):]
        return base + "/comms"

    async def get_ws_token(self) -> str | None:
        """Bearer token for the /comms socket, or None if the admin API is open."""
        await self._ensure_auth()
        return self._token
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_client_ws.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/client.py tests/test_client_ws.py
git commit -m "feat(client): comms_ws_url + get_ws_token helpers"
```

---

## Task 3: `runtime.capture_debug` core

**Files:**
- Create: `src/nrha_mcp/runtime.py`
- Modify: `tests/conftest.py` (extend `FakeNRClient`)
- Test: `tests/test_runtime.py` (create)

- [ ] **Step 1: Extend the fake client**

In `tests/conftest.py`, inside `FakeNRClient.__init__`, add a `ws_token` attribute (place it after `self.injected = []`):

```python
        self.ws_token = None      # token returned by get_ws_token()
```

And add this method to `FakeNRClient` (place it after `inject`):

```python
    async def get_ws_token(self):
        return self.ws_token
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_runtime.py`:

```python
import asyncio
import collections

import pytest

from nrha_mcp import runtime


class FakeComms:
    """In-memory /comms connection. Yields queued frames, then blocks."""

    def __init__(self, frames):
        self._frames = collections.deque(frames)
        self.sent = []

    async def send(self, obj):
        self.sent.append(obj)

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(3600)  # block until the capture window cancels us

    async def close(self):
        pass


def _connect_factory(comms):
    async def _connect(client):
        return comms
    return _connect


def _debug_frame(node_id="d1", z="tab1", name="dbg", msg="hi"):
    return {"topic": "debug",
            "data": {"id": node_id, "z": z, "name": name, "msg": msg, "format": "string"}}


@pytest.mark.asyncio
async def test_capture_collects_debug(fake_client):
    comms = FakeComms([_debug_frame(msg="rain")])
    res = await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert res["count"] == 1
    assert res["captured"][0]["msg"] == "rain"
    assert {"subscribe": "debug"} in comms.sent
    assert {"subscribe": "status/#"} in comms.sent


@pytest.mark.asyncio
async def test_capture_filters_by_node_id(fake_client):
    comms = FakeComms([_debug_frame(node_id="d1", msg="a"),
                       _debug_frame(node_id="d2", msg="b")])
    res = await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=2, node_id="d2",
        connect=_connect_factory(comms))
    assert res["count"] == 1
    assert res["captured"][0]["node_id"] == "d2"


@pytest.mark.asyncio
async def test_capture_inject_after_subscribe(fake_client):
    comms = FakeComms([])
    res = await runtime.capture_debug(
        fake_client, window_ms=50, inject_node_id="inj1",
        connect=_connect_factory(comms))
    assert res["injected"] == "inj1"
    assert fake_client.injected == ["inj1"]
    assert {"subscribe": "debug"} in comms.sent  # subscribed before inject, by construction


@pytest.mark.asyncio
async def test_capture_empty_has_hint(fake_client):
    comms = FakeComms([])
    res = await runtime.capture_debug(
        fake_client, window_ms=50, connect=_connect_factory(comms))
    assert res["count"] == 0
    assert "hint" in res


@pytest.mark.asyncio
async def test_capture_sends_auth_when_token(fake_client):
    fake_client.ws_token = "tok123"
    comms = FakeComms([_debug_frame()])
    await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert {"auth": "tok123"} in comms.sent


@pytest.mark.asyncio
async def test_capture_no_auth_when_open(fake_client):
    comms = FakeComms([_debug_frame()])
    await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert not any("auth" in m for m in comms.sent)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_runtime.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nrha_mcp.runtime'`

- [ ] **Step 4: Implement `runtime.py`**

Create `src/nrha_mcp/runtime.py`:

```python
"""Capture live Node-RED runtime messages over the /comms WebSocket.

Debug-node output and node status are pushed over /comms, not stored in flow
context, so this is the only way to observe them. The transport sits behind an
injectable `connect` callable so tests can feed frames without a live server.
"""
import asyncio
import json
import time
from typing import Awaitable, Callable

from .client import NRClient

_EMPTY_HINT = (
    "No messages captured. Check: the debug node is active and 'send to sidebar' "
    "is enabled; window_ms was long enough; the inject actually produced output "
    "(a function may have filtered it); node_id/flow_id filters are correct."
)


class _WebsocketsConn:
    """Adapter over a `websockets` client connection."""

    def __init__(self, ws):
        self._ws = ws

    async def send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    async def recv(self) -> dict:
        raw = await self._ws.recv()
        return json.loads(raw)

    async def close(self) -> None:
        await self._ws.close()


async def _connect_ws(client: NRClient):
    """Default connector: open the real /comms socket via the websockets lib."""
    import websockets  # lazy import so unit tests need no network lib

    url = client.comms_ws_url()
    headers = {}
    auth = client._client.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth  # covers a reverse-proxy auth case
    ws = await websockets.connect(url, additional_headers=headers or None)
    return _WebsocketsConn(ws)


def _normalize(frame: dict) -> dict | None:
    """Map a /comms frame to our message dict, or None if it is not debug/status."""
    topic = frame.get("topic", "")
    data = frame.get("data", {}) or {}
    if topic == "debug":
        return {
            "topic": "debug",
            "node_id": data.get("id"),
            "node_name": data.get("name"),
            "flow_id": data.get("z"),
            "msg": data.get("msg"),
            "format": data.get("format"),
            "ts": time.time(),
        }
    if topic.startswith("status/"):
        status = data.get("status", data) or {}
        return {
            "topic": "status",
            "node_id": topic.split("/", 1)[1],
            "node_name": None,
            "flow_id": data.get("z"),
            "text": status.get("text"),
            "ts": time.time(),
        }
    return None


async def capture_debug(
    client: NRClient,
    *,
    window_ms: int = 2000,
    inject_node_id: str | None = None,
    topics: list[str] | None = None,
    node_id: str | None = None,
    flow_id: str | None = None,
    max_messages: int = 100,
    connect: Callable[[NRClient], Awaitable] | None = None,
) -> dict:
    """Subscribe to /comms, optionally inject, and collect messages for a window."""
    topics = topics or ["debug", "status"]
    conn = await (connect or _connect_ws)(client)
    captured: list[dict] = []
    injected: str | None = None
    try:
        token = await client.get_ws_token()
        if token:
            await conn.send({"auth": token})
        for t in topics:
            await conn.send({"subscribe": "status/#" if t == "status" else t})
        await asyncio.sleep(0.1)  # let the subscription register before triggering
        if inject_node_id:
            await client.inject(inject_node_id)
            injected = inject_node_id
        deadline = time.monotonic() + window_ms / 1000
        while len(captured) < max_messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(conn.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            msg = _normalize(frame)
            if msg is None:
                continue
            if node_id and msg["node_id"] != node_id:
                continue
            if flow_id and msg["flow_id"] != flow_id:
                continue
            captured.append(msg)
    finally:
        await conn.close()
    result = {
        "captured": captured,
        "count": len(captured),
        "window_ms": window_ms,
        "injected": injected,
    }
    if not captured:
        result["hint"] = _EMPTY_HINT
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_runtime.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/runtime.py tests/test_runtime.py tests/conftest.py
git commit -m "feat(runtime): capture_debug over /comms with injectable transport"
```

---

## Task 4: Register `nr_capture_debug`, remove `nr_get_debug_output`

**Files:**
- Modify: `src/nrha_mcp/server.py:10` (import), `:101-104` (remove old tool, add new)
- Modify: `src/nrha_mcp/flows.py:154-155` (delete `get_debug_output`)
- Modify: `tests/test_server_smoke.py:8-18`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, edit the `expected` set: remove `"nr_get_debug_output",` and add `"nr_capture_debug",`. The `len(names) == 23` assertion stays (one removed, one added). Result:

```python
    expected = {
        "nr_get_flow_summary", "nr_get_flow", "nr_search_nodes", "nr_get_function_code",
        "nr_get_node_config", "nr_get_flow_context", "nr_get_global_context",
        "nr_capture_debug", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
        "nr_create_nodes", "nr_delete_nodes", "nr_get_installed_modules", "nr_install_module",
        "nr_inject", "ha_list_ha_nodes", "ha_get_server_config",
        "nr_backup_flow", "nr_list_backups", "nr_restore_backup",
        "ha_search_entities", "ha_get_state",
    }
    assert expected.issubset(names)
    assert len(names) == 23
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `nr_capture_debug` not in registered names; `nr_get_debug_output` still present.

- [ ] **Step 3: Update the import in `server.py`**

In `src/nrha_mcp/server.py`, change line 10 from:

```python
from . import flows, edit, modules, ha
```

to:

```python
from . import flows, edit, modules, ha, runtime
```

- [ ] **Step 4: Replace the tool in `server.py`**

Delete this block (`server.py:101-104`):

```python
@mcp.tool()
async def nr_get_debug_output(flow_id: str, key: str | None = None) -> str:
    """Read debug data stored to flow context."""
    return await _wrap(lambda: flows.get_debug_output(client, flow_id, key))
```

and replace it with:

```python
@mcp.tool()
async def nr_capture_debug(
    window_ms: int = 2000,
    inject_node_id: str | None = None,
    topics: list[str] | None = None,
    node_id: str | None = None,
    flow_id: str | None = None,
    max_messages: int = 100,
) -> str:
    """Capture live debug/status messages from the Node-RED /comms WebSocket.

    Subscribes first, then (optionally) triggers `inject_node_id`, then collects
    messages for `window_ms`. Use this to verify a flow's output deterministically:
    debug-node output is NOT in flow context, so the context tools cannot see it.
    Without `inject_node_id` it passively listens for the window."""
    return await _wrap(lambda: runtime.capture_debug(
        client,
        window_ms=window_ms,
        inject_node_id=inject_node_id,
        topics=topics,
        node_id=node_id,
        flow_id=flow_id,
        max_messages=max_messages,
    ))
```

- [ ] **Step 5: Delete `get_debug_output` from `flows.py`**

Remove these lines (`flows.py:154-155`):

```python
async def get_debug_output(client: NRClient, flow_id: str, key: str | None = None) -> dict:
    return await get_flow_context(client, flow_id, key)
```

- [ ] **Step 6: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed

- [ ] **Step 7: Verify no other references to the removed name**

Run: `grep -rn "debug_output" src tests`
Expected: no output (empty).

- [ ] **Step 8: Commit**

```bash
git add src/nrha_mcp/server.py src/nrha_mcp/flows.py tests/test_server_smoke.py
git commit -m "feat(server): add nr_capture_debug, remove misleading nr_get_debug_output"
```

---

## Task 5: Fix the context tools for multi-store

**Files:**
- Modify: `src/nrha_mcp/flows.py:134-151` (`get_flow_context`, `get_global_context`)
- Modify: `tests/conftest.py` (multi-store fake shape)
- Modify: `tests/test_flows.py` (replace `test_global_context_lists_keys`, add cases)

- [ ] **Step 1: Update the fake client's context shape**

In `tests/conftest.py`:

Change `self.global_context = {"memory": {}}` to:

```python
        self.global_context = {"default": {}, "store": {}}
```

Change the `get_context` method body from `return self.context.get(flow_id, {"memory": {}})` to:

```python
    async def get_context(self, flow_id):
        return self.context.get(flow_id, {"default": {}, "store": {}})
```

- [ ] **Step 2: Replace/add the failing tests**

In `tests/test_flows.py`, delete the existing `test_global_context_lists_keys` (lines 43-47) and add these tests at the end of the file:

```python
@pytest.mark.asyncio
async def test_global_context_lists_keys_grouped_by_store(fake_client):
    fake_client.global_context = {"default": {"scheme": {"msg": 1}}, "store": {}}
    res = await flows.get_global_context(fake_client, None)
    assert res["scope"] == "global"
    assert res["stores"] == {"default": ["scheme"], "store": []}


@pytest.mark.asyncio
async def test_global_context_lookup_prefers_default(fake_client):
    fake_client.global_context = {"default": {"k": {"msg": "v"}}, "store": {}}
    res = await flows.get_global_context(fake_client, "k")
    assert res["store"] == "default"
    assert res["value"] == "v"


@pytest.mark.asyncio
async def test_flow_context_multi_store(fake_client):
    fake_client.context["tab1"] = {"default": {"a": {"msg": 1}}, "store": {"b": {"msg": 2}}}
    res = await flows.get_flow_context(fake_client, "tab1", None)
    assert res["flow_id"] == "tab1"
    assert res["stores"]["default"] == ["a"]
    assert res["stores"]["store"] == ["b"]


@pytest.mark.asyncio
async def test_context_key_not_found_raises(fake_client):
    fake_client.global_context = {"default": {}, "store": {}}
    with pytest.raises(NRNotFoundError):
        await flows.get_global_context(fake_client, "missing")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_flows.py -v`
Expected: the four new tests FAIL (current code returns `{"keys": ...}` from the `"memory"` store, so `res["stores"]` raises `KeyError` / shape mismatch).

- [ ] **Step 4: Rewrite the context readers**

In `src/nrha_mcp/flows.py`, replace the current `get_flow_context` and `get_global_context` (lines 134-151) with:

```python
def _read_context(data: dict, *, scope: dict, key: str | None, not_found: str) -> dict:
    stores = {name: (c or {}) for name, c in data.items() if isinstance(c, dict)}
    if key is None:
        return {**scope, "stores": {name: list(c.keys()) for name, c in stores.items()}}
    order = (["default"] if "default" in stores else []) + [n for n in stores if n != "default"]
    for name in order:
        if key in stores[name]:
            return {**scope, "key": key, "store": name, "value": stores[name][key].get("msg")}
    raise NRNotFoundError(not_found)


async def get_flow_context(client: NRClient, flow_id: str, key: str | None = None) -> dict:
    data = await client.get_context(flow_id)
    return _read_context(data, scope={"flow_id": flow_id}, key=key,
                         not_found=f"Context key not found: '{key}'")


async def get_global_context(client: NRClient, key: str | None = None) -> dict:
    data = await client.get_global_context()
    return _read_context(data, scope={"scope": "global"}, key=key,
                         not_found=f"Global context key not found: '{key}'")
```

(`NRNotFoundError` is already imported in `flows.py`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_flows.py -v`
Expected: all pass (including the four new ones).

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/flows.py tests/conftest.py tests/test_flows.py
git commit -m "fix(flows): read all context stores (default/store), not hardcoded 'memory'"
```

---

## Task 6: Full test run + deploy

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass, no errors.

- [ ] **Step 2: Rebuild and redeploy the container**

The live MCP on `:8090` runs from the Docker image; code changes only take effect after a rebuild. From the project root:

Run: `docker compose build && docker compose up -d`
Expected: image rebuilds, container recreated and healthy.

(If the deployment is managed via Portainer using `deploy/portainer-stack.yml`, redeploy/recreate the stack there instead, pulling the rebuilt image.)

- [ ] **Step 3: Smoke-check the new tool against the live runtime**

After redeploy, in a Claude Code session with the `nrha` MCP, add a temporary inject wired to a debug node (or to the `rilevaPioggia` function in the `MCP Test` tab), then call `nr_capture_debug(inject_node_id="<that inject id>", window_ms=2000)`. Expected: `captured` contains the debug message; if empty, the `hint` field explains why.

- [ ] **Step 4: Final branch state**

Run: `git log --oneline main..HEAD`
Expected: the commits from Tasks 1–5. Hand off / open a PR per the user's preference.

---

## Follow-up (non-code, owner: this session)

- Correct the auto-memory note `nrha-context-read-tools-blind`: context reads are NOT blind to the runtime — they read the wrong store name (`memory` vs the real `default`). The separate, still-true caveat is that debug-node sidebar output is not in context (hence `nr_capture_debug`).

---

## Self-review

- **Spec coverage:** Part 1 `nr_capture_debug` → Tasks 3+4. Part 2 context fix → Task 5. Removal of `nr_get_debug_output` → Task 4. New module/dep/tests → Tasks 1–3,5. WS auth (open/token/header) → Task 3 (`capture_debug` token branch + `_connect_ws` header). Deploy rebuild → Task 6. Memory follow-up → Follow-up section. No gaps.
- **Placeholders:** none — every code/test step shows full code and exact commands.
- **Type consistency:** `capture_debug(...)` signature identical in `runtime.py`, its tests, and the `server.py` wrapper. `connect` factory shape matches `FakeComms`/`_connect_ws`. Context return keys (`stores`, `store`, `value`, `flow_id`, `scope`) consistent between `_read_context`, the readers, and `test_flows.py`. Tool count stays 23 in `test_server_smoke.py`.
