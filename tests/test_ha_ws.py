import collections

import pytest

from nrha_mcp import ha_ws
from nrha_mcp.config import Settings


class FakeHAWsConn:
    """In-memory HA WebSocket: yields scripted frames, records sent messages."""

    def __init__(self, recv_frames):
        self._recv = collections.deque(recv_frames)
        self.sent = []
        self.closed = False

    async def send(self, obj):
        self.sent.append(obj)

    async def recv(self):
        return self._recv.popleft()

    async def close(self):
        self.closed = True


def _connect_factory(conn):
    async def _connect(settings):
        return conn
    return _connect


def _bucket(start_ms, mean, mn, mx):
    return {"start": start_ms, "end": start_ms + 1000, "mean": mean, "min": mn, "max": mx}


def _result_frame(entity, buckets):
    return {"id": 1, "type": "result", "success": True, "result": {entity: buckets}}


def _settings():
    return Settings(ha_url="http://ha:8123", ha_token="tok")


def test_ws_url():
    assert ha_ws._ws_url("http://ha:8123") == "ws://ha:8123/api/websocket"
    assert ha_ws._ws_url("https://ha:8123/") == "wss://ha:8123/api/websocket"
