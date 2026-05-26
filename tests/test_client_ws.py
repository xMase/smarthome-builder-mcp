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
