# mcp SDK 2.x Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run smarthome-builder-mcp on `mcp>=2,<3` and release it as v0.3.0, with the same 51 tools, transports and configuration.

**Architecture:** Only the SDK seam changes. `server.py` builds an `MCPServer` (was `FastMCP`) with an explicit `version=`; the HTTP options (`host`, `port`, `json_response`, `stateless_http`) move from the constructor to `mcp.run(...)` in `main()`. Domain modules, `_wrap`, and the tools are untouched.

**Tech Stack:** Python 3.11+, `mcp` 2.x (`mcp.server.mcpserver.MCPServer`), pydantic-settings, pytest + pytest-asyncio, podman.

**Spec:** `docs/superpowers/specs/2026-10-15-mcp-v2-migration-design.md`

## Global Constraints

- Dependency pin: `mcp>=2,<3`. Package version: `0.3.0`.
- `httpx` and `pydantic-settings` stay as explicit dependencies (mcp 2 uses `httpx2` and no longer pulls `pydantic-settings`).
- Python module `nrha_mcp`, `NRHA_*` variables, client key `nrha`: unchanged.
- No new variables in the repo `.env`. `MCP_TRANSPORT` / `MCP_HTTP_HOST` / `MCP_HTTP_PORT` stay read by our `Settings`.
- No `transport_security` argument: DNS-rebinding protection is the SDK default (on for `127.0.0.1`/`localhost`/`::1`, off for `0.0.0.0`).
- Commits stage files by explicit path and end with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- No push, no tag, no change on the user's HA without the user's go-ahead.

## Review Focus

- Bind host `127.0.0.1` in http mode → the SDK must reject a foreign `Host` header with 421 (DNS rebinding). Pinned by `test_localhost_bind_rejects_foreign_host_header` in Task 1.
- Bind host `0.0.0.0` (Docker / Portainer) → a LAN `Host` such as `192.168.200.6:8090` must still get 200, or the live stack breaks. Pinned by `test_wildcard_bind_accepts_lan_host_header` in Task 1.
- Server version → must be the package version, never empty or the SDK's (`1.30.0` today). Pinned by `test_server_reports_package_version` in Task 1 and the container check in Task 2.
- v2 reordered constructor positionals → `instructions` passed positionally would land in `title`. Pinned by `test_instructions_are_the_authoring_guidance` in Task 1.
- Real stdio client against the real server → `list_tools` / `call_tool` must still parse. Covered by `scripts/smoke_e2e.py` in Task 2.

---

### Task 1: Switch the server to MCPServer on mcp 2.x

**Files:**
- Modify: `pyproject.toml` (line 7 `version`, line 23 `mcp` pin)
- Modify: `src/nrha_mcp/server.py` (lines 1-5 docstring/imports, lines 35-42 constructor, `main()` at the end of the file)
- Modify (rewrite): `tests/test_server_transport.py`
- Modify (comment only): `tests/test_server_smoke.py:6`, `tests/test_tool_count_sync.py:7` and `:22`
- Modify: `CLAUDE.md:7`

**Interfaces:**
- Consumes: `Settings.mcp_transport: Literal["stdio","http"]`, `Settings.mcp_http_host: str`, `Settings.mcp_http_port: int` (`src/nrha_mcp/config.py`, unchanged).
- Produces: module-level `nrha_mcp.server.mcp: MCPServer` with `.name == "smarthome-builder-mcp"`, `.version == importlib.metadata.version("smarthome-builder-mcp")`, `.instructions == HA_AUTHORING_GUIDANCE`; `main()` calls `mcp.run(transport="stdio")` or `mcp.run(transport="streamable-http", host=..., port=..., json_response=True, stateless_http=True)`.

- [ ] **Step 1: Write the failing tests**

Replace the whole content of `tests/test_server_transport.py` with:

```python
import importlib
import importlib.metadata

from starlette.testclient import TestClient

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}
_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _fresh_server(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import nrha_mcp.server as srv
    return importlib.reload(srv)


def _capture_run(monkeypatch, srv):
    captured = {}

    def fake_run(transport, **kwargs):
        captured["transport"] = transport
        captured["kwargs"] = kwargs

    monkeypatch.setattr(srv.mcp, "run", fake_run)
    return captured


def _post_initialize(srv, bind_host, host_header):
    # Same HTTP options main() passes to run(); host only drives the SDK's DNS-rebinding check.
    app = srv.mcp.streamable_http_app(host=bind_host, json_response=True, stateless_http=True)
    with TestClient(app) as c:
        return c.post("/mcp", json=_INIT, headers={**_HEADERS, "Host": host_header})


def test_http_mode_passes_bind_and_http_options_to_run(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http",
                        MCP_HTTP_HOST="0.0.0.0", MCP_HTTP_PORT="8080")
    captured = _capture_run(monkeypatch, srv)
    srv.main()
    assert captured["transport"] == "streamable-http"
    # Exact dict: no transport_security, so the SDK default protection applies.
    assert captured["kwargs"] == {"host": "0.0.0.0", "port": 8080,
                                  "json_response": True, "stateless_http": True}


def test_http_mode_localhost_bind_is_passed_through(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http",
                        MCP_HTTP_HOST="127.0.0.1", MCP_HTTP_PORT="9001")
    captured = _capture_run(monkeypatch, srv)
    srv.main()
    assert captured["kwargs"] == {"host": "127.0.0.1", "port": 9001,
                                  "json_response": True, "stateless_http": True}


def test_stdio_is_default(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    captured = _capture_run(monkeypatch, srv)
    srv.main()
    assert captured == {"transport": "stdio", "kwargs": {}}


def test_server_reports_package_version(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    expected = importlib.metadata.version("smarthome-builder-mcp")
    assert expected
    assert srv.mcp.name == "smarthome-builder-mcp"
    assert srv.mcp.version == expected


def test_instructions_are_the_authoring_guidance(monkeypatch):
    # v2 reordered positionals: instructions must be passed by keyword, not land in title.
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    assert srv.mcp.instructions == srv.HA_AUTHORING_GUIDANCE


def test_localhost_bind_rejects_foreign_host_header(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http")
    assert _post_initialize(srv, "127.0.0.1", "evil.example:8080").status_code == 421
    assert _post_initialize(srv, "127.0.0.1", "127.0.0.1:8080").status_code == 200


def test_wildcard_bind_accepts_lan_host_header(monkeypatch):
    # The Portainer stack binds 0.0.0.0 and is reached as 192.168.200.6:8090.
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http")
    r = _post_initialize(srv, "0.0.0.0", "192.168.200.6:8090")
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["version"] == importlib.metadata.version("smarthome-builder-mcp")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/bin/pytest tests/test_server_transport.py -q`
Expected: FAIL. With mcp 1.27.1, `run` is still called as `run(transport=...)` with no host/port kwargs, `FastMCP.version` does not exist, and `streamable_http_app(host=...)` is not accepted.

- [ ] **Step 3: Bump the pin and the version in `pyproject.toml`**

Change line 7:

```toml
version = "0.3.0"
```

Change line 23 (drop the `shortcut:` comment):

```toml
    "mcp>=2,<3",
```

Leave `httpx>=0.27.0` and `pydantic-settings>=2.0.0` as they are.

- [ ] **Step 4: Reinstall the dev venv and confirm mcp 2.x**

Run:

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'), m.version('smarthome-builder-mcp'))"
```

Expected: `2.x.y 0.3.0` (2.3.0 or later). The whole suite now fails on `import mcp.server.fastmcp` — fixed by the next step.

- [ ] **Step 5: Migrate `src/nrha_mcp/server.py`**

Replace lines 1-5:

```python
"""MCP server for smarthome-builder-mcp."""

import json
from importlib.metadata import version

from mcp.server.mcpserver import MCPServer
```

Replace the constructor (lines 35-42):

```python
mcp = MCPServer(
    "smarthome-builder-mcp",
    instructions=HA_AUTHORING_GUIDANCE,
    version=version("smarthome-builder-mcp"),
)
```

Replace `main()`:

```python
def main():
    """Console-script entry point."""
    if settings.mcp_transport == "http":
        mcp.run(transport="streamable-http", host=settings.mcp_http_host,
                port=settings.mcp_http_port, json_response=True, stateless_http=True)
    else:
        mcp.run(transport="stdio")
```

Do not touch the tools, `_ok`, `_err`, `_wrap`.

- [ ] **Step 6: Update the "FastMCP" wording**

- `tests/test_server_smoke.py:6`: `# FastMCP 1.27.1 stores registered tools in _tool_manager._tools (dict keyed by name).` → `# MCPServer (mcp 2.x) stores registered tools in _tool_manager._tools (dict keyed by name).`
- `tests/test_tool_count_sync.py:7`: `the tools actually registered on the FastMCP server.` → `the tools actually registered on the MCPServer.`
- `tests/test_tool_count_sync.py:22`: `# FastMCP stores registered tools in _tool_manager._tools (keyed by name).` → `# MCPServer stores registered tools in _tool_manager._tools (keyed by name).`
- `CLAUDE.md:7`: `A FastMCP server (\`mcp\` SDK) that lets` → `An MCP server (\`mcp\` SDK 2.x, \`MCPServer\`) that lets`

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass — 296 tests (291 before, minus the 2 old transport tests, plus the 7 new ones).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/nrha_mcp/server.py tests/test_server_transport.py \
        tests/test_server_smoke.py tests/test_tool_count_sync.py CLAUDE.md
git commit -m "feat!: migrate to mcp SDK 2.x (MCPServer), release 0.3.0

FastMCP -> MCPServer; HTTP options move from the constructor to run();
explicit version= so serverInfo reports the package version.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: End-to-end and container verification

No code change expected. If a check fails, stop and report the output; any fix gets its own commit with explicit paths.

**Files:** none (runs `scripts/smoke_e2e.py`, `Dockerfile`, `docker-compose.yml` as they are).

**Interfaces:**
- Consumes: Task 1's `main()` and `mcp` (server name/version, 51 tools).
- Produces: nothing for later tasks — a pass/fail report.

- [ ] **Step 1: stdio smoke test against a throwaway Node-RED**

```bash
docker compose up -d nodered
sleep 10
NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py
docker compose down -v
```

Expected: every step prints `OK`, script exits 0, step 1 reports 51 tools registered.

- [ ] **Step 2: Build the image with podman**

```bash
podman build -t smarthome-builder-mcp:0.3.0 .
podman run --rm smarthome-builder-mcp:0.3.0 python -c "import importlib.metadata as m; print(m.version('mcp'), m.version('smarthome-builder-mcp'))"
```

Expected: build succeeds; prints `2.x.y 0.3.0`.

- [ ] **Step 3: Run the container in http mode and query it**

```bash
podman run -d --rm --name sbm-v030 -p 18080:8080 smarthome-builder-mcp:0.3.0
sleep 3
H='-H Content-Type:application/json -H Accept:application/json,text/event-stream'
curl -s $H -X POST http://127.0.0.1:18080/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["serverInfo"])'
curl -s $H -X POST http://127.0.0.1:18080/mcp \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 -c 'import json,sys; t=[x["name"] for x in json.load(sys.stdin)["result"]["tools"]]; print(len(t), sum(n.startswith("esphome_") for n in t))'
podman stop sbm-v030
```

Expected: `{'name': 'smarthome-builder-mcp', 'version': '0.3.0'}` then `51 11`.

- [ ] **Step 4: Stop and hand over to the user**

Report the results. Push of `main`, tag `v0.3.0`, and the Portainer stack update on the user's HA each wait for the user's explicit go-ahead. After the stack update (with go-ahead), repeat Step 3's two `curl` calls against `http://192.168.200.6:8090/mcp`.
