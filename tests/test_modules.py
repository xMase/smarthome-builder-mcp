# tests/test_modules.py
import pytest

from nrha_mcp import modules
from nrha_mcp.client import NRError


@pytest.mark.asyncio
async def test_list_modules_filters(fake_client):
    fake_client.nodes = [
        {"name": "node-red", "version": "3", "types": ["inject", "debug"]},
        {"name": "node-red-contrib-home-assistant-websocket", "version": "0.6", "types": ["server", "api-call-service"]},
    ]
    res = await modules.get_installed_modules(fake_client, "home-assistant")
    assert res["total"] == 1
    assert res["modules"][0]["name"].endswith("websocket")


@pytest.mark.asyncio
async def test_inject_ok(fake_client):
    res = await modules.inject_node(fake_client, "n1")
    assert res["success"] is True
    assert "n1" in fake_client.injected


@pytest.mark.asyncio
async def test_inject_wrong_type(fake_client):
    with pytest.raises(NRError):
        await modules.inject_node(fake_client, "n2")   # function, not inject
