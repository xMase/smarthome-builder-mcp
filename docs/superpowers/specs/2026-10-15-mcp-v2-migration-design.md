# Migrate to the `mcp` SDK 2.x — design

Date: 2026-10-15 · Target release: **v0.3.0**

## Goal

Run the server on `mcp>=2,<3` (verified against 2.3.0) with no change to the tool surface, the
transports, or the configuration. v0.2.0 crashed on import because the image pulled mcp 2.x; v0.2.1
capped `mcp<2` as a shortcut. This removes the cap.

Success criteria:

- `pytest -q` passes (291 tests today, plus the new/changed ones below).
- `scripts/smoke_e2e.py` passes over stdio against a throwaway Node-RED.
- `podman build` succeeds; the container answers `initialize` with
  `serverInfo = {"name": "smarthome-builder-mcp", "version": "0.3.0"}` and `tools/list` returns 51 tools.

## Decisions not to reopen

- Python module `nrha_mcp`, the `NRHA_*` variables and the client key `nrha` stay as they are.
- The old GHCR package `node-red-homeassistant-mcp` stays.
- No new variables in the repo `.env`. `MCP_TRANSPORT` / `MCP_HTTP_HOST` / `MCP_HTTP_PORT` keep being
  read by our own `Settings` (pydantic-settings); the SDK no longer reads `MCP_*` itself, which does
  not affect us.

## What changes

### `pyproject.toml`

- `mcp>=1.0.0,<2` (and its `shortcut:` comment) → `mcp>=2,<3`.
- `version = "0.3.0"`.
- `httpx` and `pydantic-settings` stay explicit: mcp 2 depends on `httpx2`, not `httpx`, and no
  longer pulls `pydantic-settings`; our code uses both directly.

### `src/nrha_mcp/server.py`

- `from mcp.server.fastmcp import FastMCP` → `from mcp.server.mcpserver import MCPServer`.
- Constructor: name positional, everything else by keyword (v2 reordered positionals):
  `MCPServer("smarthome-builder-mcp", instructions=HA_AUTHORING_GUIDANCE, version=<package version>)`.
  The version comes from `importlib.metadata.version("smarthome-builder-mcp")`, so `pyproject.toml`
  stays the single source. Without it, v2 reports an empty version (v1 reported the SDK's, `1.30.0`).
- Transport options move from the constructor to `main()`:
  - `http` → `mcp.run(transport="streamable-http", host=settings.mcp_http_host,
    port=settings.mcp_http_port, json_response=True, stateless_http=True)`.
  - `stdio` → `mcp.run(transport="stdio")`.
- No `transport_security` argument. DNS-rebinding protection is the SDK default: on when `host` is
  `127.0.0.1` / `localhost` / `::1`, off for `0.0.0.0` (the Docker default). Behaviour on the
  Portainer stack (`0.0.0.0`, `"8090:8080"`) is unchanged.
- Module docstring: "FastMCP server" → "MCP server".
- The 51 tools, `_wrap`, `_ok`, `_err` are untouched (all are `async`; none use `Context` or raise
  `McpError`).

### Tests

- `tests/test_server_transport.py`: `mcp.settings.host/port` no longer exist. Both tests replace
  `srv.mcp.run` with a stub that captures `(transport, **kwargs)` and assert:
  - http mode → `transport == "streamable-http"`, `host`/`port` from the env,
    `json_response is True`, `stateless_http is True`, no `transport_security` key;
  - stdio mode → `transport == "stdio"` and no kwargs.
- New test: the server reports the package version
  (`importlib.metadata.version("smarthome-builder-mcp")`) and is not empty.
- `tests/test_server_smoke.py`, `tests/test_tool_count_sync.py`: no logic change —
  `_tool_manager._tools` still exists in 2.3.0. Only the "FastMCP" wording in comments is updated.
- `scripts/smoke_e2e.py`: no change expected. It uses `stdio_client`, `StdioServerParameters`,
  `ClientSession.initialize/list_tools/call_tool` and `result.content`, all unchanged; there are no
  `inputSchema` / `isError` accesses to rename. It is run to confirm.

### Docs

- `CLAUDE.md`: "A FastMCP server (`mcp` SDK)" → "An MCP server (`mcp` SDK 2.x, `MCPServer`)".
- README has no FastMCP / SDK-version text; no change.

## Out of scope

- Moving tests off the private `_tool_manager._tools` to the public `list_tools()`.
- The SDK's new 4 MiB request-body limit: our largest payloads (flows, ESPHome YAML) are far below it.
- OpenTelemetry (on by default in v2, a no-op without an OTel SDK).

## Verification and release

1. Reinstall the dev venv (`uv pip install -e ".[dev]"`) and confirm `mcp` resolves to 2.x.
2. `pytest -q`.
3. `docker compose up -d nodered` → `NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py`
   → `docker compose down -v`.
4. `podman build`, run the image with `MCP_TRANSPORT=http`, `curl` `initialize` and `tools/list`
   (version `0.3.0`, 51 tools, 11 `esphome_*`).
5. Commit with explicit paths. Push and tag `v0.3.0` only after the user's go-ahead.
6. Portainer stack update on the user's HA only after the user's go-ahead, then repeat the step-4
   check against `http://192.168.200.6:8090/mcp`.
