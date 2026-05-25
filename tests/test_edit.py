# tests/test_edit.py
import pytest

from nrha_mcp import edit
from nrha_mcp.client import NRError, NRNotFoundError


@pytest.mark.asyncio
async def test_safe_deploy_preview_only(fake_client, settings_propose, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.safe_deploy(fake_client, store, settings_propose,
                                 node_id="n2", fields={"name": "calc2"}, confirm=False)
    assert res["deployed"] is False
    assert res["preview"]["changed"]["n2"]["name"]["after"] == "calc2"
    assert fake_client.post_calls == []


@pytest.mark.asyncio
async def test_safe_deploy_confirm_writes(fake_client, settings_propose, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.safe_deploy(fake_client, store, settings_propose,
                                 node_id="n2", fields={"name": "calc2"}, confirm=True)
    assert res["deployed"] is True
    assert len(fake_client.post_calls) == 1


@pytest.mark.asyncio
async def test_safe_deploy_rejects_immutable(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await edit.safe_deploy(fake_client, store, settings_auto,
                               node_id="n2", fields={"z": "other"}, confirm=True)


@pytest.mark.asyncio
async def test_add_wire_idempotent(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    # n2 already wires to n3; adding again is a no-op
    res = await edit.add_wire(fake_client, store, settings_auto,
                              from_id="n2", output_index=0, to_id="n3", confirm=True)
    assert res.get("already_existed") is True
    assert fake_client.post_calls == []


@pytest.mark.asyncio
async def test_safe_deploy_many(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.safe_deploy_many(fake_client, store, settings_auto, updates=[
        {"node_id": "n2", "fields": {"name": "a"}},
        {"node_id": "n3", "fields": {"name": "b"}},
    ], confirm=True)
    assert res["deployed"] is True
    assert len(fake_client.post_calls) == 1   # single atomic deploy


@pytest.mark.asyncio
async def test_create_nodes_validates_and_writes(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.create_nodes(fake_client, store, settings_auto,
        nodes=[{"id": "n9", "type": "debug", "z": "tab1"}], confirm=True)
    assert res["deployed"] is True
    flows, _ = await fake_client.get_flows()
    assert any(f["id"] == "n9" for f in flows)


@pytest.mark.asyncio
async def test_create_nodes_duplicate_id_rejected(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await edit.create_nodes(fake_client, store, settings_auto,
            nodes=[{"id": "n2", "type": "debug", "z": "tab1"}], confirm=True)


@pytest.mark.asyncio
async def test_delete_nodes_cleans_refs(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.delete_nodes(fake_client, store, settings_auto,
        node_ids=["n3"], confirm=True)
    assert res["deployed"] is True
    flows, _ = await fake_client.get_flows()
    # n3 removed; group g1 no longer lists it; n2 wires no longer reference it
    g1 = next(f for f in flows if f["id"] == "g1")
    n2 = next(f for f in flows if f["id"] == "n2")
    assert "n3" not in g1["nodes"]
    assert all("n3" not in out for out in n2["wires"])


@pytest.mark.asyncio
async def test_delete_tab_refused(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await edit.delete_nodes(fake_client, store, settings_auto, node_ids=["tab1"], confirm=True)
