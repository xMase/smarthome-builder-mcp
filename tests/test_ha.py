# tests/test_ha.py
import pytest
import respx
import httpx

from nrha_mcp import ha
from nrha_mcp.config import Settings


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


# ---------------------------------------------------------------------------
# HARestClient + search_entities + get_entity_state
# ---------------------------------------------------------------------------

FAKE_STATES = [
    {
        "entity_id": "sensor.bagno_humidity",
        "state": "82",
        "attributes": {"friendly_name": "Bagno umidità"},
    },
    {
        "entity_id": "light.luce_bagno",
        "state": "on",
        "attributes": {},
    },
]


class FakeHAClient:
    """Minimal fake HA REST client for unit tests."""

    def __init__(self, states=None, state_map=None):
        self._states = states or FAKE_STATES
        self._state_map = state_map or {s["entity_id"]: s for s in self._states}

    async def get_states(self):
        return list(self._states)

    async def get_state(self, entity_id: str):
        if entity_id not in self._state_map:
            raise KeyError(entity_id)
        return self._state_map[entity_id]


@pytest.mark.asyncio
async def test_search_entities_matches_id_and_name():
    fake = FakeHAClient()

    # "umid" matches friendly_name "Bagno umidità" → only humidity sensor
    result = await ha.search_entities(fake, "umid")
    assert result["total"] == 1
    assert result["results"][0]["entity_id"] == "sensor.bagno_humidity"
    assert result["query"] == "umid"

    # "bagno" matches both entity_id (luce_bagno) and friendly_name (Bagno umidità)
    result2 = await ha.search_entities(fake, "bagno")
    ids = {r["entity_id"] for r in result2["results"]}
    assert ids == {"sensor.bagno_humidity", "light.luce_bagno"}
    assert result2["total"] == 2


@pytest.mark.asyncio
async def test_get_entity_state():
    fake_state = {
        "entity_id": "sensor.bagno_humidity",
        "state": "82",
        "attributes": {"friendly_name": "Bagno umidità", "unit_of_measurement": "%"},
    }
    fake = FakeHAClient(
        states=[fake_state],
        state_map={"sensor.bagno_humidity": fake_state},
    )

    result = await ha.get_entity_state(fake, "sensor.bagno_humidity")
    assert result["entity_id"] == "sensor.bagno_humidity"
    assert result["state"] == "82"
    assert result["attributes"]["unit_of_measurement"] == "%"


@pytest.mark.asyncio
@respx.mock
async def test_harestclient_get_states():
    states_payload = [
        {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Temp"}},
    ]
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=states_payload)
    )

    settings = Settings(ha_url="http://ha.test", ha_token="t", _env_file=None)
    client = ha.HARestClient(settings)
    try:
        result = await client.get_states()
    finally:
        await client.close()

    assert result == states_payload
    # Verify Authorization header was sent
    req = respx.calls.last.request
    assert req.headers["authorization"] == "Bearer t"
