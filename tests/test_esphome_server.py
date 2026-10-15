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
