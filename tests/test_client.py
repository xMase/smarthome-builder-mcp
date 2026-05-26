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


def test_token_mode_defers_login():
    """In token mode, no static auth at construction — Authorization header absent."""
    c = make_client(nr_user="u", nr_pass="p", nr_auth="token")
    assert c._client.auth is None
    assert "authorization" not in c._client.headers


def test_basic_auth_default():
    """In basic mode (default), httpx.BasicAuth is set on the client at construction."""
    c = make_client(nr_user="u", nr_pass="p")
    assert c._client.auth is not None
    assert isinstance(c._client.auth, httpx.BasicAuth)


@respx.mock
@pytest.mark.asyncio
async def test_basic_auth_sends_basic_header():
    """In basic mode, GET /flows carries an Authorization: Basic ... header."""
    respx.get("http://nr.test/flows").mock(
        return_value=httpx.Response(200, json={"rev": "r1", "flows": []})
    )
    c = make_client(nr_user="u", nr_pass="p")  # default nr_auth="basic"
    await c.get_flows()
    req = respx.routes[0].calls.last.request
    assert req.headers["Authorization"].startswith("Basic ")
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_basic_mode_does_not_call_auth_token():
    """In basic mode, /auth/token is never hit — only GET /flows."""
    respx.get("http://nr.test/flows").mock(
        return_value=httpx.Response(200, json={"rev": "r", "flows": []})
    )
    c = make_client(nr_user="u", nr_pass="p")  # default nr_auth="basic"
    await c.get_flows()
    # /auth/token route is never registered; respx strict mode would raise if it were called.
    # Confirm by checking only one route was used and it is the flows route.
    assert len(respx.routes) == 1
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_password_login_then_authorizes_requests():
    """In token mode, on first API call the client POSTs /auth/token, then uses the returned bearer."""
    respx.post("http://nr.test/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer"})
    )
    respx.get("http://nr.test/flows").mock(
        return_value=httpx.Response(200, json={"rev": "r", "flows": []})
    )
    c = make_client(nr_user="u", nr_pass="p", nr_auth="token")
    await c.get_flows()
    flows_request = respx.routes[1].calls.last.request
    assert flows_request.headers["Authorization"] == "Bearer tok"
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_login_failure_raises():
    """In token mode, non-200 from /auth/token raises NRAuthError."""
    respx.post("http://nr.test/auth/token").mock(return_value=httpx.Response(401))
    c = make_client(nr_user="u", nr_pass="p", nr_auth="token")
    with pytest.raises(NRAuthError):
        await c.get_flows()
    await c.close()


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


@respx.mock
@pytest.mark.asyncio
async def test_token_mode_refreshes_on_401():
    """In token mode, a 401 from the API triggers re-login and a single retry."""
    auth_route = respx.post("http://nr.test/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    flows_route = respx.get("http://nr.test/flows").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"rev": "r", "flows": []}),
        ]
    )
    c = make_client(nr_user="u", nr_pass="p", nr_auth="token")
    flows, rev = await c.get_flows()
    assert rev == "r"
    assert flows == []
    # initial login + one refresh = 2 calls to /auth/token
    assert auth_route.call_count == 2
    # GET /flows was attempted twice (401 then 200)
    assert flows_route.call_count == 2
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_basic_mode_does_not_refresh_on_401():
    """In basic mode, a 401 is NOT retried — it raises NRAuthError immediately."""
    respx.get("http://nr.test/flows").mock(return_value=httpx.Response(401))
    # /auth/token is intentionally NOT registered; respx would error if it were called
    c = make_client(nr_user="u", nr_pass="p")  # default nr_auth="basic"
    with pytest.raises(NRAuthError):
        await c.get_flows()
    await c.close()
