import asyncio

import pytest

from nrha_mcp import esphome
from nrha_mcp.client import NRAuthError, NRConflictError, NRNotFoundError
from nrha_mcp.config import Settings
from nrha_mcp.esphome import ESPHomeError, YamlBackupStore
from tests.test_ha_ws import FakeHAWsConn


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


async def test_db_connection_lost_maps_to_esphome_error(tmp_path):
    db = FakeDBConn({"x": lambda a: []})

    async def boom():
        raise ConnectionError("socket dropped")

    async with session_for(db)(_settings(tmp_path)) as conn:
        db.recv = boom
        with pytest.raises(ESPHomeError, match="connection lost"):
            await conn.call("x")


async def test_ha_connection_lost_maps_to_esphome_error(tmp_path):
    ha = FakeHAWsConn(_ha_frames()[:2])  # auth completes, then recv fails
    orig = ha.recv

    async def flaky():
        try:
            return await orig()
        except Exception:
            raise ConnectionError("socket dropped")

    ha.recv = flaky
    with pytest.raises(ESPHomeError, match="connection lost"):
        async with session_for(FakeDBConn({}), ha)(_settings(tmp_path)):
            pass


async def test_db_send_failure_maps_to_esphome_error(tmp_path):
    db = FakeDBConn({"x": lambda a: []})

    async def boom(obj):
        raise ConnectionError("socket dropped")

    async with session_for(db)(_settings(tmp_path)) as conn:
        db.send = boom
        with pytest.raises(ESPHomeError, match="connection lost"):
            await conn.call("x")


async def test_ha_auth_drop_maps_to_esphome_error(tmp_path):
    ha = FakeHAWsConn([])

    async def boom():
        raise ConnectionError("socket dropped")

    ha.recv = boom
    with pytest.raises(ESPHomeError, match="connection lost"):
        async with session_for(FakeDBConn({}), ha)(_settings(tmp_path)):
            pass


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
