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


def _entry(domain, entry_id, title, state):
    return {"domain": domain, "entry_id": entry_id, "title": title, "state": state}


def _entries_result(entries):
    return {"id": 1, "type": "result", "success": True, "result": entries}


@pytest.mark.asyncio
async def test_nodered_present_and_loaded():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _entries_result([_entry("sun", "s1", "Sun", "loaded"),
                               _entry("nodered", "n1", "Node-RED", "loaded")])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert res["installed"] is True
    assert res["entries"] == [{"entry_id": "n1", "title": "Node-RED", "state": "loaded"}]
    assert "loaded" in res["note"].lower()
    assert {"id": 1, "type": "config_entries/get"} in conn.sent
    assert conn.closed is True


@pytest.mark.asyncio
async def test_nodered_present_not_loaded():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _entries_result([_entry("nodered", "n1", "Node-RED", "setup_error")])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert res["installed"] is True
    assert res["entries"][0]["state"] == "setup_error"
    assert "not loaded" in res["note"].lower()


@pytest.mark.asyncio
async def test_nodered_absent():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              _entries_result([_entry("sun", "s1", "Sun", "loaded")])]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert res["installed"] is False
    assert res["entries"] == []
    assert "not set up" in res["note"].lower()


@pytest.mark.asyncio
async def test_nodered_unauthorized_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": False, "error": {"code": "unauthorized"}}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAWsError, match="admin"):
        await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert conn.closed is True


@pytest.mark.asyncio
async def test_nodered_generic_failure_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": False, "error": {"code": "unknown_error"}}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAWsError, match="config_entries/get failed"):
        await ha_ws.check_nodered_integration(_settings(), connect=_connect_factory(conn))
    assert conn.closed is True


def test_settings_have_ws_policy_defaults():
    s = Settings(ha_url="http://ha:8123", ha_token="tok")
    assert s.ha_ws_block_commands == ""
    assert s.ha_ws_read_commands is None


@pytest.mark.asyncio
async def test_run_command_read_executes_without_confirm():
    # a "/list" command is a read -> runs immediately, no confirm needed
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": True,
               "result": [{"entity_id": "button.x"}]}]
    conn = FakeHAWsConn(frames)
    res = await ha_ws.run_command(
        _settings(), "config/entity_registry/list", connect=_connect_factory(conn))
    assert res["ok"] is True
    assert res["command"] == "config/entity_registry/list"
    assert res["result"] == [{"entity_id": "button.x"}]
    assert {"id": 1, "type": "config/entity_registry/list"} in conn.sent
    assert conn.closed is True


@pytest.mark.asyncio
async def test_run_command_read_failure_raises():
    frames = [{"type": "auth_required"}, {"type": "auth_ok"},
              {"id": 1, "type": "result", "success": False, "error": {"code": "boom"}}]
    conn = FakeHAWsConn(frames)
    with pytest.raises(HAWsError, match="failed"):
        await ha_ws.run_command(
            _settings(), "config/entity_registry/list", connect=_connect_factory(conn))
    assert conn.closed is True
