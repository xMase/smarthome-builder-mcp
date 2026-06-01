"""Home Assistant queries over the WebSocket API at ws(s)://<ha>/api/websocket.

Two consumers live here: long-term statistics (recorder/statistics_during_period,
which is not on the REST API) and Node-RED Companion integration detection
(config_entries/get). The transport sits behind an injectable `connect` so tests feed
frames without a live server (mirrors runtime.py).
"""
import json
from datetime import datetime, timedelta, timezone

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


async def _auth(conn, token) -> None:
    await conn.recv()  # auth_required greeting
    await conn.send({"type": "auth", "access_token": token})
    auth = await conn.recv()
    if auth.get("type") != "auth_ok":
        raise HAWsError(f"HA WebSocket auth failed: {auth.get('type')}")


async def _query_statistics(conn, token, *, statistic_id, start_iso, period, types) -> dict:
    await _auth(conn, token)
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
                raise HAWsError(f"statistics query failed: {msg.get('error')}")
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


async def check_nodered_integration(settings, *, connect=None) -> dict:
    """Whether the 'Node-RED Companion' integration (config-entry domain 'nodered')
    is set up in Home Assistant. Read-only; requires an admin HA token."""
    conn = await (connect or _default_connect)(settings)
    try:
        await _auth(conn, settings.ha_token)
        await conn.send({"id": 1, "type": "config_entries/get"})
        while True:
            msg = await conn.recv()
            if msg.get("id") == 1 and msg.get("type") == "result":
                if not msg.get("success"):
                    err = msg.get("error") or {}
                    if err.get("code") == "unauthorized":
                        raise HAWsError(
                            "config_entries/get was refused (unauthorized): the HA token "
                            "likely lacks admin rights, which this check requires.")
                    raise HAWsError(f"config_entries/get failed: {err}")
                entries = msg.get("result") or []
                break
    finally:
        await conn.close()

    nodered = [e for e in entries if e.get("domain") == "nodered"]
    out_entries = [{"entry_id": e.get("entry_id"), "title": e.get("title"),
                    "state": e.get("state")} for e in nodered]
    installed = bool(nodered)
    loaded = any(e.get("state") == "loaded" for e in nodered)
    if not installed:
        note = ("The 'Node-RED Companion' integration is not set up in Home Assistant "
                "(Settings -> Devices & Services). It is a manual HA step - no tool can "
                "create it - and it must be present before ha-switch/ha-button/ha-sensor "
                "nodes can publish entities to HA.")
    elif not loaded:
        note = ("Integration present but not loaded (state is not 'loaded'); entities "
                "from Node-RED won't appear until it loads. Check it in HA.")
    else:
        note = "Integration present and loaded; Node-RED can publish entities to HA."
    return {"installed": installed, "entries": out_entries, "note": note}


# --- Generic WS command (ha_ws_command) ----------------------------------

# Commands recognized as read-only (execute without confirm). Anything not here
# and not ending in /list or /get is treated as a write (preview -> confirm).
READ_COMMANDS = {
    "get_states", "get_config", "get_services",
    "render_template", "recorder/statistics_during_period",
}


def _split_csv(raw: str) -> set[str]:
    return {t.strip() for t in (raw or "").split(",") if t.strip()}


def _read_commands(settings) -> set[str]:
    return READ_COMMANDS | _split_csv(settings.ha_ws_read_commands or "")


def _is_read(command: str, settings) -> bool:
    if command.endswith("/list") or command.endswith("/get"):
        return True
    return command in _read_commands(settings)


def _ws_block_set(settings) -> set[str]:
    return _split_csv(settings.ha_ws_block_commands)


def _preview(command: str, params: dict) -> dict:
    return {
        "preview": True,
        "would_send": {"type": command, **params},
        "requires_confirm": True,
        "hint": "re-call with confirm=true to execute.",
    }


async def _execute(settings, command: str, params: dict, connect) -> dict:
    conn = await (connect or _default_connect)(settings)
    try:
        await _auth(conn, settings.ha_token)
        await conn.send({"id": 1, "type": command, **params})
        while True:
            msg = await conn.recv()
            if msg.get("id") == 1 and msg.get("type") == "result":
                if not msg.get("success"):
                    raise HAWsError(f"{command} failed: {msg.get('error')}")
                return {"ok": True, "command": command, "result": msg.get("result")}
    finally:
        await conn.close()


def effective_policy(settings) -> dict:
    """Resolved WS-command policy for observability (nr_diagnostics)."""
    return {
        "read_commands": sorted(_read_commands(settings)),
        "blocked_commands": sorted(_ws_block_set(settings)),
    }


async def run_command(settings, command: str, params: dict | None = None,
                      *, confirm: bool = False, connect=None) -> dict:
    """Send an arbitrary HA WebSocket command. Reads run immediately; writes return a
    preview unless confirm=true. call_service obeys the shared catastrophic floor."""
    params = dict(params or {})
    # call_service over WS must obey the SAME floor as ha_call_service (no bypass).
    if command == "call_service":
        from .ha import _service_tier  # reuse CATASTROPHIC_FLOOR / DEFAULT_CONFIRM
        domain, service = params.get("domain"), params.get("service")
        if not domain or not service:
            raise HAWsError("call_service requires 'domain' and 'service' in params.")
        tier = _service_tier(domain, service, settings)
        if tier == "blocked":
            raise HAWsError(
                f"{domain}.{service} is in the catastrophic block list and cannot be "
                "executed; confirm=true does not override it.")
        if tier == "confirm" and not confirm:
            return _preview(command, params)
        return await _execute(settings, command, params, connect)
    if command in _ws_block_set(settings):
        raise HAWsError(
            f"{command} is in HA_WS_BLOCK_COMMANDS and cannot be executed; "
            "confirm=true does not override it.")
    if _is_read(command, settings):
        return await _execute(settings, command, params, connect)
    if not confirm:
        return _preview(command, params)
    return await _execute(settings, command, params, connect)
