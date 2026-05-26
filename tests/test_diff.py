import copy

import pytest

from nrha_mcp import deploy
from nrha_mcp.backup import BackupStore
from nrha_mcp.client import NRError


def _store(tmp_path):
    return BackupStore(str(tmp_path / "backups"))


@pytest.mark.asyncio
async def test_diff_backup_against_current(fake_client, tmp_path):
    store = _store(tmp_path)
    current, _ = await fake_client.get_flows()          # SAMPLE_FLOWS
    before = copy.deepcopy(current)
    before = [n for n in before if n["id"] != "n3"]      # n3 will appear as "added" (in current, not backup)
    for n in before:
        if n["id"] == "n2":
            n["name"] = "calc-old"                       # n2 will appear as "changed"
    before.append({"id": "extra1", "type": "debug", "z": "tab1", "name": "old dbg", "wires": [[]]})  # "removed"
    path = store.snapshot(before, "t")

    res = await deploy.diff_backup(fake_client, store, path=path)

    assert res["before"] == path
    assert res["after"] == "current"
    assert res["summary"] == {"added": 1, "removed": 1, "changed": 1}
    assert any(a["id"] == "n3" and a["type"] == "api-call-service" for a in res["added"])
    assert any(r["id"] == "extra1" and r["type"] == "debug" for r in res["removed"])
    assert "n2" in res["changed"] and "name" in res["changed"]["n2"]


@pytest.mark.asyncio
async def test_diff_two_backups(fake_client, tmp_path):
    store = _store(tmp_path)
    current, _ = await fake_client.get_flows()
    path_a = store.snapshot(current, "A")
    modified = [n for n in copy.deepcopy(current) if n["id"] != "n1"]   # B lacks n1
    path_b = store.snapshot(modified, "B")

    res = await deploy.diff_backup(fake_client, store, path=path_a, path_b=path_b)

    assert res["before"] == path_a
    assert res["after"] == path_b
    assert any(r["id"] == "n1" for r in res["removed"])   # in A (before), not in B (after)


@pytest.mark.asyncio
async def test_diff_bad_path_raises(fake_client, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(NRError):
        await deploy.diff_backup(fake_client, store, path=str(tmp_path / "nope.json"))
