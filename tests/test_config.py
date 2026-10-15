# tests/test_config.py
import pytest
from pydantic import ValidationError
from nrha_mcp.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.nr_url == "http://localhost:1880"
    assert s.deploy_mode == "propose"
    assert s.nr_verify_ssl is True
    assert s.mcp_transport == "stdio"


def test_env_override(monkeypatch):
    monkeypatch.setenv("NR_URL", "http://ha.local:1880")
    monkeypatch.setenv("DEPLOY_MODE", "auto")
    monkeypatch.setenv("NR_VERIFY_SSL", "0")
    s = Settings(_env_file=None)
    assert s.nr_url == "http://ha.local:1880"
    assert s.deploy_mode == "auto"
    assert s.nr_verify_ssl is False


def test_invalid_mode_rejected():
    with pytest.raises(ValidationError):
        Settings(deploy_mode="banana", _env_file=None)


def test_esphome_defaults():
    s = Settings(_env_file=None)
    assert s.esphome_addon_slug == "5c53de3b_esphome"
    assert s.esphome_allow_secrets is False


def test_auth_token_hidden_from_repr():
    s = Settings(_env_file=None, mcp_auth_token="s3cret-token")
    assert "s3cret-token" not in repr(s)
    assert s.mcp_auth_token.get_secret_value() == "s3cret-token"


def test_blank_auth_token_is_none():
    assert Settings(_env_file=None, mcp_auth_token="").mcp_auth_token is None
    assert Settings(_env_file=None, mcp_auth_token="  \n").mcp_auth_token is None


def test_token_is_stripped():
    s = Settings(_env_file=None, mcp_auth_token="  s3cret-token\n")
    assert s.mcp_auth_token.get_secret_value() == "s3cret-token"
