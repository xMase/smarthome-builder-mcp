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
