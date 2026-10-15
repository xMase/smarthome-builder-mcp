import importlib
import importlib.metadata
import tomllib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}
_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _fresh_server(monkeypatch, **env):
    env.setdefault("MCP_AUTH_TOKEN", "")  # "" overrides a token in a local .env
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


def _post_initialize(srv, bind_host, host_header, headers=None):
    # Same HTTP options main() passes to run(); host only drives the SDK's DNS-rebinding check.
    app = srv.mcp.streamable_http_app(host=bind_host, json_response=True, stateless_http=True)
    with TestClient(app) as c:
        return c.post("/mcp", json=_INIT, headers={**_HEADERS, "Host": host_header, **(headers or {})})


def test_http_mode_passes_bind_and_http_options_to_run(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http",
                        MCP_HTTP_HOST="0.0.0.0", MCP_HTTP_PORT="8080", MCP_AUTH_TOKEN="t0ken")
    captured = _capture_run(monkeypatch, srv)
    srv.main()
    assert captured["transport"] == "streamable-http"
    # Exact dict: no transport_security, so the SDK default protection applies.
    assert captured["kwargs"] == {"host": "0.0.0.0", "port": 8080,
                                  "json_response": True, "stateless_http": True}


def test_http_mode_localhost_bind_is_passed_through(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http",
                        MCP_HTTP_HOST="127.0.0.1", MCP_HTTP_PORT="9001", MCP_AUTH_TOKEN="t0ken")
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
    # Compare with pyproject, not the installed metadata: catches a stale editable install.
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    with open(Path(__file__).resolve().parent.parent / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert srv.mcp.name == "smarthome-builder-mcp"
    assert srv.mcp.version == expected


def test_uninstalled_checkout_still_starts(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata, "version", missing)
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    assert srv.mcp.version == "0.0.0+unknown"


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


def test_non_ascii_token_authenticates(monkeypatch):
    # Starlette decodes header bytes as latin-1; the client sends the token as UTF-8.
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http", MCP_AUTH_TOKEN="sécret")
    host = "192.168.200.6:8090"
    assert _post_initialize(srv, "0.0.0.0", host,
                            {"Authorization": "Bearer sécret".encode()}).status_code == 200
    assert _post_initialize(srv, "0.0.0.0", host,
                            {"Authorization": "Bearer secret"}).status_code == 401
