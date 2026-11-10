"""Home Assistant WebSocket transport (ws(s)://<ha>/api/websocket) used by esphome.py.

The transport sits behind an injectable `connect` so tests feed frames without a live
server (mirrors runtime.py).
"""
import json

from .client import NRError


class HAWsError(NRError):
    """HA WebSocket auth or command failure (an expected, surfaced error)."""


def _ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https"):
        base = "wss" + base[len("https"):]
    elif base.startswith("http"):
        base = "ws" + base[len("http"):]
    return base + "/api/websocket"


class _HAWsConn:
    """Adapter over a `websockets` connection (json on the wire)."""

    def __init__(self, ws):
        self._ws = ws

    async def send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    async def recv(self) -> dict:
        return json.loads(await self._ws.recv())

    async def close(self) -> None:
        await self._ws.close()


async def _default_connect(settings):
    import websockets  # lazy import so unit tests need no network lib

    ws = await websockets.connect(_ws_url(settings.ha_url))
    return _HAWsConn(ws)


async def _auth(conn, token) -> None:
    await conn.recv()  # auth_required greeting
    await conn.send({"type": "auth", "access_token": token})
    auth = await conn.recv()
    if auth.get("type") != "auth_ok":
        raise HAWsError(f"HA WebSocket auth failed: {auth.get('type')}")
