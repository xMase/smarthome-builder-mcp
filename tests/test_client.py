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


def test_basic_auth_used():
    c = make_client(nr_user="u", nr_pass="p")
    assert c._client.auth is not None


@respx.mock
@pytest.mark.asyncio
async def test_403_maps_to_auth_error():
    respx.get("http://nr.test/flows").mock(return_value=httpx.Response(403))
    c = make_client()
    with pytest.raises(NRAuthError):
        await c.get_flows()
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_404_maps_to_not_found():
    respx.get("http://nr.test/context/flow/x").mock(return_value=httpx.Response(404))
    c = make_client()
    with pytest.raises(NRNotFoundError):
        await c.get_context("x")
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_post_flows_empty_body_ok():
    respx.post("http://nr.test/flows").mock(return_value=httpx.Response(200, content=b""))
    c = make_client()
    result = await c.post_flows([], "r")
    assert result == ""
    await c.close()
