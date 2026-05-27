# tests/test_subflow_edit.py
import pytest

from nrha_mcp import subflow_edit
from nrha_mcp.backup import BackupStore
from nrha_mcp.client import NRError, NRNotFoundError
from tests.conftest import FakeNRClient

SF_FLOWS = [
    {"id": "tab1", "type": "tab", "label": "Main"},
    {"id": "sf", "type": "subflow", "name": "mySub",
     "in": [{"x": 80, "y": 80, "wires": [{"id": "inode"}]}],
     "out": [
         {"x": 600, "y": 40, "wires": [{"id": "inode", "port": 0}]},
         {"x": 600, "y": 100, "wires": [{"id": "inode", "port": 1}]},
     ],
     "outputLabels": ["a", "b"]},
    {"id": "inode", "type": "function", "z": "sf", "name": "fn", "wires": [[], []]},
    {"id": "inst1", "type": "subflow:sf", "z": "tab1", "name": "i1", "wires": [["d1"], []]},
    {"id": "inst2", "type": "subflow:sf", "z": "tab1", "name": "i2", "wires": [[], []]},
    {"id": "d1", "type": "debug", "z": "tab1", "wires": [[]]},
]


def _client():
    return FakeNRClient(flows=SF_FLOWS)


@pytest.mark.asyncio
async def test_instantiate_creates_instance_with_correct_slots(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.instantiate_subflow(
        client, store, settings_auto,
        subflow="mySub", z="tab1", name="i3",
        env=[{"name": "X", "value": "1"}], confirm=True)
    assert res["deployed"] is True
    new_id = res["node_id"]
    flows, _ = await client.get_flows()
    node = next(f for f in flows if f["id"] == new_id)
    assert node["type"] == "subflow:sf"
    assert node["z"] == "tab1"
    assert node["name"] == "i3"
    assert node["wires"] == [[], []]          # one empty slot per output (2 outputs)
    assert node["env"] == [{"name": "X", "value": "1"}]


@pytest.mark.asyncio
async def test_instantiate_unknown_tab_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.instantiate_subflow(
            client, store, settings_auto, subflow="mySub", z="nope", confirm=True)


@pytest.mark.asyncio
async def test_instantiate_unknown_subflow_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRNotFoundError):
        await subflow_edit.instantiate_subflow(
            client, store, settings_auto, subflow="ghost", z="tab1", confirm=True)


@pytest.mark.asyncio
async def test_add_port_syncs_definition_and_all_instances(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.add_subflow_port(
        client, store, settings_auto, subflow="sf", label="c", confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == "sf")
    assert len(sf["out"]) == 3
    assert sf["outputLabels"] == ["a", "b", "c"]
    for iid in ("inst1", "inst2"):
        inst = next(f for f in flows if f["id"] == iid)
        assert len(inst["wires"]) == 3
        assert inst["wires"][2] == []          # new slot present and empty


@pytest.mark.asyncio
async def test_add_port_with_source_wires_internal_node(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    await subflow_edit.add_subflow_port(
        client, store, settings_auto, subflow="sf",
        source={"node_id": "inode", "port": 1}, confirm=True)
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == "sf")
    assert sf["out"][2]["wires"] == [{"id": "inode", "port": 1}]


@pytest.mark.asyncio
async def test_add_port_bad_source_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        # d1 is on tab1, not an internal node of subflow sf
        await subflow_edit.add_subflow_port(
            client, store, settings_auto, subflow="sf",
            source={"node_id": "d1"}, confirm=True)


@pytest.mark.asyncio
async def test_add_port_propose_mode_writes_nothing(settings_propose, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.add_subflow_port(
        client, store, settings_propose, subflow="sf", label="c", confirm=False)
    assert res["deployed"] is False
    assert "preview" in res
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_remove_port_syncs_definition_and_all_instances(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.remove_subflow_port(
        client, store, settings_auto, subflow="sf", index=0, confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == "sf")
    assert len(sf["out"]) == 1
    assert sf["outputLabels"] == ["b"]
    inst1 = next(f for f in flows if f["id"] == "inst1")
    assert inst1["wires"] == [[]]            # old slot 0 (->d1) gone; slot 1 ([]) remains


@pytest.mark.asyncio
async def test_remove_port_out_of_range_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.remove_subflow_port(
            client, store, settings_auto, subflow="sf", index=5, confirm=True)


@pytest.mark.asyncio
async def test_delete_refuses_with_instances(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.delete_subflow(
            client, store, settings_auto, subflow="sf", cascade=False, confirm=True)
    assert client.post_calls == []           # nothing written


@pytest.mark.asyncio
async def test_delete_cascade_removes_everything(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.delete_subflow(
        client, store, settings_auto, subflow="sf", cascade=True, confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    ids = {f["id"] for f in flows}
    assert ids == {"tab1", "d1"}             # def sf, inode, inst1, inst2 all removed


@pytest.mark.asyncio
async def test_delete_no_instances_removes_def_and_internals(settings_auto, tmp_path):
    flows0 = [
        {"id": "tab1", "type": "tab", "label": "Main"},
        {"id": "sf", "type": "subflow", "name": "mySub", "in": [], "out": [], "outputLabels": []},
        {"id": "inode", "type": "function", "z": "sf", "wires": [[]]},
    ]
    client = FakeNRClient(flows=flows0)
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.delete_subflow(
        client, store, settings_auto, subflow="sf", confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    assert {f["id"] for f in flows} == {"tab1"}
