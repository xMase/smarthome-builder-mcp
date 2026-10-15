# ESPHome tools via Device Builder over HA ingress (`esphome_*`) — Implementation Plan

**Status:** plan reviewed by the user 2026-10-15 (two fixes: `unauthorized` on add-on info → `NRAuthError`;
no `expected` on secrets writes). Execution method not chosen yet.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give nrha 11 `esphome_*` tools that list, read, write (preview + confirm + local backup), validate, log, compile and OTA-flash ESPHome devices through the Device Builder add-on's `/ws` websocket, reached over Home Assistant ingress with only `HA_URL` + `HA_TOKEN`.

**Architecture:** A new domain module `src/nrha_mcp/esphome.py` owns everything: configuration-name checks, a text backup store, the ingress handshake (HA websocket → add-on info + ingress session → second websocket to Device Builder), a small `_DB` protocol class (`call` for request/reply, `stream` for output streams with deadline and `devices/stop_stream`), and one async function per tool. `server.py` adds 11 thin `_wrap` tools. Every public domain function takes an injectable `session` factory so unit tests run the real protocol code against in-memory fake websockets.

**Tech Stack:** Python 3.11+, FastMCP (`mcp` SDK), pydantic-settings, `websockets` (asyncio client, already a dependency), pytest / pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-14-esphome-ingress-design.md`

## Global Constraints

- Only `HA_URL` + `HA_TOKEN` are needed; no SSH, no extra add-on, no new Python dependency. Raise the existing floor to `websockets>=14` (the code uses `additional_headers`, available from 14; 16.0 is installed).
- New settings, defaults only: `esphome_addon_slug: str = "5c53de3b_esphome"`, `esphome_allow_secrets: bool = False`. **Never add them (or any variable) to the repo `.env`** — document them in `.env.example` and README only.
- Configuration names: a bare name (no `/`, no `\`, no leading `.`) ending in `.yaml` or `.yml`; `secrets.yaml` / `secrets.yml` refused unless `esphome_allow_secrets`. Checked before any connection.
- `esphome_update_config` / `esphome_restore_backup` follow `DEPLOY_MODE` (propose → preview unless `confirm=true`). `esphome_install` **always** needs `confirm=true`, also with `DEPLOY_MODE=auto`. `esphome_compile` and `esphome_cancel_job` need no confirm.
- Backups: `BACKUP_DIR/esphome/<configuration>.<UTC %Y%m%dT%H%M%S%fZ>.yaml`, bytes exactly as read, no pruning.
- Limits: `esphome_logs` `seconds` ≤ 60; `esphome_job_status` `wait_seconds` ≤ 300; validate keeps the last 200 lines.
- nrha never sends `show_secrets` to `devices/validate`.
- All failures are `NRError` subclasses (`ESPHomeError(NRError)`, `NRAuthError`, `NRConflictError`, `NRNotFoundError` from `client.py`).
- The tool set is declared in THREE places: `server.py`, `tests/test_server_smoke.py` (`expected` set + `len(names) == N`), `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`). 40 → 51.
- Tests construct `Settings(...)` with explicit `ha_url`, `ha_token`, `backup_dir`, `deploy_mode`, `esphome_*` values so the developer's local `.env` cannot change their outcome.
- Commits: stage by explicit path; message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif`. Never push.

## Review Focus

- Device Builder reply shapes read from source but not observed live (`devices/get_config` returning a string vs `{"content": ...}`, `firmware/get_jobs` returning a list vs `{"jobs": [...]}`, `has_pending_changes`/`update_available` on the device vs inside `runtime_state`) — the tolerant helpers must accept both; Tasks 3 and 5 pin both shapes.
- A file with CRLF line endings or no final newline — backup bytes, sha256 and the `expected` sent must be byte-exact; Task 1 pins the CRLF backup round trip, Task 4 pins `expected` equal to the exact current text.
- Restoring a backup of `secrets.yaml` while `ESPHOME_ALLOW_SECRETS=false` — must be refused, not written; Task 4 pins it.
- A log stream from a chatty or unreachable device that never ends — must stop at `seconds` or `max_lines` and send `devices/stop_stream`; Task 3 pins both stops.
- `confirm=true` sent without `base_sha256` after the file changed since the preview — by design it still writes after the re-read check (the spec makes `base_sha256` optional); Task 4 pins that behaviour so it is a decision, not an accident.

---

### Task 1: Settings, configuration-name rule, YAML backup store

**Files:**
- Modify: `src/nrha_mcp/config.py` (after the `ha_ws_read_commands` line)
- Modify: `pyproject.toml` (`"websockets>=12"` → `"websockets>=14"`)
- Create: `src/nrha_mcp/esphome.py`
- Test: `tests/test_esphome.py` (create), `tests/test_config.py` (one test added)

**Interfaces:**
- Produces: `Settings.esphome_addon_slug: str`, `Settings.esphome_allow_secrets: bool`; in `esphome.py`: `class ESPHomeError(NRError)`, `check_configuration(name: str, settings) -> str`, `_sha(text: str) -> str`, `class YamlBackupStore(backup_dir: str)` with `snapshot(configuration: str, content: str) -> str` (returns `backup_id`), `list(configuration: str | None = None) -> list[dict]` (newest first; keys `backup_id`, `configuration`, `timestamp`, `size`), `read(backup_id: str) -> tuple[str, str]` (configuration, content).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_esphome_defaults():
    s = Settings(_env_file=None)
    assert s.esphome_addon_slug == "5c53de3b_esphome"
    assert s.esphome_allow_secrets is False
```

Create `tests/test_esphome.py`:

```python
import pytest

from nrha_mcp import esphome
from nrha_mcp.client import NRNotFoundError
from nrha_mcp.config import Settings
from nrha_mcp.esphome import ESPHomeError, YamlBackupStore


def _settings(tmp_path, **kw):
    base = dict(ha_url="http://ha:8123", ha_token="tok", backup_dir=str(tmp_path),
                deploy_mode="propose", esphome_addon_slug="5c53de3b_esphome",
                esphome_allow_secrets=False)
    base.update(kw)
    return Settings(**base)


@pytest.mark.parametrize("name", ["onju.yaml", "onju-voice-87864c.yml", "a b.yaml"])
def test_check_configuration_accepts(tmp_path, name):
    assert esphome.check_configuration(name, _settings(tmp_path)) == name


@pytest.mark.parametrize("name", ["", "onju", "onju.txt", "dir/onju.yaml", "..\\onju.yaml",
                                  ".hidden.yaml", "../onju.yaml", "on\x00ju.yaml", None])
def test_check_configuration_rejects(tmp_path, name):
    with pytest.raises(ESPHomeError, match="invalid configuration name"):
        esphome.check_configuration(name, _settings(tmp_path))


@pytest.mark.parametrize("name", ["secrets.yaml", "secrets.yml", "Secrets.yaml"])
def test_secrets_blocked_unless_flag(tmp_path, name):
    with pytest.raises(ESPHomeError, match="ESPHOME_ALLOW_SECRETS"):
        esphome.check_configuration(name, _settings(tmp_path))
    assert esphome.check_configuration(name, _settings(tmp_path, esphome_allow_secrets=True)) == name


def test_backup_store_roundtrip_is_byte_exact(tmp_path):
    store = YamlBackupStore(str(tmp_path))
    content = "esphome:\r\n  name: onju\r\nno-final-newline"
    bid = store.snapshot("onju.yaml", content)
    assert (tmp_path / "esphome" / bid).read_bytes() == content.encode("utf-8")
    assert store.read(bid) == ("onju.yaml", content)


def test_backup_store_list_newest_first_and_filter(tmp_path):
    store = YamlBackupStore(str(tmp_path))
    a = store.snapshot("a.yaml", "x: 1\n")
    b = store.snapshot("b.yaml", "x: 2\n")
    a2 = store.snapshot("a.yaml", "x: 3\n")
    ids = [e["backup_id"] for e in store.list()]
    assert ids == [a2, b, a]
    only_a = store.list("a.yaml")
    assert [e["backup_id"] for e in only_a] == [a2, a]
    assert only_a[0]["configuration"] == "a.yaml" and only_a[0]["size"] == 5


def test_backup_store_list_empty_dir(tmp_path):
    assert YamlBackupStore(str(tmp_path / "missing")).list() == []


@pytest.mark.parametrize("bid", ["../x.yaml", "onju.yaml", "x/onju.yaml.20261014T000000000000Z.yaml", ""])
def test_backup_store_read_rejects_bad_id(tmp_path, bid):
    with pytest.raises(ESPHomeError, match="invalid backup_id"):
        YamlBackupStore(str(tmp_path)).read(bid)


def test_backup_store_read_missing(tmp_path):
    with pytest.raises(NRNotFoundError):
        YamlBackupStore(str(tmp_path)).read("onju.yaml.20261014T000000000000Z.yaml")


def test_sha():
    assert esphome._sha("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_esphome.py tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.esphome` / `AttributeError: esphome_addon_slug`.

- [ ] **Step 3: Implement**

In `src/nrha_mcp/config.py`, after `ha_ws_read_commands: str | None = None    # ...`:

```python

    # ESPHome Device Builder via HA ingress (esphome_* tools; needs HA_URL + HA_TOKEN)
    esphome_addon_slug: str = "5c53de3b_esphome"
    esphome_allow_secrets: bool = False   # unblocks secrets.yaml / secrets.yml
```

In `pyproject.toml` replace `"websockets>=12",` with `"websockets>=14",`.

Create `src/nrha_mcp/esphome.py` (the full import block is used by Tasks 2–5):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_esphome.py tests/test_config.py -q`
Expected: PASS. Then the full suite: `.venv/bin/pytest -q` → all pass (201 + new).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/config.py pyproject.toml src/nrha_mcp/esphome.py tests/test_esphome.py tests/test_config.py
git commit -F - <<'EOF'
feat(esphome): settings, configuration-name rule and YAML backup store

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif
EOF
```

---

### Task 2: Ingress handshake and Device Builder `/ws` protocol

**Files:**
- Modify: `src/nrha_mcp/esphome.py` (append)
- Test: `tests/test_esphome.py` (append; the fakes defined here are reused by Tasks 3–5)

**Interfaces:**
- Consumes: `ha_ws._default_connect(settings)`, `ha_ws._auth(conn, token)`, `ha_ws._ws_url(ha_url)`, `ha_ws._HAWsConn(ws)`; `tests.test_ha_ws.FakeHAWsConn(frames)`.
- Produces (module constants, read at call time so tests can monkeypatch): `CONNECT_TIMEOUT = 15.0`, `REPLY_TIMEOUT = 30.0`, `VALIDATE_TIMEOUT = 180.0`, `LOGS_MAX_SECONDS = 60`, `JOB_MAX_WAIT = 300`, `REPLAY_SECONDS = 2.0`, `STREAM_TAIL = 200`.
  `_db_ws_url(ha_url: str, ingress_url: str) -> str`;
  `async _open_ingress(settings, ha_connect=None) -> tuple[str, str]` (ingress_url, session);
  `async _default_db_connect(url: str, cookie: str)` → conn with `send(dict)`, `recv() -> dict`, `close()`;
  `class _DB(conn)` with attribute `server_info: dict`, `async call(command: str, **args)` → the reply `result`, `async stream(command: str, args: dict, *, seconds: float, max_lines: int | None = None, tail: int | None = None) -> tuple[list[str], dict | None]` (lines, terminal `result` event data or None when stopped early / ended without it);
  `_db_error(command: str, msg: dict) -> NRError`;
  `_session(settings, *, ha_connect=None, db_connect=None)` — async context manager yielding a `_DB`.
  Test helpers in `tests/test_esphome.py`: `INGRESS`, `_ha_frames(info=None, session_ok=True)`, `FakeDBConn(handlers, server_info=None)` (attrs `sent`, `closed`, `url`, `cookie`), `reply(value)`, `stream_result(lines, data)`, `stream_open(lines)`, `STOP`, `session_for(db, ha=None)`, `sent(db, command) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_esphome.py` (add the imports at the top of the file):

```python
import asyncio

from nrha_mcp.client import NRAuthError, NRConflictError
from tests.test_ha_ws import FakeHAWsConn

INGRESS = "/api/hassio_ingress/TOK/"


def _ha_frames(info=None, session_ok=True):
    info = info if info is not None else {"state": "started", "ingress": True, "ingress_url": INGRESS}
    session = ({"id": 2, "type": "result", "success": True, "result": {"session": "SESS"}}
               if session_ok else
               {"id": 2, "type": "result", "success": False, "error": {"code": "unauthorized"}})
    return [{"type": "auth_required"}, {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": info}, session]


class FakeDBConn:
    """Device Builder /ws stand-in: answers each sent command from `handlers`
    (command -> fn(args) -> list of frames without message_id)."""

    def __init__(self, handlers, server_info=None):
        self.handlers = handlers
        self.sent = []
        self.closed = False
        self.url = self.cookie = None
        self._q = asyncio.Queue()
        self._q.put_nowait(server_info or {"server_version": "1.14.9", "ha_ingress": True})

    async def send(self, obj):
        self.sent.append(obj)
        for frame in self.handlers[obj["command"]](obj.get("args", {})):
            self._q.put_nowait({"message_id": obj["message_id"], **frame})

    async def recv(self):
        return await self._q.get()

    async def close(self):
        self.closed = True


def reply(value):
    return lambda args: [{"result": value}]


def stream_result(lines, data):
    return lambda args: [*({"event": "output", "data": ln} for ln in lines),
                         {"event": "result", "data": data}, {"result": None}]


def stream_open(lines):
    return lambda args: [{"event": "output", "data": ln} for ln in lines]


STOP = {"devices/stop_stream": reply({"cancelled": True})}


def session_for(db, ha=None):
    ha = ha or FakeHAWsConn(_ha_frames())

    async def ha_connect(settings):
        return ha

    async def db_connect(url, cookie):
        db.url, db.cookie = url, cookie
        return db

    return lambda settings: esphome._session(settings, ha_connect=ha_connect, db_connect=db_connect)


def sent(db, command):
    return [m["args"] for m in db.sent if m["command"] == command]


def test_db_ws_url():
    assert esphome._db_ws_url("http://ha:8123", INGRESS) == "ws://ha:8123/api/hassio_ingress/TOK/ws"
    assert esphome._db_ws_url("https://ha.x/", INGRESS) == "wss://ha.x/api/hassio_ingress/TOK/ws"


async def test_session_handshake(tmp_path):
    ha = FakeHAWsConn(_ha_frames())
    db = FakeDBConn({"ping": reply({"pong": True})})
    async with session_for(db, ha)(_settings(tmp_path)) as conn:
        assert conn.server_info["server_version"] == "1.14.9"
        assert await conn.call("ping") == {"pong": True}
    assert db.url == "ws://ha:8123/api/hassio_ingress/TOK/ws"
    assert db.cookie == "ingress_session=SESS"
    assert ha.sent[1] == {"id": 1, "type": "supervisor/api",
                          "endpoint": "/addons/5c53de3b_esphome/info", "method": "get"}
    assert ha.sent[2] == {"id": 2, "type": "supervisor/api",
                          "endpoint": "/ingress/session", "method": "post"}
    assert ha.closed and db.closed
    assert db.sent[0] == {"command": "ping", "message_id": "1", "args": {}}


async def test_addon_not_found(tmp_path):
    frames = _ha_frames()
    frames[2] = {"id": 1, "type": "result", "success": False, "error": {"message": "App does not exist"}}
    with pytest.raises(NRNotFoundError, match="ESPHOME_ADDON_SLUG"):
        async with session_for(FakeDBConn({}), FakeHAWsConn(frames))(_settings(tmp_path)):
            pass


async def test_addon_info_unauthorized_is_auth_error(tmp_path):
    frames = _ha_frames()
    frames[2] = {"id": 1, "type": "result", "success": False,
                 "error": {"code": "unauthorized", "message": "Unauthorized"}}
    with pytest.raises(NRAuthError, match="admin"):
        async with session_for(FakeDBConn({}), FakeHAWsConn(frames))(_settings(tmp_path)):
            pass


async def test_addon_not_started(tmp_path):
    ha = FakeHAWsConn(_ha_frames(info={"state": "stopped", "ingress": True, "ingress_url": INGRESS}))
    with pytest.raises(NRNotFoundError, match="state=stopped"):
        async with session_for(FakeDBConn({}), ha)(_settings(tmp_path)):
            pass


async def test_ingress_session_refused(tmp_path):
    ha = FakeHAWsConn(_ha_frames(session_ok=False))
    with pytest.raises(NRAuthError, match="admin"):
        async with session_for(FakeDBConn({}), ha)(_settings(tmp_path)):
            pass


@pytest.mark.parametrize("code,cls", [("not_found", NRNotFoundError),
                                      ("precondition_failed", NRConflictError),
                                      ("not_authenticated", NRAuthError),
                                      ("invalid_args", ESPHomeError)])
async def test_error_frames_map(tmp_path, code, cls):
    db = FakeDBConn({"x": lambda a: [{"error_code": code, "details": "boom"}]})
    async with session_for(db)(_settings(tmp_path)) as conn:
        with pytest.raises(cls, match=f"x failed: {code}: boom"):
            await conn.call("x")


async def test_call_reply_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome, "REPLY_TIMEOUT", 0.05)
    db = FakeDBConn({"x": lambda a: []})
    async with session_for(db)(_settings(tmp_path)) as conn:
        with pytest.raises(ESPHomeError, match="no reply"):
            await conn.call("x")


async def test_stream_until_result_keeps_tail(tmp_path):
    db = FakeDBConn({"s": stream_result([f"l{i}" for i in range(5)], {"success": True, "code": 0})})
    async with session_for(db)(_settings(tmp_path)) as conn:
        lines, final = await conn.stream("s", {}, seconds=5, tail=3)
    assert lines == ["l2", "l3", "l4"] and final == {"success": True, "code": 0}
    assert sent(db, "devices/stop_stream") == []


async def test_stream_stops_at_max_lines(tmp_path):
    db = FakeDBConn({"s": stream_open(["a", "b", "c", "d"]), **STOP})
    async with session_for(db)(_settings(tmp_path)) as conn:
        lines, final = await conn.stream("s", {}, seconds=5, max_lines=2)
    assert lines == ["a", "b"] and final is None
    assert sent(db, "devices/stop_stream") == [{"stream_id": "1"}]


async def test_stream_stops_at_deadline(tmp_path):
    db = FakeDBConn({"s": stream_open(["a"]), **STOP})
    async with session_for(db)(_settings(tmp_path)) as conn:
        lines, final = await conn.stream("s", {}, seconds=0.05)
    assert lines == ["a"] and final is None
    assert sent(db, "devices/stop_stream") == [{"stream_id": "1"}]


async def test_default_db_connect_maps_403(monkeypatch):
    import websockets
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    async def refuse(*a, **k):
        raise websockets.exceptions.InvalidStatus(Response(403, "Forbidden", Headers()))

    monkeypatch.setattr(websockets, "connect", refuse)
    with pytest.raises(NRAuthError, match="HTTP 403"):
        await esphome._default_db_connect("ws://ha/x/ws", "ingress_session=S")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_esphome.py -q`
Expected: the new tests FAIL with `AttributeError: module 'nrha_mcp.esphome' has no attribute '_session'` (and `_db_ws_url`, `_default_db_connect`); Task 1 tests still pass.

- [ ] **Step 3: Implement**

Append to `src/nrha_mcp/esphome.py`:

```python


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_esphome.py -q` → PASS; then `.venv/bin/pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/esphome.py tests/test_esphome.py
git commit -F - <<'EOF'
feat(esphome): HA ingress handshake and Device Builder /ws protocol

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif
EOF
```

---

### Task 3: Read and check — list devices, get config, validate, logs

**Files:**
- Modify: `src/nrha_mcp/esphome.py` (append)
- Test: `tests/test_esphome.py` (append)

**Interfaces:**
- Consumes: Task 1 `check_configuration`, `_sha`; Task 2 `_session`, `_DB.call`, `_DB.stream`, constants; test helpers.
- Produces: `_flat_device(d: dict) -> dict`, `_content(res) -> str`, and
  `async list_devices(settings, *, session=None) -> dict` (`server_version`, `devices: list[dict]`);
  `async get_config(settings, configuration, *, session=None) -> dict` (`configuration`, `content`, `sha256`);
  `async validate(settings, configuration, *, session=None) -> dict` (`configuration`, `success`, `code`, `output_tail`);
  `async logs(settings, configuration, *, seconds=10, max_lines=200, session=None) -> dict` (`configuration`, `seconds`, `lines`, `ended`, optional `exit_code`).
  `session` is a factory `settings -> async context manager yielding _DB`; default `_session`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_esphome.py`:

```python
DEV = {"configuration": "onju.yaml", "name": "onju", "address": "onju.local",
       "current_version": "2026.9.1", "update_available": True, "has_pending_changes": True,
       "runtime_state": {"state": "online", "ip_addresses": ["192.168.1.50"],
                         "deployed_version": "2026.8.0", "queued_update": False}}


async def test_list_devices_flattens(tmp_path):
    db = FakeDBConn({"devices/list": reply({"configured": [DEV], "importable": []})})
    res = await esphome.list_devices(_settings(tmp_path), session=session_for(db))
    assert res["server_version"] == "1.14.9"
    assert res["devices"] == [{
        "configuration": "onju.yaml", "name": "onju", "state": "online", "address": "onju.local",
        "ip_addresses": ["192.168.1.50"], "current_version": "2026.9.1",
        "deployed_version": "2026.8.0", "update_available": True,
        "has_pending_changes": True, "queued_update": False}]


async def test_list_devices_fields_inside_runtime_state(tmp_path):
    dev = {"configuration": "a.yaml", "name": "a",
           "runtime_state": {"state": "offline", "update_available": False, "has_pending_changes": False}}
    db = FakeDBConn({"devices/list": reply({"configured": [dev]})})
    (d,) = (await esphome.list_devices(_settings(tmp_path), session=session_for(db)))["devices"]
    assert d["state"] == "offline" and d["update_available"] is False and d["has_pending_changes"] is False


@pytest.mark.parametrize("shape", ["esphome:\n  name: onju\n", {"content": "esphome:\n  name: onju\n"}])
async def test_get_config_returns_content_and_sha(tmp_path, shape):
    db = FakeDBConn({"devices/get_config": reply(shape)})
    res = await esphome.get_config(_settings(tmp_path), "onju.yaml", session=session_for(db))
    assert res == {"configuration": "onju.yaml", "content": "esphome:\n  name: onju\n",
                   "sha256": esphome._sha("esphome:\n  name: onju\n")}
    assert sent(db, "devices/get_config") == [{"configuration": "onju.yaml"}]


async def test_get_config_rejects_before_connecting(tmp_path):
    def never(settings):
        raise AssertionError("must not connect")
    with pytest.raises(ESPHomeError, match="ESPHOME_ALLOW_SECRETS"):
        await esphome.get_config(_settings(tmp_path), "secrets.yaml", session=never)


async def test_validate_success_never_shows_secrets(tmp_path):
    db = FakeDBConn({"devices/validate": stream_result(["INFO ok"], {"success": True, "code": 0})})
    res = await esphome.validate(_settings(tmp_path), "onju.yaml", session=session_for(db))
    assert res == {"configuration": "onju.yaml", "success": True, "code": 0, "output_tail": ["INFO ok"]}
    assert sent(db, "devices/validate") == [{"configuration": "onju.yaml"}]


async def test_validate_failure_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome, "STREAM_TAIL", 2)
    db = FakeDBConn({"devices/validate": stream_result(["a", "b", "ERROR x"], {"success": False, "code": 2})})
    res = await esphome.validate(_settings(tmp_path), "onju.yaml", session=session_for(db))
    assert res["success"] is False and res["code"] == 2 and res["output_tail"] == ["b", "ERROR x"]


async def test_validate_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome, "VALIDATE_TIMEOUT", 0.05)
    db = FakeDBConn({"devices/validate": stream_open(["a"]), **STOP})
    with pytest.raises(ESPHomeError, match="did not finish"):
        await esphome.validate(_settings(tmp_path), "onju.yaml", session=session_for(db))


async def test_logs_stop_at_max_lines(tmp_path):
    db = FakeDBConn({"devices/logs": stream_open(["l1", "l2", "l3"]), **STOP})
    res = await esphome.logs(_settings(tmp_path), "onju.yaml", seconds=5, max_lines=2,
                             session=session_for(db))
    assert res["lines"] == ["l1", "l2"] and res["ended"] is False
    assert sent(db, "devices/stop_stream") == [{"stream_id": "1"}]


async def test_logs_stop_at_deadline(tmp_path, monkeypatch):
    db = FakeDBConn({"devices/logs": stream_open(["l1"]), **STOP})
    res = await esphome.logs(_settings(tmp_path), "onju.yaml", seconds=0.05, session=session_for(db))
    assert res["lines"] == ["l1"] and res["ended"] is False and res["seconds"] == 0.05
    assert sent(db, "devices/stop_stream") == [{"stream_id": "1"}]


async def test_logs_process_exit(tmp_path):
    db = FakeDBConn({"devices/logs": stream_result(["ERROR Connecting failed"], {"success": False, "code": 1})})
    res = await esphome.logs(_settings(tmp_path), "onju.yaml", seconds=5, session=session_for(db))
    assert res["ended"] is True and res["exit_code"] == 1 and res["lines"] == ["ERROR Connecting failed"]


async def test_logs_seconds_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome, "LOGS_MAX_SECONDS", 0.05)
    db = FakeDBConn({"devices/logs": stream_open([]), **STOP})
    res = await esphome.logs(_settings(tmp_path), "onju.yaml", seconds=999, session=session_for(db))
    assert res["seconds"] == 0.05
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_esphome.py -q`
Expected: new tests FAIL with `AttributeError: ... 'list_devices'` (and `get_config`, `validate`, `logs`).

- [ ] **Step 3: Implement**

Append to `src/nrha_mcp/esphome.py`:

```python


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_esphome.py -q` → PASS; `.venv/bin/pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/esphome.py tests/test_esphome.py
git commit -F - <<'EOF'
feat(esphome): list devices, get config, validate and logs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif
EOF
```

---

### Task 4: Write path — update config, list and restore backups

**Files:**
- Modify: `src/nrha_mcp/esphome.py` (append)
- Test: `tests/test_esphome.py` (append)

**Interfaces:**
- Consumes: Task 1 `check_configuration`, `_sha`, `YamlBackupStore`; Task 2 `_session`; Task 3 `_content`; test helpers.
- Produces: `_diff(old: str, new: str, name: str) -> str`;
  `async update_config(settings, configuration, content, *, base_sha256=None, confirm=False, session=None, backups=None) -> dict` — preview `{preview: True, configuration, new_file, diff, base_sha256, requires_confirm: True, hint}`, no-op `{configuration, changed: False}`, written `{configuration, changed: True, backup_id, sha256}`;
  `async list_backups(settings, configuration=None) -> dict` (`backups: list[dict]`);
  `async restore_backup(settings, backup_id, *, base_sha256=None, confirm=False, session=None, backups=None) -> dict` (same shapes as `update_config`).
  `backups` is a `YamlBackupStore`; default `YamlBackupStore(settings.backup_dir)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_esphome.py`:

```python
OLD = "esphome:\r\n  name: onju\r\n"
NEW = "esphome:\r\n  name: onju2\r\n"


def _files(initial):
    """Handlers backed by a dict of files; update_config writes into it."""
    files = dict(initial)

    def get(args):
        if args["configuration"] not in files:
            return [{"error_code": "not_found", "details": "missing"}]
        return [{"result": files[args["configuration"]]}]

    def put(args):
        files[args["configuration"]] = args["content"]
        return [{"result": None}]

    return files, {"devices/get_config": get, "devices/update_config": put}


async def test_update_preview_writes_nothing(tmp_path):
    files, h = _files({"onju.yaml": OLD})
    db = FakeDBConn(h)
    res = await esphome.update_config(_settings(tmp_path), "onju.yaml", NEW, session=session_for(db))
    assert res["preview"] is True and res["requires_confirm"] is True and res["new_file"] is False
    assert res["base_sha256"] == esphome._sha(OLD)
    assert "-  name: onju" in res["diff"] and "+  name: onju2" in res["diff"]
    assert files["onju.yaml"] == OLD and sent(db, "devices/update_config") == []
    assert not (tmp_path / "esphome").exists()


async def test_update_confirm_backs_up_and_sends_expected(tmp_path):
    files, h = _files({"onju.yaml": OLD})
    db = FakeDBConn(h)
    res = await esphome.update_config(_settings(tmp_path), "onju.yaml", NEW,
                                      base_sha256=esphome._sha(OLD), confirm=True,
                                      session=session_for(db))
    assert res["changed"] is True and res["sha256"] == esphome._sha(NEW)
    assert files["onju.yaml"] == NEW
    assert sent(db, "devices/update_config") == [
        {"configuration": "onju.yaml", "content": NEW, "expected": OLD}]
    assert (tmp_path / "esphome" / res["backup_id"]).read_bytes() == OLD.encode()


async def test_update_secrets_sends_no_expected(tmp_path):
    # Device Builder >= 1.17 answers invalid_args to `expected` on secrets.yaml.
    files, h = _files({"secrets.yaml": "wifi: a\n"})
    db = FakeDBConn(h)
    await esphome.update_config(_settings(tmp_path, esphome_allow_secrets=True), "secrets.yaml",
                                "wifi: b\n", confirm=True, session=session_for(db))
    assert sent(db, "devices/update_config") == [{"configuration": "secrets.yaml", "content": "wifi: b\n"}]


async def test_update_auto_mode_writes_without_confirm(tmp_path):
    files, h = _files({"onju.yaml": OLD})
    res = await esphome.update_config(_settings(tmp_path, deploy_mode="auto"), "onju.yaml", NEW,
                                      session=session_for(FakeDBConn(h)))
    assert res["changed"] is True and files["onju.yaml"] == NEW


async def test_update_no_change(tmp_path):
    files, h = _files({"onju.yaml": OLD})
    db = FakeDBConn(h)
    res = await esphome.update_config(_settings(tmp_path), "onju.yaml", OLD, confirm=True,
                                      session=session_for(db))
    assert res == {"configuration": "onju.yaml", "changed": False}
    assert sent(db, "devices/update_config") == []


async def test_update_base_sha_conflict(tmp_path):
    files, h = _files({"onju.yaml": OLD})
    db = FakeDBConn(h)
    with pytest.raises(NRConflictError, match="re-read"):
        await esphome.update_config(_settings(tmp_path), "onju.yaml", NEW, base_sha256="0" * 64,
                                    confirm=True, session=session_for(db))
    assert files["onju.yaml"] == OLD


async def test_update_reread_conflict(tmp_path):
    reads = iter([OLD, "changed meanwhile\n"])
    db = FakeDBConn({"devices/get_config": lambda a: [{"result": next(reads)}],
                     "devices/update_config": reply(None)})
    with pytest.raises(NRConflictError, match="changed while writing"):
        await esphome.update_config(_settings(tmp_path), "onju.yaml", NEW, confirm=True,
                                    session=session_for(db))
    assert sent(db, "devices/update_config") == []


async def test_update_confirm_without_base_still_writes(tmp_path):
    # Review Focus: base_sha256 is optional; without it only the re-read check applies.
    files, h = _files({"onju.yaml": "edited after preview\n"})
    res = await esphome.update_config(_settings(tmp_path), "onju.yaml", NEW, confirm=True,
                                      session=session_for(FakeDBConn(h)))
    assert res["changed"] is True and files["onju.yaml"] == NEW


async def test_update_new_file(tmp_path):
    files, h = _files({})
    db = FakeDBConn(h)
    prev = await esphome.update_config(_settings(tmp_path), "new.yaml", NEW, session=session_for(db))
    assert prev["new_file"] is True and prev["base_sha256"] is None
    res = await esphome.update_config(_settings(tmp_path), "new.yaml", NEW, confirm=True,
                                      session=session_for(FakeDBConn(h)))
    assert res["backup_id"] is None and files["new.yaml"] == NEW


async def test_update_rejects_empty_content(tmp_path):
    with pytest.raises(ESPHomeError, match="empty"):
        await esphome.update_config(_settings(tmp_path), "onju.yaml", "  \n", confirm=True,
                                    session=lambda s: (_ for _ in ()).throw(AssertionError()))


async def test_list_backups(tmp_path):
    store = esphome.YamlBackupStore(str(tmp_path))
    bid = store.snapshot("onju.yaml", OLD)
    res = await esphome.list_backups(_settings(tmp_path), "onju.yaml")
    assert [b["backup_id"] for b in res["backups"]] == [bid]


async def test_restore_goes_through_write_path(tmp_path):
    store = esphome.YamlBackupStore(str(tmp_path))
    bid = store.snapshot("onju.yaml", OLD)
    files, h = _files({"onju.yaml": NEW})
    prev = await esphome.restore_backup(_settings(tmp_path), bid, session=session_for(FakeDBConn(h)))
    assert prev["preview"] is True and files["onju.yaml"] == NEW
    res = await esphome.restore_backup(_settings(tmp_path), bid, base_sha256=prev["base_sha256"],
                                       confirm=True, session=session_for(FakeDBConn(h)))
    assert res["changed"] is True and files["onju.yaml"] == OLD
    assert len(store.list("onju.yaml")) == 2  # the restore backed up NEW first


async def test_restore_secrets_backup_blocked(tmp_path):
    bid = esphome.YamlBackupStore(str(tmp_path)).snapshot("secrets.yaml", "wifi: x\n")
    with pytest.raises(ESPHomeError, match="ESPHOME_ALLOW_SECRETS"):
        await esphome.restore_backup(_settings(tmp_path), bid, confirm=True,
                                     session=lambda s: (_ for _ in ()).throw(AssertionError()))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_esphome.py -q`
Expected: new tests FAIL with `AttributeError: ... 'update_config'` (and `list_backups`, `restore_backup`).

- [ ] **Step 3: Implement**

Append to `src/nrha_mcp/esphome.py`:

```python


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
                                  f"(backup {backup_id} kept); re-read it and preview again.")
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_esphome.py -q` → PASS; `.venv/bin/pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/esphome.py tests/test_esphome.py
git commit -F - <<'EOF'
feat(esphome): YAML write with preview, backup and conflict checks; backup restore

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif
EOF
```

---

### Task 5: Firmware — compile, install (always confirm), job status, cancel

**Files:**
- Modify: `src/nrha_mcp/esphome.py` (append)
- Test: `tests/test_esphome.py` (append)

**Interfaces:**
- Consumes: Task 1 `check_configuration`; Task 2 `_session`, `_DB.call`, `_DB.stream`, `JOB_MAX_WAIT`, `REPLAY_SECONDS`; Task 3 `_flat_device`; test helpers, `DEV`.
- Produces: `_job(j: dict) -> dict` (keys `job_id`, `configuration`, `job_type`, `status`, `exit_code`, `error`, `progress`, `depends_on`, `is_deferred_install`), `_job_list(res) -> list[dict]`, `TERMINAL = {"completed", "failed", "cancelled"}`, and
  `async compile_firmware(settings, configuration, *, session=None) -> dict` (`job`);
  `async install(settings, configuration, *, port="OTA", confirm=False, session=None) -> dict` — preview `{preview: True, device, port, requires_confirm: True, hint, warning?}`, confirmed `{compile_job, upload_job, is_deferred_install}`;
  `async job_status(settings, job_id, *, wait_seconds=0, tail_lines=50, session=None) -> dict` (`_job` keys + `done`, `output_tail`);
  `async cancel_job(settings, job_id, *, session=None) -> dict` (`job_id`, `result`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_esphome.py`:

```python
JOB = {"job_id": "c1", "configuration": "onju.yaml", "job_type": "install", "status": "queued",
       "exit_code": None, "error": None, "progress": None, "depends_on": None,
       "is_deferred_install": False}
UPLOAD = {**JOB, "job_id": "u1", "job_type": "upload", "depends_on": "c1"}


async def test_compile_returns_job_at_once(tmp_path):
    db = FakeDBConn({"firmware/compile": reply({**JOB, "job_type": "compile"})})
    res = await esphome.compile_firmware(_settings(tmp_path), "onju.yaml", session=session_for(db))
    assert res["job"]["job_id"] == "c1" and res["job"]["job_type"] == "compile"
    assert sent(db, "firmware/compile") == [{"configuration": "onju.yaml"}]


@pytest.mark.parametrize("mode", ["propose", "auto"])
async def test_install_always_previews_without_confirm(tmp_path, mode):
    db = FakeDBConn({"devices/list": reply({"configured": [DEV]})})
    res = await esphome.install(_settings(tmp_path, deploy_mode=mode), "onju.yaml",
                                session=session_for(db))
    assert res["preview"] is True and res["requires_confirm"] is True
    assert res["device"]["state"] == "online" and "warning" not in res
    assert sent(db, "firmware/install") == []


async def test_install_offline_warns_deferred(tmp_path):
    dev = {**DEV, "runtime_state": {**DEV["runtime_state"], "state": "offline"}}
    db = FakeDBConn({"devices/list": reply({"configured": [dev]})})
    res = await esphome.install(_settings(tmp_path), "onju.yaml", session=session_for(db))
    assert "DEFERRED" in res["warning"]


async def test_install_unknown_device(tmp_path):
    db = FakeDBConn({"devices/list": reply({"configured": [DEV]})})
    with pytest.raises(NRNotFoundError):
        await esphome.install(_settings(tmp_path), "other.yaml", session=session_for(db))


@pytest.mark.parametrize("jobs_shape", [lambda js: js, lambda js: {"jobs": js}])
async def test_install_confirm_finds_upload_job(tmp_path, jobs_shape):
    db = FakeDBConn({"devices/list": reply({"configured": [DEV]}),
                     "firmware/install": reply(JOB),
                     "firmware/get_jobs": reply(jobs_shape([JOB, UPLOAD]))})
    res = await esphome.install(_settings(tmp_path), "onju.yaml", confirm=True, session=session_for(db))
    assert res["compile_job"]["job_id"] == "c1" and res["upload_job"]["job_id"] == "u1"
    assert res["is_deferred_install"] is False
    assert sent(db, "firmware/install") == [{"configuration": "onju.yaml", "port": "OTA"}]
    assert sent(db, "firmware/get_jobs") == [{"configuration": "onju.yaml"}]


async def test_job_status_terminal(tmp_path):
    done = {**JOB, "status": "completed", "exit_code": 0}
    db = FakeDBConn({"firmware/get_job": reply(done),
                     "firmware/follow_job": stream_result(["a", "b", "c"], {"status": "completed",
                                                                            "exit_code": 0})})
    res = await esphome.job_status(_settings(tmp_path), "c1", tail_lines=2, session=session_for(db))
    assert res["done"] is True and res["status"] == "completed" and res["output_tail"] == ["b", "c"]
    assert sent(db, "devices/stop_stream") == []


async def test_job_status_running_stops_after_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome, "REPLAY_SECONDS", 0.05)
    db = FakeDBConn({"firmware/get_job": reply({**JOB, "status": "running", "progress": 40}),
                     "firmware/follow_job": stream_open(["Compiling ..."]), **STOP})
    res = await esphome.job_status(_settings(tmp_path), "c1", session=session_for(db))
    assert res["done"] is False and res["progress"] == 40 and res["output_tail"] == ["Compiling ..."]
    assert sent(db, "devices/stop_stream") == [{"stream_id": "2"}]


async def test_job_status_wait_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome, "JOB_MAX_WAIT", 0.05)
    monkeypatch.setattr(esphome, "REPLAY_SECONDS", 0.01)
    db = FakeDBConn({"firmware/get_job": reply({**JOB, "status": "running"}),
                     "firmware/follow_job": stream_open([]), **STOP})
    res = await esphome.job_status(_settings(tmp_path), "c1", wait_seconds=10_000, session=session_for(db))
    assert res["done"] is False


async def test_job_status_unknown(tmp_path):
    db = FakeDBConn({"firmware/get_job": lambda a: [{"error_code": "not_found", "details": "no job"}]})
    with pytest.raises(NRNotFoundError):
        await esphome.job_status(_settings(tmp_path), "nope", session=session_for(db))


async def test_cancel_job(tmp_path):
    db = FakeDBConn({"firmware/cancel": reply({"cancelled": True})})
    res = await esphome.cancel_job(_settings(tmp_path), "c1", session=session_for(db))
    assert res == {"job_id": "c1", "result": {"cancelled": True}}
    assert sent(db, "firmware/cancel") == [{"job_id": "c1"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_esphome.py -q`
Expected: new tests FAIL with `AttributeError: ... 'compile_firmware'` (and `install`, `job_status`, `cancel_job`).

- [ ] **Step 3: Implement**

Append to `src/nrha_mcp/esphome.py`:

```python


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
        jobs = _job_list(await db.call("firmware/get_jobs", configuration=configuration))
    upload = next((j for j in jobs if j.get("depends_on") == (job or {}).get("job_id")), None)
    return {"compile_job": _job(job), "upload_job": _job(upload) if upload else None,
            "is_deferred_install": bool((job or {}).get("is_deferred_install"))}


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
```

Note for `test_job_status_running_stops_after_replay`: `get_job` uses message_id `"1"`, `follow_job` uses `"2"`, so `stop_stream` carries `stream_id: "2"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_esphome.py -q` → PASS; `.venv/bin/pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/esphome.py tests/test_esphome.py
git commit -F - <<'EOF'
feat(esphome): compile, confirm-gated OTA install, job status and cancel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif
EOF
```

---

### Task 6: MCP tools, tool-count sync, docs

**Files:**
- Modify: `src/nrha_mcp/server.py` (import line 10; new tools after `ha_ws_command`, before `def main()`)
- Modify: `tests/test_server_smoke.py` (`expected` set + `len(names) == 51`)
- Modify: `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`)
- Modify: `README.md` (line 44 count; tools table row; config table rows)
- Modify: `.env.example` (new section before "Transport")
- Test: `tests/test_esphome_server.py` (create)

**Interfaces:**
- Consumes: every public function of Tasks 3–5 with the signatures above, `esphome.list_backups`, `esphome.restore_backup`; `server.settings`, `server._wrap`, `server._err`.
- Produces: tools `esphome_list_devices`, `esphome_get_config`, `esphome_update_config`, `esphome_validate`, `esphome_logs`, `esphome_compile`, `esphome_install`, `esphome_job_status`, `esphome_cancel_job`, `esphome_list_backups`, `esphome_restore_backup`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_esphome_server.py`:

```python
import json

import nrha_mcp.server as srv


async def test_esphome_tools_need_ha(monkeypatch):
    monkeypatch.setattr(srv.settings, "ha_url", "")
    out = json.loads(await srv.esphome_get_config("onju.yaml"))
    assert "HA_URL and HA_TOKEN" in out["error"]


async def test_esphome_bad_name_is_structured_error(monkeypatch):
    monkeypatch.setattr(srv.settings, "ha_url", "http://ha:8123")
    monkeypatch.setattr(srv.settings, "ha_token", "tok")
    out = json.loads(await srv.esphome_get_config("../x.yaml"))
    assert "invalid configuration name" in out["error"]


async def test_esphome_list_backups_needs_no_ha(monkeypatch, tmp_path):
    monkeypatch.setattr(srv.settings, "ha_url", "")
    monkeypatch.setattr(srv.settings, "backup_dir", str(tmp_path))
    assert json.loads(await srv.esphome_list_backups()) == {"backups": []}
```

In `tests/test_server_smoke.py` add to the `expected` set:

```python
        "esphome_list_devices", "esphome_get_config", "esphome_update_config", "esphome_validate",
        "esphome_logs", "esphome_compile", "esphome_install", "esphome_job_status",
        "esphome_cancel_job", "esphome_list_backups", "esphome_restore_backup",
```

and change `assert len(names) == 40` to `assert len(names) == 51`.

In `scripts/smoke_e2e.py` add to `EXPECTED_TOOLS` (one per line, same style):

```python
    "esphome_list_devices",
    "esphome_get_config",
    "esphome_update_config",
    "esphome_validate",
    "esphome_logs",
    "esphome_compile",
    "esphome_install",
    "esphome_job_status",
    "esphome_cancel_job",
    "esphome_list_backups",
    "esphome_restore_backup",
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_esphome_server.py tests/test_server_smoke.py tests/test_tool_count_sync.py -q`
Expected: FAIL — `AttributeError: module 'nrha_mcp.server' has no attribute 'esphome_get_config'`, smoke count 40 ≠ 51, sync test names the missing tools.

- [ ] **Step 3: Implement**

In `src/nrha_mcp/server.py` line 10 add `esphome` to the import:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics, ha_ws, subflow_edit, esphome
```

Insert after the `ha_ws_command` tool, before `def main():`:

```python
# ---- ESPHome (Device Builder add-on via HA ingress) ----
def _esphome_unset() -> str | None:
    if not settings.ha_url or not settings.ha_token:
        return _err("ESPHome tools need HA_URL and HA_TOKEN to be set.")
    return None


@mcp.tool()
async def esphome_list_devices() -> str:
    """List ESPHome devices from the Device Builder add-on (via HA ingress): configuration,
    state online/offline/unknown, address, IPs, installed vs deployed version, pending changes."""
    return _esphome_unset() or await _wrap(lambda: esphome.list_devices(settings))


@mcp.tool()
async def esphome_get_config(configuration: str) -> str:
    """Read an ESPHome YAML (bare name, e.g. 'onju.yaml'). Returns content + sha256; pass the
    sha256 as base_sha256 to esphome_update_config. secrets.yaml is blocked unless
    ESPHOME_ALLOW_SECRETS=true."""
    return _esphome_unset() or await _wrap(lambda: esphome.get_config(settings, configuration))


@mcp.tool()
async def esphome_update_config(configuration: str, content: str, base_sha256: str | None = None,
                                confirm: bool = False) -> str:
    """Write an ESPHome YAML (full new content). Returns a diff preview + base_sha256 unless
    confirm=true (DEPLOY_MODE=propose); then backs up the current file locally and writes.
    Pass the preview's base_sha256 with confirm=true so a concurrent edit becomes a conflict."""
    return _esphome_unset() or await _wrap(lambda: esphome.update_config(
        settings, configuration, content, base_sha256=base_sha256, confirm=confirm))


@mcp.tool()
async def esphome_validate(configuration: str) -> str:
    """Validate an ESPHome YAML on the Device Builder (esphome config). Returns success, code and
    the last 200 output lines (secrets stay redacted)."""
    return _esphome_unset() or await _wrap(lambda: esphome.validate(settings, configuration))


@mcp.tool()
async def esphome_logs(configuration: str, seconds: float = 10, max_lines: int = 200) -> str:
    """Capture live device logs for `seconds` (max 60) or `max_lines` lines, then stop."""
    return _esphome_unset() or await _wrap(lambda: esphome.logs(
        settings, configuration, seconds=seconds, max_lines=max_lines))


@mcp.tool()
async def esphome_compile(configuration: str) -> str:
    """Start a firmware compile on the Device Builder (no flash). Returns the job at once;
    follow it with esphome_job_status."""
    return _esphome_unset() or await _wrap(lambda: esphome.compile_firmware(settings, configuration))


@mcp.tool()
async def esphome_install(configuration: str, port: str = "OTA", confirm: bool = False) -> str:
    """Compile and flash a device (default OTA). ALWAYS returns a preview (device state,
    versions, deferred-install warning if offline) unless confirm=true, also with
    DEPLOY_MODE=auto. Returns compile + upload job ids; follow with esphome_job_status."""
    return _esphome_unset() or await _wrap(lambda: esphome.install(
        settings, configuration, port=port, confirm=confirm))


@mcp.tool()
async def esphome_job_status(job_id: str, wait_seconds: float = 0, tail_lines: int = 50) -> str:
    """Status + last output lines of a compile/upload job. wait_seconds (max 300) waits for the
    job to end; done=true when completed/failed/cancelled."""
    return _esphome_unset() or await _wrap(lambda: esphome.job_status(
        settings, job_id, wait_seconds=wait_seconds, tail_lines=tail_lines))


@mcp.tool()
async def esphome_cancel_job(job_id: str) -> str:
    """Cancel a queued or running compile/upload job (its dependent jobs cancel too)."""
    return _esphome_unset() or await _wrap(lambda: esphome.cancel_job(settings, job_id))


@mcp.tool()
async def esphome_list_backups(configuration: str | None = None) -> str:
    """List local ESPHome YAML backups (newest first), written before every esphome write."""
    return await _wrap(lambda: esphome.list_backups(settings, configuration))


@mcp.tool()
async def esphome_restore_backup(backup_id: str, base_sha256: str | None = None,
                                 confirm: bool = False) -> str:
    """Restore a local ESPHome backup through the same write path as esphome_update_config
    (diff preview, backup of the current file, conflict check)."""
    return _esphome_unset() or await _wrap(lambda: esphome.restore_backup(
        settings, backup_id, base_sha256=base_sha256, confirm=confirm))
```

`README.md`:
- line 44: `40 MCP tools, grouped:` → `51 MCP tools, grouped:`
- add a tools-table row after the "Home Assistant (act)" row:

```markdown
| **ESPHome** (Device Builder add-on via HA ingress; needs `HA_URL` + `HA_TOKEN`) | `esphome_list_devices`, `esphome_get_config`, `esphome_validate`, `esphome_logs` (read); `esphome_update_config` and `esphome_restore_backup` (diff preview → `confirm=true` per `DEPLOY_MODE`, local backup first, sha256 conflict check); `esphome_compile`, `esphome_install` (**always** `confirm=true`, also in `auto`; offline device → deferred flash), `esphome_job_status`, `esphome_cancel_job`, `esphome_list_backups`. `secrets.yaml` is blocked unless `ESPHOME_ALLOW_SECRETS=true` |
```

- add to the configuration table, after the `HA_WS_READ_COMMANDS` row:

```markdown
| `ESPHOME_ADDON_SLUG` | `5c53de3b_esphome` | Slug of the ESPHome Device Builder add-on used by the `esphome_*` tools (reached through HA ingress with `HA_URL` + `HA_TOKEN`; the token must belong to an HA admin user if the add-on panel is admin-only) |
| `ESPHOME_ALLOW_SECRETS` | `false` | `true` lets `esphome_get_config` / `esphome_update_config` / `esphome_restore_backup` touch `secrets.yaml` / `secrets.yml` (their values then pass through the conversation) |
```

`.env.example`: insert before the `# Transport` section header block:

```bash
# -----------------------------------------------------------------------------
# ESPHome  —  Device Builder add-on through HA ingress (esphome_* tools; optional)
# -----------------------------------------------------------------------------
# Uses HA_URL + HA_TOKEN above; nothing else to set up.
ESPHOME_ADDON_SLUG=5c53de3b_esphome
# true = allow reading/writing secrets.yaml (values pass through the conversation).
ESPHOME_ALLOW_SECRETS=false

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass (including `test_tool_count_sync.py` and `test_server_smoke.py` with 51).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_esphome_server.py tests/test_server_smoke.py scripts/smoke_e2e.py README.md .env.example
git commit -F - <<'EOF'
feat(server): esphome_* tools via Device Builder over HA ingress

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QPJg6EjGDYHitSvP7Vbpif
EOF
```

---

### Task 7: Manual check on the user's HA (controller only, each step with the user's ok)

Not code. Run by the controller session through the nrha MCP server built from this branch, after the final
whole-branch review. The running nrha must be restarted on this branch's code first (the user decides how: local
stdio server or the dev stack); do **not** add variables to the repo `.env`.

- [ ] **Step 1:** `esphome_list_devices` — confirm the handshake works (ingress session, cookie through the
  websocket, `server_version` 1.14.x) and the device fields are filled. If a field is `null` everywhere, compare
  with the raw `devices/list` reply and fix `_flat_device`.
- [ ] **Step 2:** `esphome_get_config("onju-voice-87864c.yaml")` — content + sha256; confirm the `get_config`
  reply shape.
- [ ] **Step 3:** `esphome_validate("onju-voice-87864c.yaml")` — success/code and output tail.
- [ ] **Step 4:** `esphome_logs("onju-voice-87864c.yaml", seconds=10)` — lines arrive and the stream stops.
- [ ] **Step 5 (only if the user asks):** a write (preview → confirm) and/or `esphome_install` (preview →
  confirm) → `esphome_job_status(wait_seconds=...)`; confirm the `firmware/get_jobs` shape and the upload job.
- [ ] **Step 6:** record outcomes and any shape fixes in the ledger; fixes go in a normal TDD commit.
