# HTTP Bearer-Token Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require a static bearer token (`MCP_AUTH_TOKEN`) in `http` mode; refuse to start without it; `stdio` unchanged.

**Architecture:** `Settings` gains `mcp_auth_token: SecretStr | None` (blank → `None`, value stripped). `server.py` passes the SDK's own `token_verifier` + `AuthSettings` to `MCPServer` when a token is set — the SDK's bearer middleware returns 401. `main()` raises `SystemExit` in `http` mode when no token is set. `run(...)` is unchanged.

**Tech Stack:** Python 3.11+, mcp 2.3 (`mcp.server.auth.provider.AccessToken`, `mcp.server.auth.settings.AuthSettings`), pydantic / pydantic-settings, pytest, starlette `TestClient`, podman.

**Spec:** `docs/superpowers/specs/2026-10-15-http-bearer-auth-design.md`

## Global Constraints

- Env var name: `MCP_AUTH_TOKEN`. Type: `SecretStr | None`. Empty / whitespace-only → `None`.
- Token comparison: `hmac.compare_digest` on bytes.
- Missing token in `http` mode → `SystemExit` from `main()`; never fail at import.
- `run(...)` kwargs unchanged: `transport="streamable-http", host, port, json_response=True, stateless_http=True`.
- No new variable in the user's `.env`. Docs/deploy files only.
- Commits stage files by explicit path and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No push, tag, or change on the user's HA without the user's go-ahead.

## Review Focus

- A token with a trailing newline / spaces (copy-paste into Portainer) → must still match what the client sends. Pinned by `test_token_is_stripped` (Task 1).
- A token in the developer's `.env` → must not change the result of tests that expect "no token". Pinned by `_fresh_server` defaulting `MCP_AUTH_TOKEN=""` (Task 1); `test_localhost_bind_rejects_foreign_host_header` and the other app tests then run unauthenticated as before.
- Non-ASCII token → `hmac.compare_digest` on `str` raises `TypeError`; comparing bytes avoids a 500. Pinned by `test_bearer_token_required_on_http_app` sending a wrong non-ASCII token (Task 1).
- Lower-case `bearer` scheme from a client → SDK accepts it (probe). Pinned in `test_bearer_token_required_on_http_app` (Task 1).
- Container without the token → must exit, not serve open. Checked in Task 2.

---

### Task 1: Token setting, SDK verifier, start-up refusal, docs

**Files:**
- Modify: `src/nrha_mcp/config.py` (imports lines 1-5; transport block lines 41-44)
- Modify: `src/nrha_mcp/server.py` (imports lines 3-6; constructor block before `def _ok`; `main()`)
- Modify: `tests/test_server_transport.py`
- Modify: `tests/test_config.py` (append)
- Modify: `.env.example` (after the `MCP_HTTP_PORT=8080` line)
- Modify: `deploy/portainer-stack.yml` (`environment:` block)
- Modify: `docker-compose.yml` (header comment)
- Modify: `README.md` (config table; `claude mcp add --transport http` example)
- Modify: `CLAUDE.md` (Conventions & gotchas)

**Interfaces:**
- Produces: `Settings.mcp_auth_token: SecretStr | None`; `nrha_mcp.server._StaticTokenVerifier(token: str)` with `async verify_token(token: str) -> AccessToken | None`; `main()` raises `SystemExit` (message starts with `MCP_AUTH_TOKEN is required`) in `http` mode without a token.

- [ ] **Step 1: Write the failing tests**

In `tests/test_server_transport.py`:

(a) Replace `_fresh_server` so every test starts with "no token" unless it sets one (a token in a local `.env` must not leak in):

```python
def _fresh_server(monkeypatch, **env):
    env.setdefault("MCP_AUTH_TOKEN", "")  # "" overrides a token in a local .env
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import nrha_mcp.server as srv
    return importlib.reload(srv)
```

(b) Add a header parameter to `_post_initialize`:

```python
def _post_initialize(srv, bind_host, host_header, headers=None):
    # Same HTTP options main() passes to run(); host only drives the SDK's DNS-rebinding check.
    app = srv.mcp.streamable_http_app(host=bind_host, json_response=True, stateless_http=True)
    with TestClient(app) as c:
        return c.post("/mcp", json=_INIT, headers={**_HEADERS, "Host": host_header, **(headers or {})})
```

(c) In `test_http_mode_passes_bind_and_http_options_to_run` and `test_http_mode_localhost_bind_is_passed_through`, add `MCP_AUTH_TOKEN="t0ken"` to the `_fresh_server(...)` call. Their assertions stay identical.

(d) Append:

```python
def test_http_mode_without_token_refuses_to_start(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http")
    captured = _capture_run(monkeypatch, srv)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN is required"):
        srv.main()
    assert captured == {}


def test_http_mode_blank_token_refuses_to_start(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http", MCP_AUTH_TOKEN="   ")
    captured = _capture_run(monkeypatch, srv)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN is required"):
        srv.main()
    assert captured == {}


def test_bearer_token_required_on_http_app(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http", MCP_AUTH_TOKEN="s3cret-token")
    host = "192.168.200.6:8090"
    assert _post_initialize(srv, "0.0.0.0", host).status_code == 401
    assert _post_initialize(srv, "0.0.0.0", host,
                            {"Authorization": "Bearer wrong"}).status_code == 401
    # Non-ASCII must be rejected, not crash compare_digest.
    assert _post_initialize(srv, "0.0.0.0", host,
                            {"Authorization": "Bearer sécret".encode()}).status_code == 401
    assert _post_initialize(srv, "0.0.0.0", host,
                            {"Authorization": "Bearer s3cret-token"}).status_code == 200
    assert _post_initialize(srv, "0.0.0.0", host,
                            {"Authorization": "bearer s3cret-token"}).status_code == 200
```

Add `import pytest` to the imports at the top of the file.

Append to `tests/test_config.py`:

```python
from nrha_mcp.config import Settings as _AuthSettings


def test_auth_token_hidden_from_repr():
    s = _AuthSettings(_env_file=None, mcp_auth_token="s3cret-token")
    assert "s3cret-token" not in repr(s)
    assert s.mcp_auth_token.get_secret_value() == "s3cret-token"


def test_blank_auth_token_is_none():
    assert _AuthSettings(_env_file=None, mcp_auth_token="").mcp_auth_token is None
    assert _AuthSettings(_env_file=None, mcp_auth_token="  \n").mcp_auth_token is None


def test_token_is_stripped():
    s = _AuthSettings(_env_file=None, mcp_auth_token="  s3cret-token\n")
    assert s.mcp_auth_token.get_secret_value() == "s3cret-token"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_server_transport.py tests/test_config.py -q`
Expected: FAIL — the two refusal tests (no `SystemExit`), `test_bearer_token_required_on_http_app` (200 without a header), and the three config tests (`mcp_auth_token` does not exist / is not a `SecretStr`).

- [ ] **Step 3: Add the setting to `src/nrha_mcp/config.py`**

Imports become:

```python
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
```

After `mcp_http_port: int = 8080` add:

```python
    mcp_auth_token: SecretStr | None = None  # required in http mode; checked in server.main()

    @field_validator("mcp_auth_token", mode="before")
    @classmethod
    def _blank_token_is_none(cls, v):
        # Strip copy-paste whitespace; MCP_AUTH_TOKEN="" overrides a .env token and means "none".
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v
```

- [ ] **Step 4: Wire the verifier and the start-up check in `src/nrha_mcp/server.py`**

Imports (after `import json`):

```python
import hmac
import json
from importlib.metadata import PackageNotFoundError, version

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
```

Replace the `mcp = MCPServer(...)` block with:

```python
class _StaticTokenVerifier:
    """Accepts exactly one bearer token: MCP_AUTH_TOKEN."""

    def __init__(self, token: str):
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        # bytes: compare_digest on a non-ASCII str raises TypeError
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token=token, client_id="static-token", scopes=[])
        return None


_auth = {}
if settings.mcp_auth_token is not None:
    _auth = {
        "token_verifier": _StaticTokenVerifier(settings.mcp_auth_token.get_secret_value()),
        # issuer_url is required by AuthSettings but published nowhere without an auth_server_provider.
        "auth": AuthSettings(issuer_url="http://localhost", resource_server_url=None),
    }

mcp = MCPServer(
    "smarthome-builder-mcp",
    instructions=HA_AUTHORING_GUIDANCE,
    version=_version,
    **_auth,
)
```

Replace `main()`:

```python
def main():
    """Console-script entry point."""
    if settings.mcp_transport == "http":
        if settings.mcp_auth_token is None:
            raise SystemExit(
                "MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        mcp.run(transport="streamable-http", host=settings.mcp_http_host,
                port=settings.mcp_http_port, json_response=True, stateless_http=True)
    else:
        mcp.run(transport="stdio")
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass — 303 tests (297 + 6 new).

- [ ] **Step 6: Docs and deploy files**

`.env.example`, after `MCP_HTTP_PORT=8080        # port in http mode`:

```
# Required in http mode: the server refuses to start without it. Clients send
# "Authorization: Bearer <token>". Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
# MCP_AUTH_TOKEN=
```

`deploy/portainer-stack.yml`, after `MCP_HTTP_PORT: "8080"`:

```yaml
      MCP_AUTH_TOKEN: "${MCP_AUTH_TOKEN}"  # required: set it in the stack's Environment variables
```

`docker-compose.yml`, append to the header comment block (after the `# For Portainer use ...` line):

```
# MCP_AUTH_TOKEN must be set in .env: the HTTP service refuses to start without it.
```

`README.md`:
- In the config table, after the `MCP_HTTP_HOST` / `MCP_HTTP_PORT` row, add:
  ``| `MCP_AUTH_TOKEN` | — | Bearer token, **required in `http` mode** (the server refuses to start without it). Clients send `Authorization: Bearer <token>`. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |``
- Replace `claude mcp add --transport http nrha http://<DOCKER_HOST_IP>:8080/mcp` with:
  ```bash
  claude mcp add --transport http nrha http://<DOCKER_HOST_IP>:8080/mcp \
    --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
  ```
- In the Portainer steps, step 2, append: `Set `MCP_AUTH_TOKEN` there: without it the container exits at start.`

`CLAUDE.md`, under "Conventions & gotchas", add a bullet:

```
- **`http` mode needs `MCP_AUTH_TOKEN`:** `main()` refuses to start without it; the SDK's bearer
  middleware (our `_StaticTokenVerifier`) returns 401 on a missing/wrong token. Tests that expect
  "no token" rely on `_fresh_server` setting `MCP_AUTH_TOKEN=""`, which overrides a local `.env`.
```

- [ ] **Step 7: Run the suite again and commit**

Run: `.venv/bin/pytest -q` → Expected: 303 passed.

```bash
git add src/nrha_mcp/config.py src/nrha_mcp/server.py tests/test_server_transport.py tests/test_config.py \
        .env.example deploy/portainer-stack.yml docker-compose.yml README.md CLAUDE.md
git commit -m "feat(http): require a bearer token (MCP_AUTH_TOKEN) in http mode

The SDK's bearer middleware returns 401 without the right token; the
server refuses to start in http mode when no token is set. stdio is
unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Container verification

No code change expected; on failure stop and report. Any fix is its own commit.

**Interfaces:** Consumes Task 1's behaviour. Produces a pass/fail report.

- [ ] **Step 1: Build**

Run: `podman build -q -t smarthome-builder-mcp:auth .` → Expected: exit 0.

- [ ] **Step 2: No token → refuses to start**

```bash
podman run --rm smarthome-builder-mcp:auth; echo "exit=$?"
```

Expected: stderr contains `MCP_AUTH_TOKEN is required`; `exit=1`.

- [ ] **Step 3: With token → 401 without header, 200 with header**

```bash
T=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
podman run -d --rm --name sbm-auth -p 18765:8080 -e MCP_AUTH_TOKEN="$T" smarthome-builder-mcp:auth >/dev/null; sleep 4
H='-H Content-Type:application/json -H Accept:application/json,text/event-stream'
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
curl -s -o /dev/null -w '%{http_code}\n' $H -X POST http://127.0.0.1:18765/mcp -d "$INIT"
curl -s -o /dev/null -w '%{http_code}\n' $H -H "Authorization: Bearer $T" -X POST http://127.0.0.1:18765/mcp -d "$INIT"
curl -s $H -H "Authorization: Bearer $T" -X POST http://127.0.0.1:18765/mcp \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["tools"]))'
podman logs sbm-auth 2>&1 | grep -c "$T"
podman stop sbm-auth >/dev/null
```

Expected: `401`, `200`, `51`, `0` (the token never appears in the logs).

- [ ] **Step 4: Stop and hand over**

Report. Push, tag and the Portainer update (the user must set `MCP_AUTH_TOKEN` in the stack and re-add the client with `--header`) each wait for the user's go-ahead.
