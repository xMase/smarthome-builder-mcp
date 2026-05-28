# tests/test_flows.py
import pytest

from nrha_mcp import flows
from nrha_mcp.client import NRNotFoundError

DISABLED_FLOWS = [
    {"id": "tabOn", "type": "tab", "label": "Active"},
    {"id": "tabOff", "type": "tab", "label": "Parked", "disabled": True},
    {"id": "nOn", "type": "inject", "z": "tabOn", "name": "live", "wires": [[]]},
    {"id": "nOff", "type": "inject", "z": "tabOff", "name": "on parked tab", "wires": [[]]},
    {"id": "nDis", "type": "function", "z": "tabOn", "name": "disabled node",
     "d": True, "func": "", "wires": [[]]},
    {"id": "sfdef", "type": "subflow", "name": "SF"},
    {"id": "nInternal", "type": "change", "z": "sfdef", "name": "inside sf", "wires": [[]]},
]


@pytest.fixture
def dis_client():
    from tests.conftest import FakeNRClient
    return FakeNRClient(flows=DISABLED_FLOWS)


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
async def test_get_flow_unknown_raises(fake_client):
    with pytest.raises(NRNotFoundError):
        await flows.get_flow(fake_client, "nope")


@pytest.mark.asyncio
async def test_global_context_lists_keys_grouped_by_store(fake_client):
    fake_client.global_context = {"default": {"scheme": {"msg": 1}}, "store": {}}
    res = await flows.get_global_context(fake_client, None)
    assert res["scope"] == "global"
    assert res["stores"] == {"default": ["scheme"], "store": []}


@pytest.mark.asyncio
async def test_global_context_lookup_prefers_default(fake_client):
    fake_client.global_context = {"default": {"k": {"msg": "v"}}, "store": {}}
    res = await flows.get_global_context(fake_client, "k")
    assert res["store"] == "default"
    assert res["value"] == "v"


@pytest.mark.asyncio
async def test_flow_context_multi_store(fake_client):
    fake_client.context["tab1"] = {"default": {"a": {"msg": 1}}, "store": {"b": {"msg": 2}}}
    res = await flows.get_flow_context(fake_client, "tab1", None)
    assert res["flow_id"] == "tab1"
    assert res["stores"]["default"] == ["a"]
    assert res["stores"]["store"] == ["b"]


@pytest.mark.asyncio
async def test_context_key_not_found_raises(fake_client):
    fake_client.global_context = {"default": {}, "store": {}}
    with pytest.raises(NRNotFoundError):
        await flows.get_global_context(fake_client, "missing")


# --- subflow fixtures/tests -------------------------------------------------

SUBFLOW_FLOWS = [
    {"id": "tabA", "type": "tab", "label": "Main"},
    {
        "id": "sfdef", "type": "subflow", "name": "seasonSwitch", "color": "#DDAA99",
        "in": [{"wires": [{"id": "getS"}]}],
        "out": [
            {"wires": [{"id": "nest", "port": 0}]},
            {"wires": [{"id": "nest", "port": 1}]},
        ],
        "status": {"wires": [{"id": "chg", "port": 0}]},
        "outputLabels": ["inverno", "estate"],
        "env": [],
    },
    {"id": "getS", "type": "api-current-state", "z": "sfdef", "name": "getSeason",
     "entity_id": "select.season", "wires": [["nest", "chg"]]},
    {"id": "nest", "type": "subflow:nestdef", "z": "sfdef", "name": "", "wires": [[], []]},
    {"id": "chg", "type": "change", "z": "sfdef", "name": "", "wires": [[]]},
    {"id": "nestdef", "type": "subflow", "name": "strSeasonSwitch",
     "in": [{"wires": [{"id": "sw"}]}],
     "out": [{"wires": [{"id": "sw", "port": 0}]}, {"wires": [{"id": "sw", "port": 1}]}],
     "outputLabels": ["inverno", "estate"]},
    {"id": "sw", "type": "switch", "z": "nestdef", "name": "", "wires": [[], []]},
    {"id": "inst1", "type": "subflow:sfdef", "z": "tabA", "name": "seasonSwitch",
     "wires": [[], ["dn1"]]},
    {"id": "dn1", "type": "debug", "z": "tabA", "name": "", "wires": [[]]},
]


@pytest.fixture
def sf_client():
    from tests.conftest import FakeNRClient
    return FakeNRClient(flows=SUBFLOW_FLOWS)


@pytest.mark.asyncio
async def test_get_subflow_outputs_resolved_with_labels(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    assert res["subflow"]["name"] == "seasonSwitch"
    assert [o["label"] for o in res["outputs"]] == ["inverno", "estate"]
    assert res["outputs"][1]["sources"] == [{"node_id": "nest", "port": 1}]
    assert res["status"]["sources"] == [{"node_id": "chg", "port": 0}]
    assert res["inputs"][0]["wires"] == [{"node_id": "getS"}]


@pytest.mark.asyncio
async def test_get_subflow_internal_node_annotations(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    by_id = {n["id"]: n for n in res["nodes"]}
    assert by_id["getS"]["_targets"] == ["nest", "chg"]
    assert by_id["nest"]["_feeds_outputs"] == [0, 1]
    assert by_id["chg"]["_feeds_status"] is True
    assert by_id["nest"]["_nested_subflow"] == {"def_id": "nestdef", "def_name": "strSeasonSwitch"}
    assert res["total_nodes"] == 3


@pytest.mark.asyncio
async def test_get_subflow_nested_pointer_not_expanded(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    assert res["nested_subflows"] == [
        {"instance_node_id": "nest", "def_id": "nestdef", "def_name": "strSeasonSwitch"}
    ]


@pytest.mark.asyncio
async def test_get_subflow_lists_instances_with_wired_outputs(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    assert res["instances"] == [
        {"node_id": "inst1", "flow_id": "tabA", "flow_label": "Main", "wired_outputs": [1]}
    ]


@pytest.mark.asyncio
async def test_resolve_subflow_skips_wire_without_id():
    from tests.conftest import FakeNRClient
    flows_with_bad_wire = [
        {"id": "sf", "type": "subflow", "name": "x",
         "in": [], "out": [{"wires": [{"port": 0}, {"id": "ok", "port": 1}]}],
         "outputLabels": ["o0"]},
        {"id": "ok", "type": "debug", "z": "sf", "wires": [[]]},
    ]
    res = await flows.get_subflow(FakeNRClient(flows=flows_with_bad_wire), "sf")
    assert res["outputs"][0]["sources"] == [{"node_id": "ok", "port": 1}]


@pytest.mark.asyncio
async def test_get_subflow_by_name(sf_client):
    res = await flows.get_subflow(sf_client, "seasonswitch")   # case-insensitive
    assert res["subflow"]["id"] == "sfdef"


@pytest.mark.asyncio
async def test_get_subflow_by_instance_id(sf_client):
    res = await flows.get_subflow(sf_client, "inst1")          # instance -> its definition
    assert res["subflow"]["id"] == "sfdef"


@pytest.mark.asyncio
async def test_get_subflow_unknown_raises(sf_client):
    with pytest.raises(NRNotFoundError):
        await flows.get_subflow(sf_client, "does-not-exist")


@pytest.mark.asyncio
async def test_node_config_on_subflow_definition(sf_client):
    res = await flows.get_node_config(sf_client, "sfdef")
    assert set(res["_downstream"]) == {"nest", "chg"}
    assert res["_ports"]["inputs"] == 1
    assert res["_ports"]["has_status"] is True
    assert {o["label"] for o in res["_ports"]["outputs"]} == {"inverno", "estate"}


@pytest.mark.asyncio
async def test_node_config_on_internal_node_feeding_port(sf_client):
    res = await flows.get_node_config(sf_client, "nest")
    assert res["_feeds_outputs"] == [0, 1]
    assert res["_subflow_def"] == {"id": "nestdef", "name": "strSeasonSwitch"}  # also an instance


@pytest.mark.asyncio
async def test_node_config_on_subflow_instance(sf_client):
    res = await flows.get_node_config(sf_client, "inst1")
    assert res["_subflow_def"] == {"id": "sfdef", "name": "seasonSwitch"}


@pytest.mark.asyncio
async def test_summary_includes_subflows(sf_client):
    res = await flows.get_flow_summary(sf_client)
    by_id = {s["id"]: s for s in res["subflows"]}
    assert by_id["sfdef"]["name"] == "seasonSwitch"
    assert by_id["sfdef"]["node_count"] == 3
    assert by_id["sfdef"]["instance_count"] == 1
    assert by_id["nestdef"]["node_count"] == 1
    assert by_id["nestdef"]["instance_count"] == 1   # 'nest' inside sfdef


@pytest.mark.asyncio
async def test_search_finds_subflow_definition(sf_client):
    res = await flows.search_nodes(sf_client, query="seasonSwitch")
    ids = {r["id"] for r in res["results"]}
    assert "sfdef" in ids        # the definition node is now searchable


@pytest.mark.asyncio
async def test_search_labels_internal_node_with_subflow_name(sf_client):
    res = await flows.search_nodes(sf_client, query="getSeason")
    hit = next(r for r in res["results"] if r["id"] == "getS")
    assert hit["flow_label"] == "seasonSwitch"


@pytest.mark.asyncio
async def test_node_config_flow_label_uses_subflow_name(sf_client):
    # an internal subflow node reports its subflow's name as _flow_label,
    # consistent with search_nodes (not an empty string)
    res = await flows.get_node_config(sf_client, "getS")
    assert res["_flow_label"] == "seasonSwitch"


# --- disabled-state visibility tests ----------------------------------------

@pytest.mark.asyncio
async def test_summary_marks_disabled_tabs(dis_client):
    res = await flows.get_flow_summary(dis_client)
    by_id = {t["id"]: t for t in res["tabs"]}
    assert by_id["tabOff"]["disabled"] is True
    assert by_id["tabOn"]["disabled"] is False


@pytest.mark.asyncio
async def test_search_surfaces_disabled_state(dis_client):
    res = await flows.search_nodes(dis_client, query="")
    by_id = {r["id"]: r for r in res["results"]}
    assert by_id["nOff"]["flow_disabled"] is True
    assert by_id["nOn"]["flow_disabled"] is False
    assert by_id["nDis"]["node_disabled"] is True
    assert by_id["nOn"]["node_disabled"] is False


@pytest.mark.asyncio
async def test_node_config_surfaces_disabled(dis_client):
    on_off_tab = await flows.get_node_config(dis_client, "nOff")
    assert on_off_tab["_flow_disabled"] is True
    dnode = await flows.get_node_config(dis_client, "nDis")
    assert dnode["_disabled"] is True
    assert dnode["_flow_disabled"] is False
    internal = await flows.get_node_config(dis_client, "nInternal")
    assert internal["_flow_disabled"] is False


# --- nr_export_flow: _resolve_tab helper ---

def test_resolve_tab_by_id_and_name():
    from nrha_mcp.flows import _resolve_tab
    flows_list = [
        {"id": "tabA", "type": "tab", "label": "Heating"},
        {"id": "tabB", "type": "tab", "label": "Cooling"},
    ]
    assert _resolve_tab(flows_list, "tabA")["id"] == "tabA"
    assert _resolve_tab(flows_list, "cool")["id"] == "tabB"

def test_resolve_tab_ambiguous_and_missing():
    from nrha_mcp.flows import _resolve_tab
    flows_list = [
        {"id": "t1", "type": "tab", "label": "Solar east"},
        {"id": "t2", "type": "tab", "label": "Solar west"},
    ]
    with pytest.raises(NRNotFoundError):
        _resolve_tab(flows_list, "solar")      # ambiguous
    with pytest.raises(NRNotFoundError):
        _resolve_tab(flows_list, "nope")       # missing
