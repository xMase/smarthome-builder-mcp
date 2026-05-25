# tests/test_ha.py
import pytest

from nrha_mcp import ha


@pytest.mark.asyncio
async def test_list_ha_nodes(fake_client):
    res = await ha.list_ha_nodes(fake_client)
    ids = [n["id"] for n in res["ha_nodes"]]
    assert "n3" in ids                       # api-call-service node
    n3 = next(n for n in res["ha_nodes"] if n["id"] == "n3")
    assert n3["domain"] == "switch" and n3["service"] == "turn_on"
    assert n3["entity"] == "switch.boiler"


@pytest.mark.asyncio
async def test_get_server_config(fake_client):
    res = await ha.get_server_config(fake_client)
    assert res["servers"][0]["id"] == "srv1"


class FakeHA:
    def __init__(self, entities, services):
        self._entities = entities
        self._services = services
    async def list_entities(self):
        return self._entities
    async def list_services(self):
        return self._services


@pytest.mark.asyncio
async def test_validate_flags_missing_entity(fake_client):
    hac = FakeHA(entities={"switch.other"}, services={"switch.turn_on"})
    res = await ha.validate_calls(fake_client, hac)
    problems = {p["node_id"]: p for p in res["problems"]}
    assert "n3" in problems
    assert "switch.boiler" in problems["n3"]["missing_entity"]


@pytest.mark.asyncio
async def test_validate_ok_when_present(fake_client):
    hac = FakeHA(entities={"switch.boiler"}, services={"switch.turn_on"})
    res = await ha.validate_calls(fake_client, hac)
    assert res["problems"] == []
