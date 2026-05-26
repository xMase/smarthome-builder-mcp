from nrha_mcp.validate import validate_flows


def _tab(id="t1"):
    return {"id": id, "type": "tab"}


def test_valid_flow():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1", "wires": [[]]}]
    res = validate_flows(flows)
    assert res["valid"] is True
    assert res["errors"] == []
    assert res["checked"] == 2


def test_duplicate_id():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1"},
             {"id": "n1", "type": "debug", "z": "t1"}]
    res = validate_flows(flows)
    assert res["valid"] is False
    assert any(e["code"] == "duplicate_id" for e in res["errors"])


def test_wire_to_missing():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1", "wires": [["ghost"]]}]
    res = validate_flows(flows)
    assert any(e["code"] == "wire_to_missing" and e["node_id"] == "n1" for e in res["errors"])


def test_bad_z():
    flows = [{"id": "n1", "type": "function", "z": "nope", "wires": [[]]}]
    res = validate_flows(flows)
    assert any(e["code"] == "bad_z" for e in res["errors"])


def test_missing_type():
    flows = [_tab(), {"id": "n1", "z": "t1"}]
    res = validate_flows(flows)
    assert any(e["code"] == "missing_type" for e in res["errors"])


def test_group_member_missing():
    flows = [_tab(), {"id": "g1", "type": "group", "z": "t1", "nodes": ["ghost"]}]
    res = validate_flows(flows)
    assert any(e["code"] == "group_member_missing" for e in res["errors"])


def test_unregistered_type_is_warning():
    flows = [_tab(), {"id": "n1", "type": "weird-node", "z": "t1", "wires": [[]]}]
    res = validate_flows(flows, registered_types={"function", "debug"})
    assert res["valid"] is True  # warnings don't fail
    assert any(w["code"] == "unregistered_type" for w in res["warnings"])


def test_subflow_instance_and_structural_types_exempt_from_type_warning():
    flows = [_tab(),
             {"id": "n1", "type": "function", "z": "t1", "wires": [[]]},
             {"id": "n2", "type": "subflow:abc", "z": "t1", "wires": [[]]}]
    res = validate_flows(flows, registered_types={"function"})
    assert not any(w["code"] == "unregistered_type" for w in res["warnings"])


def test_server_ref_missing_is_warning():
    flows = [_tab(), {"id": "n1", "type": "server-state-changed", "z": "t1",
                      "server": "ghost", "wires": [[]]}]
    res = validate_flows(flows)
    assert any(w["code"] == "server_ref_missing" for w in res["warnings"])


def test_valid_server_ref_no_warning():
    flows = [_tab(),
             {"id": "srv1", "type": "server", "z": "", "name": "HA"},
             {"id": "n1", "type": "server-state-changed", "z": "t1",
              "server": "srv1", "wires": [[]]}]
    res = validate_flows(flows)
    assert not any(w["code"] == "server_ref_missing" for w in res["warnings"])


def test_focus_ids_marks_findings():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1", "wires": [["ghost"]]}]
    res = validate_flows(flows, focus_ids={"n1"})
    err = next(e for e in res["errors"] if e["code"] == "wire_to_missing")
    assert err.get("focus") is True


import pytest

from nrha_mcp import validate


@pytest.mark.asyncio
async def test_validate_flow_audits_current(fake_client):
    # conftest SAMPLE_FLOWS is structurally valid
    res = await validate.validate_flow(fake_client)
    assert res["valid"] is True


@pytest.mark.asyncio
async def test_validate_flow_merges_proposed_nodes(fake_client):
    new = [{"id": "x1", "type": "function", "z": "tab1", "wires": [["ghost"]]}]
    res = await validate.validate_flow(fake_client, nodes=new)
    assert res["valid"] is False
    err = next(e for e in res["errors"] if e["code"] == "wire_to_missing")
    assert err["node_id"] == "x1"
    assert err.get("focus") is True


@pytest.mark.asyncio
async def test_validate_flow_uses_registered_types(fake_client):
    fake_client.nodes = [{"types": ["function", "debug"]}]
    new = [{"id": "x1", "type": "mystery", "z": "tab1", "wires": [[]]}]
    res = await validate.validate_flow(fake_client, nodes=new)
    assert any(w["code"] == "unregistered_type" and w["node_id"] == "x1"
               for w in res["warnings"])
