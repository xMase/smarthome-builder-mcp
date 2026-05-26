import asyncio
import collections

import pytest

from nrha_mcp import runtime


class FakeComms:
    """In-memory /comms connection. Yields queued frames, then blocks."""

    def __init__(self, frames):
        self._frames = collections.deque(frames)
        self.sent = []

    async def send(self, obj):
        self.sent.append(obj)

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(3600)  # block until the capture window cancels us

    async def close(self):
        pass


def _connect_factory(comms):
    async def _connect(client):
        return comms
    return _connect


def _debug_frame(node_id="d1", z="tab1", name="dbg", msg="hi"):
    return {"topic": "debug",
            "data": {"id": node_id, "z": z, "name": name, "msg": msg, "format": "string"}}


def _notification_frame(subtopic="runtime-deploy", data=None):
    return {"topic": f"notification/{subtopic}", "data": data if data is not None else {"revision": "abc"}}


@pytest.mark.asyncio
async def test_capture_collects_debug(fake_client):
    comms = FakeComms([_debug_frame(msg="rain")])
    res = await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert res["count"] == 1
    assert res["captured"][0]["msg"] == "rain"
    assert {"subscribe": "debug"} in comms.sent
    assert {"subscribe": "status/#"} in comms.sent


@pytest.mark.asyncio
async def test_capture_filters_by_node_id(fake_client):
    comms = FakeComms([_debug_frame(node_id="d1", msg="a"),
                       _debug_frame(node_id="d2", msg="b")])
    res = await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=2, node_id="d2",
        connect=_connect_factory(comms))
    assert res["count"] == 1
    assert res["captured"][0]["node_id"] == "d2"


@pytest.mark.asyncio
async def test_capture_inject_after_subscribe(fake_client):
    comms = FakeComms([])
    res = await runtime.capture_debug(
        fake_client, window_ms=50, inject_node_id="inj1",
        connect=_connect_factory(comms))
    assert res["injected"] == "inj1"
    assert fake_client.injected == ["inj1"]
    assert {"subscribe": "debug"} in comms.sent  # subscribed before inject, by construction


@pytest.mark.asyncio
async def test_capture_empty_has_hint(fake_client):
    comms = FakeComms([])
    res = await runtime.capture_debug(
        fake_client, window_ms=50, connect=_connect_factory(comms))
    assert res["count"] == 0
    assert "hint" in res


@pytest.mark.asyncio
async def test_capture_sends_auth_when_token(fake_client):
    fake_client.ws_token = "tok123"
    comms = FakeComms([_debug_frame()])
    await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert {"auth": "tok123"} in comms.sent


@pytest.mark.asyncio
async def test_capture_no_auth_when_open(fake_client):
    comms = FakeComms([_debug_frame()])
    await runtime.capture_debug(
        fake_client, window_ms=500, max_messages=1, connect=_connect_factory(comms))
    assert not any("auth" in m for m in comms.sent)


@pytest.mark.asyncio
async def test_capture_notification(fake_client):
    comms = FakeComms([_notification_frame("runtime-deploy", {"revision": "r1"})])
    res = await runtime.capture_debug(
        fake_client, topics=["notification"], window_ms=500, max_messages=1,
        connect=_connect_factory(comms))
    assert res["count"] == 1
    ev = res["captured"][0]
    assert ev["topic"] == "notification"
    assert ev["subtopic"] == "runtime-deploy"
    assert ev["data"] == {"revision": "r1"}
    assert {"subscribe": "notification/#"} in comms.sent


@pytest.mark.asyncio
async def test_notification_dropped_by_node_filter(fake_client):
    comms = FakeComms([_notification_frame()])
    res = await runtime.capture_debug(
        fake_client, topics=["notification"], window_ms=50, node_id="someNode",
        connect=_connect_factory(comms))
    assert res["count"] == 0
