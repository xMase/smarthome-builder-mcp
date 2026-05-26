"""Home Assistant Long-Term Statistics over the HA WebSocket API.

LTS is not on the REST API; it is served by `recorder/statistics_during_period`
over ws(s)://<ha>/api/websocket. The transport sits behind an injectable `connect`
so tests feed frames without a live server (mirrors runtime.py).
"""
import json
from datetime import datetime, timedelta, timezone

from .client import NRError


class HAStatsError(NRError):
    """HA WebSocket auth or statistics-command failure (an expected, surfaced error)."""


def _ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https"):
        base = "wss" + base[len("https"):]
    elif base.startswith("http"):
        base = "ws" + base[len("http"):]
    return base + "/api/websocket"


def _ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


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


async def _query_statistics(conn, token, *, statistic_id, start_iso, period, types) -> dict:
    await conn.recv()  # auth_required greeting
    await conn.send({"type": "auth", "access_token": token})
    auth = await conn.recv()
    if auth.get("type") != "auth_ok":
        raise HAStatsError(f"HA WebSocket auth failed: {auth.get('type')}")
    await conn.send({
        "id": 1,
        "type": "recorder/statistics_during_period",
        "start_time": start_iso,
        "statistic_ids": [statistic_id],
        "period": period,
        "types": list(types),
    })
    while True:
        msg = await conn.recv()
        if msg.get("id") == 1 and msg.get("type") == "result":
            if not msg.get("success"):
                raise HAStatsError(f"statistics query failed: {msg.get('error')}")
            return msg.get("result", {})


async def get_entity_statistics(settings, entity_id, *, days: int = 30, period: str = "day",
                                max_buckets: int = 200, connect=None) -> dict:
    """Long-term statistics (mean/min/max buckets) for one entity over the last `days`."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    conn = await (connect or _default_connect)(settings)
    try:
        result = await _query_statistics(
            conn, settings.ha_token, statistic_id=entity_id,
            start_iso=start.isoformat(), period=period, types=("mean", "min", "max"))
    finally:
        await conn.close()
    series = result.get(entity_id, []) or []
    buckets = [{"start": _ms_to_iso(b.get("start")), "mean": b.get("mean"),
                "min": b.get("min"), "max": b.get("max")} for b in series]
    total = len(buckets)
    kept = buckets[-max_buckets:] if total > max_buckets else buckets
    out = {
        "entity_id": entity_id,
        "period": period,
        "days": days,
        "total_buckets": total,
        "buckets": kept,
        "first": buckets[0] if buckets else None,
        "last": buckets[-1] if buckets else None,
    }
    if total > max_buckets:
        out["truncated"] = True
        out["truncated_note"] = f"Showing the most recent {max_buckets} of {total} buckets."
    if not buckets:
        out["note"] = ("No long-term statistics for this entity in the window — it may "
                       "lack a numeric state_class, or have no data in the range.")
    return out
