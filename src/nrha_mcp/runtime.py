"""Capture live Node-RED runtime messages over the /comms WebSocket.

Debug-node output and node status are pushed over /comms, not stored in flow
context, so this is the only way to observe them. The transport sits behind an
injectable `connect` callable so tests can feed frames without a live server.
"""
import asyncio
import json
import time
from typing import Awaitable, Callable

from .client import NRClient

_EMPTY_HINT = (
    "No messages captured. Check: the debug node is active and 'send to sidebar' "
    "is enabled; window_ms was long enough; the inject actually produced output "
    "(a function may have filtered it); node_id/flow_id filters are correct."
)

# Topic shorthands -> the actual /comms subscription string.
_SUBSCRIBE_TOPIC = {"status": "status/#", "notification": "notification/#"}


class _WebsocketsConn:
    """Adapter over a `websockets` client connection."""

    def __init__(self, ws):
        self._ws = ws

    async def send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    async def recv(self) -> dict:
        raw = await self._ws.recv()
        return json.loads(raw)

    async def close(self) -> None:
        await self._ws.close()


async def _connect_ws(client: NRClient):
    """Default connector: open the real /comms socket via the websockets lib."""
    import websockets  # lazy import so unit tests need no network lib

    url = client.comms_ws_url()
    auth = await client.ws_auth_header()
    headers = {"Authorization": auth} if auth else None
    ws = await websockets.connect(url, additional_headers=headers)
    return _WebsocketsConn(ws)


def _normalize(frame: dict) -> dict | None:
    """Map a /comms frame to our message dict, or None if it is not a debug/status/notification frame."""
    if not isinstance(frame, dict):
        return None
    topic = frame.get("topic", "")
    data = frame.get("data", {}) or {}
    if topic == "debug":
        return {
            "topic": "debug",
            "node_id": data.get("id"),
            "node_name": data.get("name"),
            "flow_id": data.get("z"),
            "msg": data.get("msg"),
            "format": data.get("format"),
            "ts": time.time(),
        }
    if topic.startswith("status/"):
        status = data.get("status", data) or {}
        return {
            "topic": "status",
            "node_id": topic.split("/", 1)[1],
            "node_name": None,
            "flow_id": data.get("z"),
            "text": status.get("text"),
            "ts": time.time(),
        }
    if topic.startswith("notification/"):
        return {
            "topic": "notification",
            "subtopic": topic.split("/", 1)[1],
            "node_id": None,
            "flow_id": None,
            "data": data,
            "ts": time.time(),
        }
    return None


async def capture_debug(
    client: NRClient,
    *,
    window_ms: int = 2000,
    inject_node_id: str | None = None,
    topics: list[str] | None = None,
    node_id: str | None = None,
    flow_id: str | None = None,
    max_messages: int = 100,
    connect: Callable[[NRClient], Awaitable] | None = None,
) -> dict:
    """Subscribe to /comms, optionally inject, and collect messages for a window."""
    topics = topics or ["debug", "status"]
    conn = await (connect or _connect_ws)(client)
    captured: list[dict] = []
    injected: str | None = None
    try:
        token = await client.get_ws_token()
        if token:
            await conn.send({"auth": token})
        for t in topics:
            await conn.send({"subscribe": _SUBSCRIBE_TOPIC.get(t, t)})
        if inject_node_id:
            await asyncio.sleep(0.1)  # let the subscription register before we trigger the inject
            await client.inject(inject_node_id)
            injected = inject_node_id
        deadline = time.monotonic() + window_ms / 1000
        while len(captured) < max_messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(conn.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            for item in (frame if isinstance(frame, list) else [frame]):
                msg = _normalize(item)
                if msg is None:
                    continue
                if node_id and msg["node_id"] != node_id:
                    continue
                if flow_id and msg["flow_id"] != flow_id:
                    continue
                captured.append(msg)
                if len(captured) >= max_messages:
                    break
    finally:
        await conn.close()
    result = {
        "captured": captured,
        "count": len(captured),
        "window_ms": window_ms,
        "injected": injected,
    }
    if not captured:
        result["hint"] = _EMPTY_HINT
    return result


async def get_runtime_events(
    client: NRClient,
    *,
    window_ms: int = 2000,
    max_messages: int = 100,
    connect: Callable[[NRClient], Awaitable] | None = None,
) -> dict:
    """Capture only the /comms notification channel (runtime/system events)."""
    res = await capture_debug(
        client, topics=["notification"], window_ms=window_ms,
        max_messages=max_messages, connect=connect)
    events = res["captured"]
    out = {"events": events, "count": len(events), "window_ms": window_ms}
    if not events:
        out["hint"] = (
            "No runtime events in the window. That is normal when nothing happened "
            "(no deploy, no state change, no module/node error). Trigger the action "
            "during the window, or widen window_ms."
        )
    return out
