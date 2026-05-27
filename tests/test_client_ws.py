import base64

import pytest

from nrha_mcp.client import NRClient
from nrha_mcp.config import Settings


def _client(url, **kw):
    return NRClient(Settings(nr_url=url, **kw))


def test_comms_ws_url_http():
    assert _client("http://host:1880").comms_ws_url() == "ws://host:1880/comms"


def test_comms_ws_url_https_and_trailing_slash():
    assert _client("https://host:1880/").comms_ws_url() == "wss://host:1880/comms"


@pytest.mark.asyncio
async def test_get_ws_token_open_admin_returns_none():
    # basic/open mode, no token, no user -> no token exchange, no network call
    c = _client("http://host:1880")
    assert await c.get_ws_token() is None
    await c.close()


@pytest.mark.asyncio
async def test_ws_auth_header_basic():
    c = _client("http://host:1880", nr_user="admin", nr_pass="secret", nr_auth="basic")
    assert await c.ws_auth_header() == "Basic " + base64.b64encode(b"admin:secret").decode()
    await c.close()


@pytest.mark.asyncio
async def test_ws_auth_header_token():
    c = _client("http://host:1880", nr_token="tok123")
    assert await c.ws_auth_header() == "Bearer tok123"
    await c.close()


@pytest.mark.asyncio
async def test_ws_auth_header_open_returns_none():
    c = _client("http://host:1880", nr_user="", nr_pass="", nr_token="")
    assert await c.ws_auth_header() is None
    await c.close()
