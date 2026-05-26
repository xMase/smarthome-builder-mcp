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
    headers = {}
    auth = client._client.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth  # covers a reverse-proxy auth case
    ws = await websockets.connect(url, additional_headers=headers or None)
    return _WebsocketsConn(ws)


def _normalize(frame: dict) -> dict | None:
    """Map a /comms frame to our message dict, or None if it is not debug/status."""
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
            await conn.send({"subscribe": "status/#" if t == "status" else t})
        await asyncio.sleep(0.1)  # let the subscription register before triggering
        if inject_node_id:
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
            msg = _normalize(frame)
            if msg is None:
                continue
            if node_id and msg["node_id"] != node_id:
                continue
            if flow_id and msg["flow_id"] != flow_id:
                continue
            captured.append(msg)
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
