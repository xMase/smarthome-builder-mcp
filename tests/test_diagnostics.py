import pytest

from nrha_mcp import diagnostics
from nrha_mcp.client import NRError
from nrha_mcp.config import Settings


@pytest.mark.asyncio
async def test_diagnostics_full_snapshot(fake_client):
    settings = Settings(ha_url="", ha_token="")   # no HA configured
    res = await diagnostics.run(fake_client, settings)
    assert res["node_red"]["reachable"] is True
    assert res["node_red"]["version"] == "4.1.10"
    assert res["node_red"]["context_stores"] == ["store", "default"]
    assert res["node_red"]["context_default"] == "default"
    assert res["flows"]["tabs"] == 1                 # SAMPLE_FLOWS has one tab
    assert res["config"]["deploy_mode"] == settings.deploy_mode
    assert res["home_assistant"] == {"configured": False}


@pytest.mark.asyncio
async def test_diagnostics_nr_unreachable(fake_client):
    async def _raise(*a, **k):
        raise NRError("connection refused")
    fake_client.get_settings = _raise
    settings = Settings(ha_url="", ha_token="")
    res = await diagnostics.run(fake_client, settings)
    assert res["node_red"]["reachable"] is False
    assert "error" in res["node_red"]
    assert "flows" not in res                         # early return


@pytest.mark.asyncio
async def test_diagnostics_flows_failure_is_guarded(fake_client):
    async def _raise(*a, **k):
        raise NRError("flows down")
    fake_client.get_flows = _raise
    settings = Settings(ha_url="", ha_token="")
    res = await diagnostics.run(fake_client, settings)
    assert res["node_red"]["reachable"] is True       # settings still succeeded
    assert res["flows"] is None                        # guarded, snapshot survives


@pytest.mark.asyncio
async def test_diagnostics_ha_reachable(fake_client):
    settings = Settings(ha_url="http://ha:8123", ha_token="tok")

    async def ping_ok(s):
        return {"message": "API running."}

    res = await diagnostics.run(fake_client, settings, ha_ping=ping_ok)
    assert res["home_assistant"]["configured"] is True
    assert res["home_assistant"]["reachable"] is True


@pytest.mark.asyncio
async def test_diagnostics_ha_unreachable(fake_client):
    settings = Settings(ha_url="http://ha:8123", ha_token="tok")

    async def ping_fail(s):
        raise RuntimeError("bad token")

    res = await diagnostics.run(fake_client, settings, ha_ping=ping_fail)
    assert res["home_assistant"]["reachable"] is False
    assert res["home_assistant"]["error"] == "RuntimeError"
