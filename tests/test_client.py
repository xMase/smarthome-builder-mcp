# tests/test_client.py
import httpx
import respx
import pytest

from nrha_mcp.client import NRClient, NRConflictError, NRAuthError, NRNotFoundError
from nrha_mcp.config import Settings


def make_client(**over):
    base = dict(nr_url="http://nr.test", _env_file=None)
    base.update(over)
    return NRClient(Settings(**base))


def test_token_auth_header():
    c = make_client(nr_token="abc")
    assert c._client.headers["Authorization"] == "Bearer abc"


@respx.mock
@pytest.mark.asyncio
async def test_get_flows_returns_flows_and_rev():
    respx.get("http://nr.test/flows").mock(
        return_value=httpx.Response(200, json={"rev": "r1", "flows": [{"id": "a"}]})
    )
    c = make_client()
    flows, rev = await c.get_flows()
    assert rev == "r1" and flows == [{"id": "a"}]
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_post_flows_conflict_raises():
    respx.post("http://nr.test/flows").mock(return_value=httpx.Response(409))
    c = make_client()
    with pytest.raises(NRConflictError):
        await c.post_flows([], "stale")
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_maps():
    respx.get("http://nr.test/flows").mock(return_value=httpx.Response(401))
    c = make_client()
    with pytest.raises(NRAuthError):
        await c.get_flows()
    await c.close()
