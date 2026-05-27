import collections

import pytest

from nrha_mcp import ha_ws
from nrha_mcp.ha_ws import HAWsError
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


@pytest.mark.asyncio
async def test_statistics_normalizes_buckets():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _result_frame("sensor.x", [_bucket(1746050400000, 0.13, 0.0, 13.3),
                                         _bucket(1748728800000, 0.02, 0.0, 2.7)])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.get_entity_statistics(
        _settings(), "sensor.x", days=365, period="month", connect=_connect_factory(conn))
    assert res["total_buckets"] == 2
    assert res["period"] == "month"
    assert res["buckets"][0]["start"].startswith("2025")   # epoch-ms -> ISO
    assert res["buckets"][0]["mean"] == 0.13
    assert res["first"]["max"] == 13.3
    assert res["last"]["max"] == 2.7
    assert {"type": "auth", "access_token": "tok"} in conn.sent
    cmd = next(m for m in conn.sent if m.get("type") == "recorder/statistics_during_period")
    assert cmd["statistic_ids"] == ["sensor.x"]
    assert cmd["period"] == "month"
    assert conn.closed is True


@pytest.mark.asyncio
async def test_statistics_truncates_to_recent():
    many = [_bucket(1700000000000 + i * 1000, i, i, i) for i in range(60)]
    frames = [{"type": "auth_required"}, {"type": "auth_ok"}, _result_frame("sensor.x", many)]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.get_entity_statistics(
        _settings(), "sensor.x", max_buckets=10, connect=_connect_factory(conn))
    assert res["total_buckets"] == 60
    assert res["truncated"] is True
    assert len(res["buckets"]) == 10
    assert res["buckets"][-1]["mean"] == 59
    assert res["first"]["mean"] == 0     # first/last from the FULL series
    assert res["last"]["mean"] == 59


@pytest.mark.asyncio
async def test_statistics_empty_has_note():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"}, _result_frame("sensor.x", [])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.get_entity_statistics(
        _settings(), "sensor.x", connect=_connect_factory(conn))
    assert res["total_buckets"] == 0
    assert res["buckets"] == []
    assert "note" in res


@pytest.mark.asyncio
async def test_statistics_auth_invalid_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_invalid"}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAWsError):
        await ha_ws.get_entity_statistics(
            _settings(), "sensor.x", connect=_connect_factory(conn))
    assert conn.closed is True


@pytest.mark.asyncio
async def test_statistics_skips_non_result_frames():
    # an interleaved event frame before the result must be skipped, not break parsing
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 7, "type": "event", "event": {}},
              _result_frame("sensor.x", [_bucket(1700000000000, 1.0, 0.0, 2.0)])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.get_entity_statistics(
        _settings(), "sensor.x", connect=_connect_factory(conn))
    assert res["total_buckets"] == 1
    assert res["buckets"][0]["mean"] == 1.0
