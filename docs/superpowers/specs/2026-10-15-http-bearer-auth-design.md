# Bearer-token authentication for the HTTP transport — design

Date: 2026-10-15

## Problem

In `http` mode the server has no authentication. Anyone who can reach the port (today
`192.168.200.6:8090` on the LAN) can call every tool with `confirm=true`, including
`ha_call_service`, `ha_ws_command`, `esphome_install` and `nr_install_module`. Because the server
binds `0.0.0.0`, the SDK's DNS-rebinding protection is off, so a malicious web page opened on any LAN
machine can reach it too.

## Goal

A static bearer token, required in `http` mode.

Success criteria:

- `http` mode: a request without `Authorization: Bearer <token>`, or with a wrong token, gets 401;
  the right token gets the normal response.
- `http` mode without a token configured: the server refuses to start (exit code ≠ 0, clear message
  on stderr).
- `stdio` mode: no change; no token needed.
- The token never appears in logs or in `repr(Settings)`.

## Decisions (taken with the user; do not reopen)

- Static bearer token, not OAuth. The user accepts configuring the token by hand in the client once;
  there is no "Authenticate" button in `/mcp`.
- Missing token in `http` mode → **refuse to start**, not a warning. Accepted consequence: the
  Portainer stack and the local full `docker compose` stack do not start after the upgrade until
  `MCP_AUTH_TOKEN` is set.
- No new variable in the user's `.env`. `.env.example`, `deploy/portainer-stack.yml`, README and
  CLAUDE.md document it.

## Design

### Setting — `src/nrha_mcp/config.py`

`mcp_auth_token: SecretStr | None = None`, read from `MCP_AUTH_TOKEN` like the other `MCP_*`
fields. `SecretStr` keeps it out of `repr` and logs. An empty or whitespace-only value counts as
missing: this lets tests and users override a token present in `.env` with `MCP_AUTH_TOKEN=""`.

### Verification — `src/nrha_mcp/server.py`

Use the SDK's own resource-server path (verified on mcp 2.3.0), not a custom middleware:

- A small verifier class with `async def verify_token(self, token) -> AccessToken | None` that
  compares with `hmac.compare_digest` (constant time) and returns
  `AccessToken(token=token, client_id="static-token", scopes=[])` on match, `None` otherwise.
- When a token is configured, `MCPServer(...)` also gets `token_verifier=<verifier>` and
  `auth=AuthSettings(issuer_url="http://localhost", resource_server_url=None)`. `issuer_url` is a
  required field of `AuthSettings` but is not published anywhere when there is no
  `auth_server_provider` (probe: no `/.well-known/*` routes, 404). A one-line comment says so.
- When no token is configured, the constructor is exactly as today.
- The SDK then adds its bearer middleware to the streamable-HTTP app: 401 with
  `WWW-Authenticate: Bearer error="invalid_token"` for a missing/wrong token. The `Bearer` scheme
  is matched case-insensitively by the SDK. `stdio` does not use the HTTP app, so it is unaffected.

### Start-up check — `main()`

In `main()`, before `mcp.run(...)` in the `http` branch: if no token is configured, raise
`SystemExit("MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http …")` with a hint to generate one
(`python -c "import secrets; print(secrets.token_urlsafe(32))"`). The check lives in `main()`, not in
`Settings`, so importing the module (tests, stdio) never fails because of it.

`run(...)` arguments do not change.

### Docs and deploy files

- `.env.example`: a commented `# MCP_AUTH_TOKEN=` line next to the `MCP_HTTP_*` lines, with the
  generation command.
- `deploy/portainer-stack.yml`: `MCP_AUTH_TOKEN: "${MCP_AUTH_TOKEN}"` in `environment:`, with a comment
  that it is required and set in the stack's Environment variables UI.
- `docker-compose.yml`: no change — `env_file: .env` already passes it; update the header comment
  to say the token must be in `.env`.
- README: a row in the configuration table; the HTTP `claude mcp add` example gets
  `--header "Authorization: Bearer <token>"`; one line on the refusal to start.
- `CLAUDE.md`: one gotcha line — `http` mode needs `MCP_AUTH_TOKEN`; tests set it explicitly.

## Testing

In `tests/test_server_transport.py` (it already reloads the server per test):

- Existing tests that call `main()` in `http` mode set `MCP_AUTH_TOKEN`; their `run` kwargs
  assertions stay identical.
- New: `http` + no token (`MCP_AUTH_TOKEN=""`) → `main()` raises `SystemExit`, `run` not called.
- New: `http` + whitespace-only token → `SystemExit`.
- New: `stdio` + no token → `run(transport="stdio")` as today.
- New, on the real Starlette app (`streamable_http_app`, like the DNS tests): with a token, no header
  → 401; wrong token → 401; right token → 200.
- New: `repr(Settings(...))` with a token does not contain the token value.

Tests that need "no token" set `MCP_AUTH_TOKEN=""` explicitly, so a token in the developer's `.env`
cannot change their result.

Verification before release: `pytest -q`; `smoke_e2e.py` (stdio, unaffected); `podman build`, then
run the container (a) without the token → exits ≠ 0 with the message, (b) with the token → `curl`
without header 401, with header `initialize` 200 and `tools/list` 51.

## Out of scope

- OAuth / the `/mcp` "Authenticate" button.
- Multiple tokens, token rotation without restart, per-tool scopes.
- A minimum token length or strength check.
- TLS: the token travels in clear on the LAN, like today's traffic. A reverse proxy with TLS is the
  user's choice and needs no code.
