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
