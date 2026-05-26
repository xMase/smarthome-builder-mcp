import importlib


def _fresh_server(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import nrha_mcp.server as srv
    return importlib.reload(srv)


def test_http_mode_binds_and_selects_streamable(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http",
                        MCP_HTTP_HOST="0.0.0.0", MCP_HTTP_PORT="8080")
    # FastMCP picked up host/port from Settings
    assert srv.mcp.settings.host == "0.0.0.0"
    assert srv.mcp.settings.port == 8080
    # main() runs the streamable-http transport
    captured = {}
    monkeypatch.setattr(srv.mcp, "run", lambda transport: captured.setdefault("t", transport))
    srv.main()
    assert captured["t"] == "streamable-http"


def test_stdio_is_default(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    captured = {}
    monkeypatch.setattr(srv.mcp, "run", lambda transport: captured.setdefault("t", transport))
    srv.main()
    assert captured["t"] == "stdio"
