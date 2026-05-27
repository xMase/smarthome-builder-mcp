# `/comms` WebSocket basic-auth handshake — Design

- **Date:** 2026-05-27
- **Status:** Approved (pending spec review)
- **Branch:** `feat/comms-basic-auth`

## Problem

`nr_capture_debug` (and `nr_get_runtime_events`) connect to Node-RED's `/comms`
WebSocket via `runtime._connect_ws`, which today forwards auth like this:

```python
auth = client._client.headers.get("Authorization")
if auth:
    headers["Authorization"] = auth  # covers a reverse-proxy auth case
```

This only works in **token mode**, where the bearer token is a stored header. In
**basic mode** — the Home Assistant Node-RED add-on (`NR_AUTH=basic`, nginx in front) —
httpx applies credentials through an `httpx.BasicAuth` flow, **not** as a stored header
(`client.py:39-40`). So `client._client.headers.get("Authorization")` is `None`, no
credentials reach the WS handshake, and nginx rejects the upgrade with **HTTP 401**.

Verified live (2026-05-27): every REST call (basic-authed) succeeds, but
`nr_capture_debug` returns `InvalidStatus: server rejected WebSocket connection: HTTP
401`. The tool is deployed and correct except for this auth gap.

A second, latent bug lives in the same line: `_connect_ws` never calls `_ensure_auth`,
so in **token-with-user mode** (lazy token), if `capture_debug` is the first call, the
stored header is still empty → 401 in token mode too.

## Goals

- Send the correct `Authorization` header on the `/comms` handshake for **both** auth
  modes: `Bearer …` in token mode, `Basic …` in basic mode, nothing when the admin API
  is open.
- Centralize this in `NRClient` (it owns auth) instead of `runtime.py` reaching into
  `client._client.headers` internals.
- Fix the latent token-mode-first-call case by ensuring the token before building the
  header.

## Non-goals

- **Comms-protocol message auth** (`{"auth": <token>}` sent *after* connect) is
  unchanged. The 401 is at the HTTP handshake; on the HA add-on, nginx authenticates in
  front and Node-RED itself runs open (no `adminAuth`), so no comms token is required.
  `capture_debug` already sends no auth message when `get_ws_token()` is `None`
  (`test_capture_no_auth_when_open`), which is correct for basic mode.
- **TLS verification for `wss://`** — `_connect_ws` passes no SSL context, so it does
  not honor `NR_VERIFY_SSL` for a self-signed `wss` endpoint. Out of scope (the target
  NR is `http`); tracked as a follow-up.
- **No new MCP tool.** Tool count stays 37; no smoke/e2e changes.

## Design

### A. `NRClient.ws_auth_header()` — `src/nrha_mcp/client.py`

Add `import base64` at the top (currently only `httpx` + `config` are imported). Add a
method next to `get_ws_token` (`client.py:86`):

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

`_ensure_auth()` is a no-op in basic/open mode (it early-returns unless token mode), so
the basic and open paths make no network call. In token-with-user mode it performs the
lazy token exchange (this is the latent-bug fix).

### B. Rewrite `_connect_ws` — `src/nrha_mcp/runtime.py:41`

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

Drops the `client._client.headers` access entirely. `additional_headers` is already the
keyword this file uses, so no `websockets`-version change. **Never log the header** (it
carries credentials).

## Error handling

`ws_auth_header` cannot fail except via `_ensure_auth` in token mode, which already
raises `NRAuthError` on a bad login — unchanged. A still-failing handshake (e.g. wrong
credentials) continues to surface the `websockets` `InvalidStatus`/401 through the
normal `_wrap` path.

## Testing

`tests/test_client_ws.py` (extend; it already has `_client(url, **kw)` and asyncio
tests). Add `import base64` and three network-free tests:

- **basic:** `_client(url, nr_user="admin", nr_pass="secret", nr_auth="basic")` →
  `ws_auth_header()` == `"Basic " + base64.b64encode(b"admin:secret").decode()`.
- **token:** `_client(url, nr_token="tok123")` → `ws_auth_header()` == `"Bearer tok123"`.
- **open:** `_client(url)` → `ws_auth_header()` is `None`.

`_connect_ws` itself stays untested (it is the injectable network seam; all
`test_runtime.py` cases pass a fake `connect`). The previously-buggy logic now lives in
`ws_auth_header`, which the tests above cover — so the fix is verified at the unit it
belongs to.

No change to `tests/test_server_smoke.py` / `scripts/smoke_e2e.py` (no new tool).

## Deployment

Rebuild/redeploy the Docker image so the fix is live; local `.venv` for tests. After
deploy, `nr_capture_debug` should connect on the HA add-on (basic mode) instead of
401-ing — unblocking live debug/status capture.

## Follow-ups

- **`wss://` + `NR_VERIFY_SSL`:** `_connect_ws` should pass an SSL context honoring
  `nr_verify_ssl` for secure endpoints (latent gap in the same function).
- **Comms-protocol token in basic mode** — only if a Node-RED behind the add-on is ever
  configured with `adminAuth` (then `get_ws_token()` would need to return a basic-mode
  token for the post-connect `{"auth": …}` message). Not needed for the standard add-on.
