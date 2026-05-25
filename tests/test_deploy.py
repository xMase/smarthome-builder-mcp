import pytest

from nrha_mcp.deploy import diff_flows, commit


def test_diff_detects_add_remove_change():
    before = [{"id": "a", "name": "x"}, {"id": "b"}]
    after = [{"id": "a", "name": "y"}, {"id": "c"}]
    d = diff_flows(before, after)
    assert d["added"] == ["c"]
    assert d["removed"] == ["b"]
    assert d["changed"] == {"a": {"name": {"before": "x", "after": "y"}}}


class StubStore:
    def __init__(self):
        self.snapshots = []
    def snapshot(self, flows, label):
        self.snapshots.append((flows, label))
        return f"/backups/{len(self.snapshots)}.json"


@pytest.mark.asyncio
async def test_propose_mode_does_not_write(fake_client, settings_propose):
    store = StubStore()
    def rebuild(flows):
        out = [dict(f) for f in flows]
        out.append({"id": "new", "type": "debug", "z": "tab1"})
        return out
    res = await commit(fake_client, store, settings_propose,
                       rebuild=rebuild, summary="add debug", confirm=False)
    assert res["deployed"] is False
    assert res["preview"]["added"] == ["new"]
    assert store.snapshots == []          # no backup taken on preview
    assert fake_client.post_calls == []    # nothing posted


@pytest.mark.asyncio
async def test_confirm_writes_and_backs_up(fake_client, settings_propose):
    store = StubStore()
    def rebuild(flows):
        out = [dict(f) for f in flows]
        out.append({"id": "new", "type": "debug", "z": "tab1"})
        return out
    res = await commit(fake_client, store, settings_propose,
                       rebuild=rebuild, summary="add debug", confirm=True)
    assert res["deployed"] is True
    assert len(store.snapshots) == 1       # backup before write
    assert len(fake_client.post_calls) == 1
    assert "new" in res["changes"]["added"]


@pytest.mark.asyncio
async def test_auto_mode_writes_without_confirm(fake_client, settings_auto):
    store = StubStore()
    rebuild = lambda flows: [dict(f) for f in flows]
    res = await commit(fake_client, store, settings_auto,
                       rebuild=rebuild, summary="noop", confirm=False)
    assert res["deployed"] is True


@pytest.mark.asyncio
async def test_conflict_triggers_single_retry(settings_auto):
    # client whose rev changes once between read and write, then settles
    from tests.conftest import FakeNRClient
    client = FakeNRClient()
    store = StubStore()
    calls = {"n": 0}
    real_post = client.post_flows
    async def flaky_post(flows, rev):
        calls["n"] += 1
        if calls["n"] == 1:
            client._rev = "moved"          # simulate concurrent change
            raise __import__("nrha_mcp.client", fromlist=["NRConflictError"]).NRConflictError("x")
        return await real_post(flows, rev)
    client.post_flows = flaky_post
    rebuild = lambda flows: [dict(f) for f in flows]
    res = await commit(client, store, settings_auto, rebuild=rebuild, summary="retry", confirm=True)
    assert res["deployed"] is True
    assert calls["n"] == 2                  # retried once after conflict
