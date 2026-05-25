# tests/test_flows.py
import pytest

from nrha_mcp import flows
from nrha_mcp.client import NRNotFoundError


@pytest.mark.asyncio
async def test_summary_counts(fake_client):
    res = await flows.get_flow_summary(fake_client)
    assert res["total_tabs"] == 1
    assert res["tabs"][0]["label"] == "Heating"
    assert res["tabs"][0]["node_count"] == 3


@pytest.mark.asyncio
async def test_get_flow_by_name(fake_client):
    res = await flows.get_flow(fake_client, "heating")
    assert res["tab"]["id"] == "tab1"
    assert res["total_nodes"] == 3


@pytest.mark.asyncio
async def test_get_flow_fields_filter(fake_client):
    res = await flows.get_flow(fake_client, "tab1", fields=["name"])
    n = next(x for x in res["nodes"] if x["id"] == "n2")
    assert set(n.keys()) <= {"id", "type", "z", "name"}   # identity always kept


@pytest.mark.asyncio
async def test_search_by_type(fake_client):
    res = await flows.search_nodes(fake_client, query="", node_type="function")
    # empty query matches the 'name'/'type' fields containing '' -> matches; filter by type
    assert all(r["type"] == "function" for r in res["results"])


@pytest.mark.asyncio
async def test_function_code(fake_client):
    res = await flows.get_function_code(fake_client, "n2")
    assert res["code"] == "return msg;"


@pytest.mark.asyncio
async def test_global_context_lists_keys(fake_client):
    fake_client.global_context = {"memory": {"scheme": {"msg": 1}}}
    res = await flows.get_global_context(fake_client, None)
    assert res["keys"] == ["scheme"]


@pytest.mark.asyncio
async def test_get_flow_unknown_raises(fake_client):
    with pytest.raises(NRNotFoundError):
        await flows.get_flow(fake_client, "nope")
