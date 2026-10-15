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
    try:
        await conn.send({"id": msg_id, **msg})
    except Exception as e:
        raise ESPHomeError(f"HA websocket connection lost: {type(e).__name__}: {e}") from None
    while True:
        try:
            reply = await conn.recv()
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            raise ESPHomeError(f"HA websocket connection lost: {type(e).__name__}: {e}") from None
        if reply.get("id") == msg_id and reply.get("type") == "result":
            return reply


async def _open_ingress(settings, ha_connect=None) -> tuple[str, str]:
    """HA websocket: add-on ingress_url + a fresh ingress session id."""
    slug = settings.esphome_addon_slug
    try:
        conn = await (ha_connect or ha_ws._default_connect)(settings)
    except NRError:
        raise
    except Exception as e:
        raise ESPHomeError(f"cannot reach the HA websocket: {type(e).__name__}: {e}") from None
    try:
        try:
            await ha_ws._auth(conn, settings.ha_token)
        except NRError:
            raise
        except Exception as e:
            raise ESPHomeError(f"HA websocket connection lost: {type(e).__name__}: {e}") from None
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
    except websockets.exceptions.InvalidHandshake as e:
        raise ESPHomeError(f"Device Builder websocket handshake failed: {type(e).__name__}: {e}") from None
    except (OSError, asyncio.TimeoutError) as e:
        raise ESPHomeError(f"cannot reach Device Builder through HA ingress: {e}") from None
    return ha_ws._HAWsConn(ws)


_ERROR_CLASSES = {"not_found": NRNotFoundError, "precondition_failed": NRConflictError,
                  "not_authenticated": NRAuthError}


def _db_error(command: str, msg: dict) -> NRError:
    code = msg.get("error_code")
    return _ERROR_CLASSES.get(code, ESPHomeError)(f"{command} failed: {code}: {msg.get('details')}")


# Color codes in output lines, real (ESC) or escaped as text by esphome --dashboard ("\033[32m").
_ANSI_RE = re.compile(r"(?:\x1b|\\033)\[[0-9;]*m")


class _DB:
    """Device Builder /ws protocol on one connection: string message ids, replies matched by id."""

    def __init__(self, conn):
        self._conn = conn
        self._ids = itertools.count(1)
        self.server_info: dict = {}

    async def _send(self, command: str, args: dict) -> str:
        mid = str(next(self._ids))
        try:
            await self._conn.send({"command": command, "message_id": mid, "args": args})
        except Exception as e:
            raise ESPHomeError(f"Device Builder connection lost: {type(e).__name__}: {e}") from None
        return mid

    async def _recv(self, timeout: float) -> dict:
        try:
            return await asyncio.wait_for(self._conn.recv(), timeout)
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            raise ESPHomeError(f"Device Builder connection lost: {type(e).__name__}: {e}") from None

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
                lines.append(_ANSI_RE.sub("", msg.get("data") or ""))
                if max_lines and len(lines) >= max_lines:
                    break
            elif msg.get("event") == "result":
                return list(lines), msg.get("data") or {}
            elif "result" in msg:
                return list(lines), None
        try:
            await self.call("devices/stop_stream", stream_id=mid)
        except NRError:
            pass  # the lines already collected matter more than a failed stop
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


# --- Tools: read and check --------------------------------------------------

_DEVICE_FIELDS = ("update_available", "has_pending_changes")
_RUNTIME_FIELDS = ("state", "ip_addresses", "deployed_version", "queued_update")


def _flat_device(d: dict) -> dict:
    # shortcut: shapes read from Device Builder 1.14.9 source, not observed live; Task 7 confirms.
    rs = d.get("runtime_state") or {}
    out = {"configuration": d.get("configuration"), "name": d.get("name"),
           "address": d.get("address"), "current_version": d.get("current_version")}
    for k in _RUNTIME_FIELDS:
        out[k] = rs.get(k, d.get(k))
    for k in _DEVICE_FIELDS:
        out[k] = d.get(k, rs.get(k))
    return out


def _content(res) -> str:
    return res if isinstance(res, str) else (res or {}).get("content", "")


async def list_devices(settings, *, session=None) -> dict:
    async with (session or _session)(settings) as db:
        res = await db.call("devices/list")
        version = db.server_info.get("server_version")
    return {"server_version": version,
            "devices": [_flat_device(d) for d in (res or {}).get("configured", [])]}


async def get_config(settings, configuration, *, session=None) -> dict:
    check_configuration(configuration, settings)
    async with (session or _session)(settings) as db:
        content = _content(await db.call("devices/get_config", configuration=configuration))
    return {"configuration": configuration, "content": content, "sha256": _sha(content)}


async def validate(settings, configuration, *, session=None) -> dict:
    check_configuration(configuration, settings)
    async with (session or _session)(settings) as db:
        lines, final = await db.stream("devices/validate", {"configuration": configuration},
                                       seconds=VALIDATE_TIMEOUT, tail=STREAM_TAIL)
    if final is None:
        raise ESPHomeError(f"validate of {configuration} did not finish in {VALIDATE_TIMEOUT}s")
    return {"configuration": configuration, "success": bool(final.get("success")),
            "code": final.get("code"), "output_tail": lines}


async def logs(settings, configuration, *, seconds=10, max_lines=200, session=None) -> dict:
    check_configuration(configuration, settings)
    secs = min(max(float(seconds), 0.05), LOGS_MAX_SECONDS)
    limit = min(max(int(max_lines), 1), 2000)
    async with (session or _session)(settings) as db:
        lines, final = await db.stream("devices/logs", {"configuration": configuration},
                                       seconds=secs, max_lines=limit)
    out = {"configuration": configuration, "seconds": secs, "lines": lines, "ended": final is not None}
    if final is not None:
        out["exit_code"] = final.get("code")
    return out


# --- Tools: write path -------------------------------------------------------

def _diff(old: str, new: str, name: str) -> str:
    text = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                          f"a/{name}", f"b/{name}", lineterm=""))
    return text or "(only line endings or the final newline differ)"


async def _read_or_none(db, configuration: str) -> str | None:
    try:
        return _content(await db.call("devices/get_config", configuration=configuration))
    except NRNotFoundError:
        return None


async def update_config(settings, configuration, content, *, base_sha256=None, confirm=False,
                        session=None, backups=None) -> dict:
    """Preview (diff) → confirm per DEPLOY_MODE; backup, re-read check, then write."""
    check_configuration(configuration, settings)
    if not isinstance(content, str) or not content.strip():
        raise ESPHomeError("content is empty; Device Builder refuses an empty device YAML.")
    store = backups or YamlBackupStore(settings.backup_dir)
    async with (session or _session)(settings) as db:
        current = await _read_or_none(db, configuration)
        cur_sha = _sha(current) if current is not None else None
        if base_sha256 is not None and base_sha256 != cur_sha:
            raise NRConflictError(f"{configuration} changed since it was read (sha256 now {cur_sha}); "
                                  "re-read it and preview again.")
        if current == content:
            return {"configuration": configuration, "changed": False}
        if settings.deploy_mode == "propose" and not confirm:
            return {"preview": True, "configuration": configuration, "new_file": current is None,
                    "diff": _diff(current or "", content, configuration), "base_sha256": cur_sha,
                    "requires_confirm": True,
                    "hint": "re-call with confirm=true and this base_sha256 to write."}
        backup_id = store.snapshot(configuration, current) if current is not None else None
        if await _read_or_none(db, configuration) != current:
            raise NRConflictError(f"{configuration} changed while writing; nothing written "
                                  f"{f'(backup {backup_id} kept); ' if backup_id else ''}"
                                  "re-read it and preview again.")
        args = {"configuration": configuration, "content": content}
        if current is not None and configuration.lower() not in _SECRETS:
            # ignored by Device Builder < 1.17, enforced after; secrets.yaml refuses it there
            args["expected"] = current
        await db.call("devices/update_config", **args)
    return {"configuration": configuration, "changed": True, "backup_id": backup_id,
            "sha256": _sha(content)}


async def list_backups(settings, configuration=None) -> dict:
    if configuration is not None:
        check_configuration(configuration, settings)
    return {"backups": YamlBackupStore(settings.backup_dir).list(configuration)}


async def restore_backup(settings, backup_id, *, base_sha256=None, confirm=False,
                         session=None, backups=None) -> dict:
    store = backups or YamlBackupStore(settings.backup_dir)
    configuration, content = store.read(backup_id)
    return await update_config(settings, configuration, content, base_sha256=base_sha256,
                               confirm=confirm, session=session, backups=store)


# --- Tools: firmware jobs ----------------------------------------------------

TERMINAL = {"completed", "failed", "cancelled"}
_JOB_FIELDS = ("job_id", "configuration", "job_type", "status", "exit_code", "error",
               "progress", "depends_on", "is_deferred_install")


def _job(j: dict) -> dict:
    return {k: (j or {}).get(k) for k in _JOB_FIELDS}


def _job_list(res) -> list[dict]:
    # shortcut: 1.14.9 get_jobs shape not observed live (list or {"jobs": [...]}); Task 7 confirms.
    return res if isinstance(res, list) else (res or {}).get("jobs", [])


def _check_job_id(job_id) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ESPHomeError("job_id is required (from esphome_compile / esphome_install).")
    return job_id


async def compile_firmware(settings, configuration, *, session=None) -> dict:
    check_configuration(configuration, settings)
    async with (session or _session)(settings) as db:
        job = await db.call("firmware/compile", configuration=configuration)
    return {"job": _job(job)}


async def install(settings, configuration, *, port="OTA", confirm=False, session=None) -> dict:
    """Compile + flash. Always needs confirm=true, whatever DEPLOY_MODE says."""
    check_configuration(configuration, settings)
    async with (session or _session)(settings) as db:
        devices = (await db.call("devices/list") or {}).get("configured", [])
        dev = next((_flat_device(d) for d in devices if d.get("configuration") == configuration), None)
        if dev is None:
            raise NRNotFoundError(f"no ESPHome device with configuration {configuration}")
        if not confirm:
            out = {"preview": True, "device": dev, "port": port, "requires_confirm": True,
                   "hint": "re-call with confirm=true to compile and flash "
                           "(always required, also with DEPLOY_MODE=auto)."}
            if port == "OTA" and dev["state"] == "offline":
                out["warning"] = ("device is offline: the install becomes DEFERRED — it compiles now "
                                  "and flashes automatically when the device comes back online.")
            return out
        job = await db.call("firmware/install", configuration=configuration, port=port)
        try:
            jobs = _job_list(await db.call("firmware/get_jobs", configuration=configuration))
        except NRError:
            jobs = None
    cid = (job or {}).get("job_id")
    upload = next((j for j in jobs if cid is not None and j.get("depends_on") == cid), None) if jobs else None
    out = {"compile_job": _job(job), "upload_job": _job(upload) if upload else None,
           "is_deferred_install": bool((job or {}).get("is_deferred_install"))}
    if jobs is None:
        out["warning"] = "the compile job started, but the upload job could not be looked up; use esphome_job_status."
    return out


async def job_status(settings, job_id, *, wait_seconds=0, tail_lines=50, session=None) -> dict:
    _check_job_id(job_id)
    wait = min(max(float(wait_seconds), 0.0), JOB_MAX_WAIT)
    tail = min(max(int(tail_lines), 1), 2000)
    async with (session or _session)(settings) as db:
        job = await db.call("firmware/get_job", job_id=job_id)
        if not job:
            raise NRNotFoundError(f"job {job_id} not found")
        lines, final = await db.stream("firmware/follow_job", {"job_id": job_id},
                                       seconds=max(wait, REPLAY_SECONDS), tail=tail)
        if final is not None and job.get("status") not in TERMINAL:
            job = await db.call("firmware/get_job", job_id=job_id) or job
    out = _job(job)
    return {**out, "done": out["status"] in TERMINAL, "output_tail": lines}


async def cancel_job(settings, job_id, *, session=None) -> dict:
    _check_job_id(job_id)
    async with (session or _session)(settings) as db:
        res = await db.call("firmware/cancel", job_id=job_id)
    return {"job_id": job_id, "result": res}
