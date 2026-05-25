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
