# tests/test_ha.py
import httpx
import pytest
import respx

from nrha_mcp import ha
from nrha_mcp.config import Settings


@pytest.mark.asyncio
async def test_list_ha_nodes(fake_client):
    res = await ha.list_ha_nodes(fake_client)
    ids = [n["id"] for n in res["ha_nodes"]]
    assert "n3" in ids                       # api-call-service node
    n3 = next(n for n in res["ha_nodes"] if n["id"] == "n3")
    assert n3["domain"] == "switch" and n3["service"] == "turn_on"
    assert n3["entity"] == "switch.boiler"


@pytest.mark.asyncio
async def test_get_server_config(fake_client):
    res = await ha.get_server_config(fake_client)
    assert res["servers"][0]["id"] == "srv1"


@pytest.mark.asyncio
@respx.mock
async def test_ha_rest_client_ping_sends_bearer_token():
    route = respx.get("http://ha.test/api/").mock(
        return_value=httpx.Response(200, json={"message": "API running."}))
    client = ha.HARestClient(Settings(ha_url="http://ha.test/", ha_token="t", _env_file=None))
    try:
        assert await client.ping() == {"message": "API running."}
    finally:
        await client.close()
    assert route.calls.last.request.headers["Authorization"] == "Bearer t"
