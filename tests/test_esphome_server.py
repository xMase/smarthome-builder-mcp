import json
import logging

import nrha_mcp.server as srv
from nrha_mcp.client import NRNotFoundError
from nrha_mcp.esphome import ESPHomeError

UPSTREAM = [
    {"name": "esphome_get_config", "description": "read", "inputSchema": {"type": "object", "properties": {"configuration": {"type": "string"}}}},
    {"name": "esphome_list_devices", "description": "list", "inputSchema": {"type": "object"}},
]


def _ha(monkeypatch, on=True):
    monkeypatch.setattr(srv.settings, "ha_url", "http://ha:8123" if on else "")
    monkeypatch.setattr(srv.settings, "ha_token", "tok")


def _fake_list(monkeypatch, tools=None, exc=None):
    calls = []

    async def fake(settings):
        calls.append(1)
        if exc:
            raise exc
        return tools if tools is not None else UPSTREAM

    monkeypatch.setattr(srv.esphome_proxy, "list_tools", fake)
    return calls


def _text(result):
    return result.content[0].text


async def _static_names():
    return {t.name for t in srv.mcp._tool_manager.list_tools()}


async def test_list_tools_merges_upstream(monkeypatch):
    _ha(monkeypatch)
    _fake_list(monkeypatch)
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    assert tools["esphome_list_devices"].input_schema == {"type": "object"}
    assert "configuration" in tools["esphome_get_config"].input_schema["properties"]
    assert await _static_names() <= set(tools)


async def test_list_tools_ha_unset_static_only(monkeypatch):
    _ha(monkeypatch, on=False)
    calls = _fake_list(monkeypatch)
    names = {t.name for t in await srv.mcp.list_tools()}
    assert names == await _static_names()
    assert calls == []


async def test_list_tools_upstream_error_static_only(monkeypatch, caplog):
    _ha(monkeypatch)
    _fake_list(monkeypatch, exc=ESPHomeError("boom"))
    with caplog.at_level(logging.WARNING):
        names = {t.name for t in await srv.mcp.list_tools()}
    assert names == await _static_names()
    assert any("boom" in r.getMessage() for r in caplog.records)


async def test_list_tools_skips_collision(monkeypatch):
    _ha(monkeypatch)
    _fake_list(monkeypatch, tools=[{"name": "esphome_logs", "description": "x", "inputSchema": {"type": "object"}}])
    logs = [t for t in await srv.mcp.list_tools() if t.name == "esphome_logs"]
    assert len(logs) == 1
    assert "seconds" in logs[0].input_schema["properties"]


async def test_call_routes_generated_name(monkeypatch):
    _ha(monkeypatch)
    got = []

    async def fake(settings, tool, arguments):
        got.append((tool, arguments))
        return {"ok": 1}

    monkeypatch.setattr(srv.esphome_proxy, "call", fake)
    res = await srv.mcp.call_tool("esphome_get_config", {"configuration": "x.yaml"})
    assert got == [("get_config", {"configuration": "x.yaml"})]
    assert _text(res) == srv._ok({"ok": 1})


async def test_call_generated_error_is_err_envelope(monkeypatch):
    _ha(monkeypatch)

    async def fake(settings, tool, arguments):
        raise NRNotFoundError("nope")

    monkeypatch.setattr(srv.esphome_proxy, "call", fake)
    res = await srv.mcp.call_tool("esphome_get_config", {})
    assert json.loads(_text(res)) == {"error": "nope"}


async def test_call_generated_needs_ha(monkeypatch):
    _ha(monkeypatch, on=False)
    res = await srv.mcp.call_tool("esphome_get_config", {})
    assert "HA_URL and HA_TOKEN" in json.loads(_text(res))["error"]


async def test_call_bad_name_is_structured_error(monkeypatch):
    _ha(monkeypatch)
    res = await srv.mcp.call_tool("esphome_get_config", {"configuration": "../x.yaml"})
    assert "error" in json.loads(_text(res))


async def test_static_tool_still_works(monkeypatch, tmp_path):
    _ha(monkeypatch, on=False)
    monkeypatch.setattr(srv.settings, "backup_dir", str(tmp_path))
    res = await srv.mcp.call_tool("esphome_list_backups", {})
    assert json.loads(_text(res)) == {"backups": []}
