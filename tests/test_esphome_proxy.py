import json

import httpx
import pytest

from nrha_mcp import esphome_proxy
from nrha_mcp.client import NRAuthError
from nrha_mcp.esphome import ESPHomeError
from tests.test_esphome import _ha_frames, _settings
from tests.test_ha_ws import FakeHAWsConn

INGRESS = "/api/hassio_ingress/abc"


async def _rpc(tmp_path, handler, method="tools/list", params=None):
    frames = _ha_frames(info={"state": "started", "ingress": True, "ingress_url": INGRESS})

    async def ha_connect(settings):
        return FakeHAWsConn(frames)

    return await esphome_proxy.rpc(_settings(tmp_path), method, params, ha_connect=ha_connect,
                                   transport=httpx.MockTransport(handler))


async def test_rpc_posts_jsonrpc_with_ingress_cookie(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    assert await _rpc(tmp_path, handler) == {"tools": []}
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "http://ha:8123/api/hassio_ingress/abc/api/mcp"
    assert "ingress_session=SESS" in req.headers["cookie"]
    body = json.loads(req.content)
    assert body["jsonrpc"] == "2.0" and body["method"] == "tools/list" and "id" in body


async def test_rpc_404_is_version_error(tmp_path):
    with pytest.raises(ESPHomeError, match=">= 1.15"):
        await _rpc(tmp_path, lambda r: httpx.Response(404))


@pytest.mark.parametrize("status", [401, 403])
async def test_rpc_401_403_is_auth_error(tmp_path, status):
    with pytest.raises(NRAuthError):
        await _rpc(tmp_path, lambda r: httpx.Response(status))


async def test_rpc_unknown_tool_is_stale_error(tmp_path):
    reply = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Unknown tool: foo"}}
    with pytest.raises(ESPHomeError, match=r"(?s)Unknown tool: foo.*/mcp"):
        await _rpc(tmp_path, lambda r: httpx.Response(200, json=reply), "tools/call", {"name": "foo"})


async def test_rpc_other_jsonrpc_error_passes_message(tmp_path):
    reply = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"}}
    with pytest.raises(ESPHomeError, match="boom"):
        await _rpc(tmp_path, lambda r: httpx.Response(200, json=reply))


async def test_rpc_timeout_is_esphome_error(tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ESPHomeError, match="ReadTimeout"):
        await _rpc(tmp_path, handler)


# ---- tool list and gated call -------------------------------------------------

from pathlib import Path

from nrha_mcp.client import NRConflictError, NRNotFoundError

_FIXTURE = Path(__file__).parent / "fixtures" / "esphome_mcp_tools_list_1.25.0.json"
_ERR = object()


def _text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class FakeRpc:
    """Records (method, params); answers tools/call per tool name (a value or a list consumed in order)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}
        self.list_result = json.loads(_FIXTURE.read_text())["result"]  # one object, to detect mutation

    async def __call__(self, settings, method, params=None, **kw):
        self.calls.append((method, params))
        if method == "tools/list":
            return self.list_result
        ans = self.answers[params["name"]]
        if isinstance(ans, list):
            ans = ans.pop(0)
        return ans

    @property
    def tools_called(self):
        return [p["name"] for m, p in self.calls if m == "tools/call"]


def _backups(tmp_path):
    d = tmp_path / "esphome"
    return list(d.iterdir()) if d.is_dir() else []


def test_gate_class():
    g = esphome_proxy.gate_class
    assert [g("get_config"), g("compile"), g("update_config"), g("install"),
            g("set_secret"), g("delete_device")] == ["none", "none", "deploy_mode", "always",
                                                     "always", "always"]


async def test_list_tools_renames_and_adds_confirm(tmp_path):
    fake = FakeRpc()
    tools = await esphome_proxy.list_tools(_settings(tmp_path), rpc=fake)
    by = {t["name"]: t for t in tools}
    assert len(tools) == 21 and all(n.startswith("esphome_") for n in by)
    assert "confirm" not in by["esphome_get_config"]["inputSchema"]["properties"]
    for n in ("esphome_update_config", "esphome_install"):
        assert by[n]["inputSchema"]["properties"]["confirm"]["type"] == "boolean"
        assert "confirm=true" in by[n]["description"]
    pristine = {t["name"]: t for t in json.loads(_FIXTURE.read_text())["result"]["tools"]}
    for t in fake.list_result["tools"]:  # the objects list_tools received are untouched
        assert "confirm" not in t["inputSchema"]["properties"]
        assert t["inputSchema"]["required"] == pristine[t["name"]]["inputSchema"]["required"]
        assert by['esphome_' + t["name"]]["inputSchema"]["required"] == t["inputSchema"]["required"]


async def test_read_tool_forwards_without_confirm(tmp_path):
    rows = [{"configuration": "a.yaml", "state": "online"}]
    fake = FakeRpc({"list_devices": _text(json.dumps(rows))})
    assert await esphome_proxy.call(_settings(tmp_path), "list_devices", {}, rpc=fake) == {"result": rows}
    assert fake.calls == [("tools/call", {"name": "list_devices", "arguments": {}})]


async def test_non_json_text_returned_as_text(tmp_path):
    fake = FakeRpc({"get_config": _text("esphome:\n  name: x\n")})
    out = await esphome_proxy.call(_settings(tmp_path), "get_config", {"configuration": "x.yaml"}, rpc=fake)
    assert out == {"result": "esphome:\n  name: x\n"}


async def test_confirm_stripped_for_no_confirm_tool(tmp_path):
    fake = FakeRpc({"compile": _text('{"job_id": "1"}')})
    await esphome_proxy.call(_settings(tmp_path), "compile",
                             {"configuration": "x.yaml", "confirm": True}, rpc=fake)
    assert fake.calls[0][1]["arguments"] == {"configuration": "x.yaml"}


async def test_secrets_blocked_before_network(tmp_path):
    fake = FakeRpc()
    with pytest.raises(ESPHomeError):
        await esphome_proxy.call(_settings(tmp_path), "get_config",
                                 {"configuration": "secrets.yaml"}, rpc=fake)
    assert fake.calls == []


@pytest.mark.parametrize("code,exc", [("not_found", NRNotFoundError),
                                      ("precondition_failed", NRConflictError),
                                      ("invalid_args", ESPHomeError)])
async def test_is_error_maps_code(tmp_path, code, exc):
    msg = f"{code}: Device 'x.yaml' not found"
    fake = FakeRpc({"get_config": _text(msg, True)})
    with pytest.raises(exc, match="Device 'x.yaml'"):
        await esphome_proxy.call(_settings(tmp_path), "get_config", {"configuration": "x.yaml"}, rpc=fake)


async def test_update_config_preview_in_propose(tmp_path):
    fake = FakeRpc({"get_config": _text("a: 1\n")})
    out = await esphome_proxy.call(_settings(tmp_path), "update_config",
                                   {"configuration": "x.yaml", "content": "a: 2\n"}, rpc=fake)
    assert out["requires_confirm"] is True and "-a: 1" in out["diff"] and "+a: 2" in out["diff"]
    assert fake.tools_called == ["get_config"] and _backups(tmp_path) == []


async def test_update_config_preview_missing_file(tmp_path):
    fake = FakeRpc({"get_config": _text("not_found: nope", True)})
    with pytest.raises(NRNotFoundError):
        await esphome_proxy.call(_settings(tmp_path), "update_config",
                                 {"configuration": "x.yaml", "content": "a: 2\n"}, rpc=fake)
    assert fake.tools_called == ["get_config"] and _backups(tmp_path) == []


async def test_update_config_auto_runs_with_backup_and_diff(tmp_path):
    fake = FakeRpc({"get_config": [_text("a: 1\n"), _text("a: 2\n")], "update_config": _text('{"ok": true}')})
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "update_config",
                                   {"configuration": "x.yaml", "content": "a: 2\n"}, rpc=fake)
    assert fake.tools_called == ["get_config", "update_config", "get_config"]
    assert len(_backups(tmp_path)) == 1
    assert out["result"] == {"ok": True} and out["backup_id"] and "+a: 2" in out["diff"]


async def test_install_always_needs_confirm(tmp_path):
    rows = [{"configuration": "o.yaml", "state": "unknown", "offline_seconds": 12.5},
            {"configuration": "p.yaml", "state": "online", "offline_seconds": None}]
    fake = FakeRpc({"list_devices": _text(json.dumps(rows)), "install": _text('{"job_id": "9"}')})
    s = _settings(tmp_path, deploy_mode="auto")
    out = await esphome_proxy.call(s, "install", {"configuration": "o.yaml"}, rpc=fake)
    assert out["requires_confirm"] is True and out["device"] == rows[0] and out["warning"]
    assert fake.tools_called == ["list_devices"]
    out = await esphome_proxy.call(s, "install", {"configuration": "p.yaml"}, rpc=fake)
    assert "warning" not in out
    fake.calls.clear()
    out = await esphome_proxy.call(s, "install", {"configuration": "o.yaml", "confirm": True}, rpc=fake)
    assert fake.tools_called == ["install"] and out == {"result": {"job_id": "9"}}
    assert _backups(tmp_path) == []


async def test_unknown_tool_always_gated(tmp_path):
    fake = FakeRpc()
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "delete_device",
                                   {"name": "x"}, rpc=fake)
    assert out["requires_confirm"] is True and fake.calls == []


async def test_set_secret_backs_up_secrets_without_returning_them(tmp_path):
    fake = FakeRpc({"get_config": _text("wifi: hunter2\n"), "set_secret": _text('{"ok": true}')})
    out = await esphome_proxy.call(_settings(tmp_path), "set_secret",
                                   {"name": "k", "value": "v", "confirm": True}, rpc=fake)
    assert fake.tools_called == ["get_config", "set_secret"]
    assert fake.calls[0][1]["arguments"] == {"configuration": "secrets.yaml"}
    assert len(_backups(tmp_path)) == 1 and out["backup_id"]
    assert "hunter2" not in json.dumps(out)


async def test_set_secret_creates_missing_secrets_file(tmp_path):
    fake = FakeRpc({"get_config": _text("not_found: no file", True), "set_secret": _text('{"ok": true}')})
    out = await esphome_proxy.call(_settings(tmp_path), "set_secret",
                                   {"name": "k", "value": "v", "confirm": True}, rpc=fake)
    assert fake.tools_called == ["get_config", "set_secret"] and "backup_id" not in out
    assert _backups(tmp_path) == []


async def test_backend_rendered_write_previews_call(tmp_path):
    args = {"configuration": "x.yaml", "location": "on_boot", "automation": {}}
    fake = FakeRpc()
    out = await esphome_proxy.call(_settings(tmp_path), "upsert_automation", dict(args), rpc=fake)
    assert out == {"preview": True, "tool": "upsert_automation", "arguments": args,
                   "requires_confirm": True}
    assert fake.calls == []


async def test_string_confirm_is_not_confirmation(tmp_path):
    fake = FakeRpc({"list_devices": _text("[]")})
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "install",
                                   {"configuration": "o.yaml", "confirm": "false"}, rpc=fake)
    assert out["requires_confirm"] is True and fake.tools_called == ["list_devices"]


async def test_failed_after_read_keeps_write_result(tmp_path):
    fake = FakeRpc({"get_config": [_text("a: 1\n"), _text("boom: x", True)],
                    "update_config": _text('{"ok": true}')})
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "update_config",
                                   {"configuration": "x.yaml", "content": "a: 2\n"}, rpc=fake)
    assert out["result"] == {"ok": True} and out["backup_id"] and "warning" in out and "diff" not in out


async def test_json_flow_yaml_is_kept_as_text(tmp_path):
    flow = '{"esphome": {"name": "x"}}'
    fake = FakeRpc({"get_config": [_text(flow), _text(flow)], "update_config": _text('{"ok": true}')})
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "update_config",
                                   {"configuration": "x.yaml", "content": flow}, rpc=fake)
    assert out["backup_id"] and "diff" in out
    assert (tmp_path / "esphome" / out["backup_id"]).read_text() == flow


async def test_update_config_propose_with_confirm_runs_with_one_backup(tmp_path):
    fake = FakeRpc({"get_config": [_text("a: 1\n"), _text("a: 2\n")], "update_config": _text('{"ok": true}')})
    await esphome_proxy.call(_settings(tmp_path, deploy_mode="propose"), "update_config",
                             {"configuration": "x.yaml", "content": "a: 2\n", "confirm": True}, rpc=fake)
    assert fake.tools_called == ["get_config", "update_config", "get_config"]
    assert len(_backups(tmp_path)) == 1


async def test_install_warns_when_live_offline_signal(tmp_path):
    # Live Device Builder rows: offline devices keep state "unknown" and carry offline_seconds.
    rows = [{"configuration": "o.yaml", "state": "unknown", "offline_seconds": 12.5}]
    fake = FakeRpc({"list_devices": _text(json.dumps(rows))})
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "install",
                                   {"configuration": "o.yaml"}, rpc=fake)
    assert out["warning"]


async def test_install_no_warning_for_online_device(tmp_path):
    rows = [{"configuration": "p.yaml", "state": "online", "offline_seconds": None}]
    fake = FakeRpc({"list_devices": _text(json.dumps(rows))})
    out = await esphome_proxy.call(_settings(tmp_path, deploy_mode="auto"), "install",
                                   {"configuration": "p.yaml"}, rpc=fake)
    assert "warning" not in out
