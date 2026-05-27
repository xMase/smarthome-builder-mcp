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
