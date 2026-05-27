# `/comms` WebSocket basic-auth handshake — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send the correct `Authorization` header on the Node-RED `/comms` WebSocket handshake in both auth modes, so `nr_capture_debug` stops 401-ing on the HA add-on (basic mode).

**Architecture:** Add `NRClient.ws_auth_header()` (Bearer / Basic / None) and rewrite `runtime._connect_ws` to use it instead of poking `client._client.headers`. Pure-logic auth method is unit-tested; `_connect_ws` remains the injectable network seam.

**Tech Stack:** Python 3.11+, pytest (`asyncio_mode=auto`), `websockets` (lazy import; tests inject a fake connector). No new tool — count stays 37.

---

## File Structure

- **Modify:** `src/nrha_mcp/client.py` — add `import base64`; add `NRClient.ws_auth_header()`.
- **Modify:** `src/nrha_mcp/runtime.py` — rewrite `_connect_ws` to use `ws_auth_header`.
- **Test:** `tests/test_client_ws.py` — 3 network-free tests for `ws_auth_header`.

No change to `tests/test_server_smoke.py` / `scripts/smoke_e2e.py`.

---

## Task 1: `ws_auth_header` + `_connect_ws` rewrite

**Files:**
- Test: `tests/test_client_ws.py`
- Modify: `src/nrha_mcp/client.py` (`import base64`; method after `get_ws_token` @86)
- Modify: `src/nrha_mcp/runtime.py` (`_connect_ws` @41)

- [ ] **Step 1: Write the three failing tests**

Add `import base64` to the top of `tests/test_client_ws.py`, then append:

```python
@pytest.mark.asyncio
async def test_ws_auth_header_basic():
    c = _client("http://host:1880", nr_user="admin", nr_pass="secret", nr_auth="basic")
    assert await c.ws_auth_header() == "Basic " + base64.b64encode(b"admin:secret").decode()
    await c.close()


@pytest.mark.asyncio
async def test_ws_auth_header_token():
    c = _client("http://host:1880", nr_token="tok123")
    assert await c.ws_auth_header() == "Bearer tok123"
    await c.close()


@pytest.mark.asyncio
async def test_ws_auth_header_open_returns_none():
    c = _client("http://host:1880")
    assert await c.ws_auth_header() is None
    await c.close()
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv/bin/pytest tests/test_client_ws.py -k ws_auth_header -v`
Expected: FAIL with `AttributeError: 'NRClient' object has no attribute 'ws_auth_header'`.

- [ ] **Step 3: Add `import base64` and the method in `client.py`**

At the top of `src/nrha_mcp/client.py`, add `import base64` (it currently imports only `httpx` and `from .config import Settings`).

Add this method to `NRClient`, right after `get_ws_token` (around line 89):

```python
    async def ws_auth_header(self) -> str | None:
        """Authorization header for the /comms WebSocket handshake.

        Bearer in token mode (after a lazy token exchange), HTTP Basic in basic mode, or
        None when the admin API is open. httpx applies basic-auth via an auth flow rather
        than a stored header, so the WS handshake must build the Basic header explicitly.
        """
        await self._ensure_auth()
        if self._token:
            return f"Bearer {self._token}"
        if self._user and self._auth_mode == "basic":
            raw = f"{self._user}:{self._pass or ''}".encode()
            return "Basic " + base64.b64encode(raw).decode()
        return None
```

- [ ] **Step 4: Run them, confirm they pass**

Run: `.venv/bin/pytest tests/test_client_ws.py -k ws_auth_header -v` → PASS.

- [ ] **Step 5: Rewrite `_connect_ws` in `runtime.py`**

Replace the body of `_connect_ws` (`src/nrha_mcp/runtime.py:41`) — currently it reads
`client._client.headers.get("Authorization")` — with:

```python
async def _connect_ws(client: NRClient):
    """Default connector: open the real /comms socket via the websockets lib."""
    import websockets  # lazy import so unit tests need no network lib

    url = client.comms_ws_url()
    auth = await client.ws_auth_header()
    headers = {"Authorization": auth} if auth else {}
    ws = await websockets.connect(url, additional_headers=headers or None)
    return _WebsocketsConn(ws)
```

Do NOT add any logging of `auth`/`headers` (they carry credentials).

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `.venv/bin/pytest -q`
Expected: all PASS. `tests/test_runtime.py` injects a fake `connect`, so it never calls
`_connect_ws` — the rewrite must not change `capture_debug`'s behavior or the
`_WebsocketsConn` adapter. Confirm `test_client_ws.py` (5 tests) and the runtime suite
are green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "fix: send basic-auth header on /comms WS handshake

NRClient.ws_auth_header() builds Bearer/Basic/None for the handshake;
_connect_ws uses it instead of reading a stored header (absent in basic
mode -> nginx 401 on the HA add-on). Also fixes a latent token-first-call
401 by ensuring the token before building the header.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- `_ensure_auth()` is a no-op outside token mode (it early-returns), so the **basic** and
  **open** test paths make **no network call** — the three tests run offline.
- Keep the naming/return contract exact: `ws_auth_header` returns the **full header
  value** (with the `Basic `/`Bearer ` scheme), not just a token.
- Only `client.py`, `runtime.py`, `tests/test_client_ws.py` change. No new tool, no
  `server.py`/smoke edits, no touching the comms-protocol `{"auth": …}` message path in
  `capture_debug`.
