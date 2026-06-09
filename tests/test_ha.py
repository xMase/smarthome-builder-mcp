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

FAKE_SERVICES = [
    {"domain": "light", "services": {
        "turn_on": {"name": "Turn on", "description": "Turn the light on",
                    "fields": {"brightness_pct": {"description": "Brightness %", "example": 50,
                                                  "required": False,
                                                  "selector": {"number": {"min": 0, "max": 100}}}}},
        "turn_off": {"name": "Turn off", "description": "Turn the light off", "fields": {}},
    }},
    {"domain": "switch", "services": {
        "toggle": {"name": "Toggle", "description": "Toggle the switch", "fields": {}},
    }},
]

FAKE_HISTORY = [
    {"entity_id": "sensor.x", "state": "0", "last_changed": "2026-05-26T08:00:00+00:00"},
    {"entity_id": "sensor.x", "state": "1.2", "last_changed": "2026-05-26T09:00:00+00:00"},
    {"entity_id": "sensor.x", "state": "0", "last_changed": "2026-05-26T10:00:00+00:00"},
]


class FakeHAClient:
    """Minimal fake HA REST client for unit tests."""

    def __init__(self, states=None, state_map=None, services=None, history=None, call_result=None):
        self._states = states or FAKE_STATES
        self._state_map = state_map or {s["entity_id"]: s for s in self._states}
        self._services = services if services is not None else FAKE_SERVICES
        self._history = history if history is not None else FAKE_HISTORY
        self._call_result = call_result if call_result is not None else []
        self.calls = []

    async def get_states(self):
        return list(self._states)

    async def get_state(self, entity_id: str):
        if entity_id not in self._state_map:
            raise KeyError(entity_id)
        return self._state_map[entity_id]

    async def get_services(self):
        return list(self._services)

    async def get_history(self, entity_id, start_iso, end_iso=None):
        self.calls.append(("get_history", entity_id, start_iso, end_iso))
        return [list(self._history)]

    async def call_service(self, domain, service, body):
        self.calls.append((domain, service, body))
        return list(self._call_result)


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


@pytest.mark.asyncio
async def test_get_services_index():
    fake = FakeHAClient()
    res = await ha.get_services(fake)
    assert res["total_domains"] == 2
    assert res["domains"]["light"] == ["turn_off", "turn_on"]   # names sorted
    assert res["domains"]["switch"] == ["toggle"]


@pytest.mark.asyncio
async def test_get_services_domain_detail_trims_fields():
    fake = FakeHAClient()
    res = await ha.get_services(fake, "light")
    assert res["domain"] == "light"
    assert res["total_services"] == 2
    svc = res["services"]["turn_on"]
    assert svc["name"] == "Turn on"
    assert svc["fields"]["brightness_pct"] == {
        "description": "Brightness %", "example": 50, "required": False,
    }  # selector dropped


@pytest.mark.asyncio
async def test_get_services_unknown_domain():
    fake = FakeHAClient()
    res = await ha.get_services(fake, "nope")
    assert res["found"] is False
    assert res["available_domains"] == ["light", "switch"]


@pytest.mark.asyncio
async def test_get_services_index_skips_entry_without_domain():
    fake = FakeHAClient(services=[{"services": {"x": {}}},
                                  {"domain": "light", "services": {"turn_on": {}}}])
    res = await ha.get_services(fake)
    assert res["domains"] == {"light": ["turn_on"]}
    assert res["total_domains"] == 1


@pytest.mark.asyncio
async def test_get_entity_history_basic():
    fake = FakeHAClient()
    res = await ha.get_entity_history(fake, "sensor.x", hours=24)
    assert res["total_changes"] == 3
    assert res["first"]["state"] == "0"
    assert res["last"]["state"] == "0"
    assert len(res["points"]) == 3
    assert "note" not in res


@pytest.mark.asyncio
async def test_get_entity_history_truncates_to_recent():
    many = [{"state": str(i), "last_changed": f"2026-05-26T00:{i:02d}:00+00:00"} for i in range(60)]
    fake = FakeHAClient(history=many)
    res = await ha.get_entity_history(fake, "sensor.x", hours=24, max_points=10)
    assert res["total_changes"] == 60
    assert res["truncated"] is True
    assert len(res["points"]) == 10
    assert res["points"][-1]["state"] == "59"   # most recent kept
    assert res["points"][0]["state"] == "50"
    # first/last reflect the FULL series, not the truncated slice
    assert res["first"]["state"] == "0"
    assert res["last"]["state"] == "59"


@pytest.mark.asyncio
async def test_get_entity_history_empty_has_note():
    fake = FakeHAClient(history=[])
    res = await ha.get_entity_history(fake, "sensor.x", hours=24)
    assert res["total_changes"] == 0
    assert res["points"] == []
    assert res["first"] is None
    assert "note" in res


@pytest.mark.asyncio
async def test_get_entity_history_passes_explicit_end_time_now():
    # Regression: without an explicit end_time, HA's /api/history/period/<start>
    # defaults end to start+24h, so hours>24 silently drops the most recent
    # (hours-24)h. The tool must pass end_time=now and span the full window.
    from datetime import datetime, timezone, timedelta

    fake = FakeHAClient()
    before = datetime.now(timezone.utc)
    await ha.get_entity_history(fake, "sensor.x", hours=60)
    after = datetime.now(timezone.utc)

    _, _, start_iso, end_iso = fake.calls[-1]
    assert end_iso is not None, "end_time must be passed explicitly (else HA caps at start+24h)"

    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    # end is 'now'
    assert before <= end <= after
    # window spans the requested hours, not a fixed 24h
    assert abs((end - start) - timedelta(hours=60)) < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_get_entity_history_large_window_has_note():
    fake = FakeHAClient()
    res = await ha.get_entity_history(fake, "sensor.x", hours=9000)
    assert "note" in res


# --- ha_call_service: config defaults ---

def test_call_service_settings_defaults():
    s = Settings()
    assert s.ha_block_services == ""
    assert s.ha_confirm_services is None


# --- ha_call_service: tier classification ---

def test_tier_floor_blocked():
    s = Settings()
    assert ha._service_tier("homeassistant", "restart", s) == "blocked"
    assert ha._service_tier("hassio", "restore_full", s) == "blocked"

def test_tier_default_confirm_and_direct():
    s = Settings()
    assert ha._service_tier("lock", "unlock", s) == "confirm"
    assert ha._service_tier("alarm_control_panel", "alarm_trigger", s) == "confirm"
    assert ha._service_tier("light", "turn_on", s) == "direct"
    assert ha._service_tier("cover", "open_cover", s) == "direct"

def test_tier_block_is_additive_and_wildcard():
    s = Settings(ha_block_services="cover.*")
    assert ha._service_tier("cover", "open_cover", s) == "blocked"
    assert ha._service_tier("homeassistant", "stop", s) == "blocked"   # floor still applies

def test_tier_confirm_override_replaces_default():
    s = Settings(ha_confirm_services="climate.set_temperature")
    assert ha._service_tier("lock", "unlock", s) == "direct"           # default replaced
    assert ha._service_tier("climate", "set_temperature", s) == "confirm"

def test_tier_confirm_empty_gates_nothing_but_floor_holds():
    s = Settings(ha_confirm_services="")
    assert ha._service_tier("lock", "unlock", s) == "direct"
    assert ha._service_tier("homeassistant", "stop", s) == "blocked"

def test_effective_policy_source():
    assert ha.effective_policy(Settings())["confirm_source"] == "default"
    cfg = ha.effective_policy(Settings(ha_confirm_services="lock.unlock"))
    assert cfg["confirm_source"] == "configured"
    assert cfg["confirm"] == ["lock.unlock"]


# --- ha_call_service: HARestClient.call_service ---

@pytest.mark.asyncio
@respx.mock
async def test_harestclient_call_service_posts_body_and_returns_changed():
    import json as _json
    route = respx.post("http://ha.local/api/services/light/turn_on").mock(
        return_value=httpx.Response(
            200, json=[{"entity_id": "light.k", "state": "on", "attributes": {"brightness": 5}}]))
    c = ha.HARestClient(Settings(ha_url="http://ha.local", ha_token="t"))
    try:
        out = await c.call_service("light", "turn_on", {"entity_id": "light.k"})
    finally:
        await c.close()
    assert route.called
    assert _json.loads(route.calls.last.request.content) == {"entity_id": "light.k"}
    assert out == [{"entity_id": "light.k", "state": "on", "attributes": {"brightness": 5}}]


# --- ha_call_service: orchestrator ---

@pytest.mark.asyncio
async def test_call_service_direct_executes_and_trims():
    fake = FakeHAClient(call_result=[{"entity_id": "light.k", "state": "on",
                                      "attributes": {"brightness": 5}}])
    out = await ha.call_service(fake, Settings(), "light", "turn_on", entity_id="light.k")
    assert fake.calls == [("light", "turn_on", {"entity_id": "light.k"})]
    assert out["ok"] is True and out["called"] == "light.turn_on"
    assert out["changed"] == [{"entity_id": "light.k", "state": "on"}]
    assert out["changed_count"] == 1 and "note" not in out

@pytest.mark.asyncio
async def test_call_service_empty_changed_adds_note():
    fake = FakeHAClient(call_result=[])
    out = await ha.call_service(fake, Settings(), "script", "test", entity_id="script.test")
    assert out["changed"] == [] and out["changed_count"] == 0
    assert "verify with ha_get_state" in out["note"]

@pytest.mark.asyncio
async def test_call_service_blocked_raises_even_with_confirm():
    fake = FakeHAClient()
    with pytest.raises(ha.NRError):
        await ha.call_service(fake, Settings(), "homeassistant", "restart", confirm=True)
    assert fake.calls == []

@pytest.mark.asyncio
async def test_call_service_confirm_tier_previews_without_confirm():
    fake = FakeHAClient()
    out = await ha.call_service(fake, Settings(), "lock", "unlock", entity_id="lock.porta")
    assert out["preview"] is True and out["requires_confirm"] is True
    assert out["would_call"] == "lock.unlock"
    assert fake.calls == []

@pytest.mark.asyncio
async def test_call_service_confirm_tier_executes_with_confirm():
    fake = FakeHAClient(call_result=[{"entity_id": "lock.porta", "state": "unlocked"}])
    out = await ha.call_service(fake, Settings(), "lock", "unlock",
                                entity_id="lock.porta", confirm=True)
    assert out["ok"] is True
    assert fake.calls == [("lock", "unlock", {"entity_id": "lock.porta"})]

@pytest.mark.asyncio
async def test_call_service_entity_id_collision_raises():
    fake = FakeHAClient()
    with pytest.raises(ha.NRError):
        await ha.call_service(fake, Settings(), "light", "turn_on",
                              entity_id="light.a", data={"entity_id": "light.b"})
    assert fake.calls == []

@pytest.mark.asyncio
async def test_call_service_entity_id_agreeing_in_data_is_accepted():
    fake = FakeHAClient(call_result=[{"entity_id": "light.a", "state": "on"}])
    out = await ha.call_service(fake, Settings(), "light", "turn_on",
                                entity_id="light.a", data={"entity_id": "light.a"})
    assert out["ok"] is True
    assert fake.calls == [("light", "turn_on", {"entity_id": "light.a"})]


@pytest.mark.asyncio
async def test_call_service_ha_http_error_surfaces_as_nrerror():
    class FailingHAClient(FakeHAClient):
        async def call_service(self, domain, service, body):
            req = httpx.Request("POST", f"http://ha.local/api/services/{domain}/{service}")
            resp = httpx.Response(400, request=req, text="Service light.turn_on requires return_response")
            raise httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)

    fake = FailingHAClient()
    with pytest.raises(ha.NRError) as exc:
        await ha.call_service(fake, Settings(), "light", "turn_on", entity_id="light.k")
    msg = str(exc.value)
    assert "400" in msg
    assert "requires return_response" in msg   # HA's actual message is preserved
