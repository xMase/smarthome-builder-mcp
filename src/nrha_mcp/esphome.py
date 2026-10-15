"""ESPHome Device Builder over Home Assistant ingress (esphome_* tools).

Each call opens the HA websocket (auth with HA_TOKEN), reads the add-on's
ingress_url, asks for an ingress session, then opens a second websocket to the
Device Builder's /ws through the ingress proxy with the ingress_session cookie.
Both transports sit behind injectable connect callables so tests need no network.
"""
import asyncio
import collections
import contextlib
import difflib
import hashlib
import itertools
import re
from datetime import datetime, timezone
from pathlib import Path

from . import ha_ws
from .client import NRAuthError, NRConflictError, NRError, NRNotFoundError


class ESPHomeError(NRError):
    """Device Builder / ingress failure (an expected, surfaced error)."""


_NAME_RE = re.compile(r"[^/\\.\x00][^/\\\x00]*\.ya?ml")
_SECRETS = {"secrets.yaml", "secrets.yml"}


def check_configuration(name, settings) -> str:
    """Bare *.yaml/*.yml name, no '/', '\\' or leading '.'; secrets blocked unless allowed."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ESPHomeError(
            f"invalid configuration name {name!r}: use a bare file name ending in .yaml "
            "or .yml, without '/' and without a leading '.'")
    if name.lower() in _SECRETS and not settings.esphome_allow_secrets:
        raise ESPHomeError(f"{name} is blocked; set ESPHOME_ALLOW_SECRETS=true to allow it.")
    return name


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_BACKUP_RE = re.compile(r"(?P<cfg>[^/\\.\x00][^/\\\x00]*\.ya?ml)\.(?P<ts>\d{8}T\d{12}Z)\.yaml")


class YamlBackupStore:
    """Text backups of ESPHome files: BACKUP_DIR/esphome/<cfg>.<UTC ts>.yaml, bytes as read."""

    def __init__(self, backup_dir: str):
        self.dir = Path(backup_dir) / "esphome"

    def snapshot(self, configuration: str, content: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        while True:  # exclusive create: two snapshots in the same microsecond never collide
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.dir / f"{configuration}.{ts}.yaml"
            try:
                with open(path, "xb") as f:
                    f.write(content.encode("utf-8"))
                return path.name
            except FileExistsError:
                continue

    def list(self, configuration: str | None = None) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.iterdir():
            m = _BACKUP_RE.fullmatch(p.name)
            if m and configuration in (None, m["cfg"]):
                out.append({"backup_id": p.name, "configuration": m["cfg"],
                            "timestamp": m["ts"], "size": p.stat().st_size})
        return sorted(out, key=lambda b: b["timestamp"], reverse=True)

    def read(self, backup_id: str) -> tuple[str, str]:
        m = _BACKUP_RE.fullmatch(backup_id or "")
        if not m:
            raise ESPHomeError(f"invalid backup_id {backup_id!r}; use an id from esphome_list_backups.")
        path = self.dir / backup_id
        if not path.is_file():
            raise NRNotFoundError(f"backup {backup_id} not found in {self.dir}")
        return m["cfg"], path.read_bytes().decode("utf-8")


# --- Transport: HA ingress handshake + Device Builder /ws ------------------

CONNECT_TIMEOUT = 15.0     # HA handshake + Device Builder websocket open
REPLY_TIMEOUT = 30.0       # one request/reply on /ws
VALIDATE_TIMEOUT = 180.0
LOGS_MAX_SECONDS = 60
JOB_MAX_WAIT = 300
REPLAY_SECONDS = 2.0       # follow_job has no end-of-replay marker
STREAM_TAIL = 200
_WS_MAX_SIZE = 16 * 2**20  # HA ingress proxy limit


def _db_ws_url(ha_url: str, ingress_url: str) -> str:
    base = ha_ws._ws_url(ha_url)[: -len("/api/websocket")]
    return f"{base}/{ingress_url.strip('/')}/ws"


async def _ha_result(conn, msg_id: int, msg: dict) -> dict:
    await conn.send({"id": msg_id, **msg})
    while True:
        reply = await conn.recv()
        if reply.get("id") == msg_id and reply.get("type") == "result":
            return reply


async def _open_ingress(settings, ha_connect=None) -> tuple[str, str]:
    """HA websocket: add-on ingress_url + a fresh ingress session id."""
    slug = settings.esphome_addon_slug
    conn = await (ha_connect or ha_ws._default_connect)(settings)
    try:
        await ha_ws._auth(conn, settings.ha_token)
        info = await _ha_result(conn, 1, {"type": "supervisor/api",
                                          "endpoint": f"/addons/{slug}/info", "method": "get"})
        if not info.get("success"):
            if (info.get("error") or {}).get("code") == "unauthorized":
                raise NRAuthError("HA refused supervisor/api (unauthorized): "
                                  "the HA token must belong to an admin user")
            raise NRNotFoundError(
                f"ESPHome add-on {slug!r} not found: {info.get('error')} (set ESPHOME_ADDON_SLUG)")
        data = info.get("result") or {}
        if data.get("state") != "started" or not data.get("ingress_url"):
            raise NRNotFoundError(
                f"ESPHome add-on {slug!r} is not usable: state={data.get('state')}, "
                f"ingress_url={data.get('ingress_url')!r}")
        sess = await _ha_result(conn, 2, {"type": "supervisor/api",
                                          "endpoint": "/ingress/session", "method": "post"})
        session_id = (sess.get("result") or {}).get("session") if sess.get("success") else None
        if not session_id:
            raise NRAuthError(f"HA refused an ingress session: {sess.get('error')} "
                              "(the HA token must belong to an admin user)")
        return data["ingress_url"], session_id
    finally:
        await conn.close()


async def _default_db_connect(url: str, cookie: str):
    import websockets  # lazy import so unit tests need no network lib

    try:
        ws = await websockets.connect(url, additional_headers={"Cookie": cookie},
                                      max_size=_WS_MAX_SIZE, open_timeout=CONNECT_TIMEOUT)
    except websockets.exceptions.InvalidStatus as e:
        status = e.response.status_code
        if status in (401, 403):
            raise NRAuthError(f"HA ingress refused the Device Builder websocket (HTTP {status}); "
                              "the HA token must belong to an admin user") from None
        raise ESPHomeError(f"HA ingress refused the Device Builder websocket (HTTP {status})") from None
    except (OSError, asyncio.TimeoutError) as e:
        raise ESPHomeError(f"cannot reach Device Builder through HA ingress: {e}") from None
    return ha_ws._HAWsConn(ws)


_ERROR_CLASSES = {"not_found": NRNotFoundError, "precondition_failed": NRConflictError,
                  "not_authenticated": NRAuthError}


def _db_error(command: str, msg: dict) -> NRError:
    code = msg.get("error_code")
    return _ERROR_CLASSES.get(code, ESPHomeError)(f"{command} failed: {code}: {msg.get('details')}")


class _DB:
    """Device Builder /ws protocol on one connection: string message ids, replies matched by id."""

    def __init__(self, conn):
        self._conn = conn
        self._ids = itertools.count(1)
        self.server_info: dict = {}

    async def _send(self, command: str, args: dict) -> str:
        mid = str(next(self._ids))
        await self._conn.send({"command": command, "message_id": mid, "args": args})
        return mid

    async def _recv(self, timeout: float) -> dict:
        return await asyncio.wait_for(self._conn.recv(), timeout)

    async def call(self, command: str, **args):
        mid = await self._send(command, args)
        while True:
            try:
                msg = await self._recv(REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                raise ESPHomeError(f"{command}: no reply from Device Builder in {REPLY_TIMEOUT}s") from None
            if msg.get("message_id") != mid:
                continue
            if "error_code" in msg:
                raise _db_error(command, msg)
            if "result" in msg:
                return msg["result"]

    async def stream(self, command: str, args: dict, *, seconds: float,
                     max_lines: int | None = None, tail: int | None = None):
        """Collect `output` lines until the terminal `result` event, `max_lines` or the
        deadline; a stream stopped early gets devices/stop_stream. Returns (lines, data|None)."""
        mid = await self._send(command, args)
        lines: collections.deque = collections.deque(maxlen=tail)
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while (left := end - loop.time()) > 0:
            try:
                msg = await self._recv(left)
            except asyncio.TimeoutError:
                break
            if msg.get("message_id") != mid:
                continue
            if "error_code" in msg:
                raise _db_error(command, msg)
            if msg.get("event") == "output":
                lines.append(msg.get("data"))
                if max_lines and len(lines) >= max_lines:
                    break
            elif msg.get("event") == "result":
                return list(lines), msg.get("data") or {}
            elif "result" in msg:
                return list(lines), None
        await self.call("devices/stop_stream", stream_id=mid)
        return list(lines), None


@contextlib.asynccontextmanager
async def _session(settings, *, ha_connect=None, db_connect=None):
    """One Device Builder connection through HA ingress, closed on exit."""
    try:
        ingress_url, session_id = await asyncio.wait_for(
            _open_ingress(settings, ha_connect), CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        raise ESPHomeError(f"timed out after {CONNECT_TIMEOUT}s opening the HA ingress session") from None
    conn = await (db_connect or _default_db_connect)(
        _db_ws_url(settings.ha_url, ingress_url), f"ingress_session={session_id}")
    db = _DB(conn)
    try:
        try:
            db.server_info = await db._recv(REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            raise ESPHomeError("Device Builder sent no greeting; is the ESPHome add-on running?") from None
        yield db
    finally:
        await conn.close()
