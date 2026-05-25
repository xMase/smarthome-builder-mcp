# tests/test_backup.py
import json

from nrha_mcp.backup import BackupStore


def test_snapshot_writes_json(tmp_path):
    store = BackupStore(str(tmp_path))
    flows = [{"id": "a"}]
    path = store.snapshot(flows, label="my change")
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved["flows"] == flows
    assert saved["label"] == "my change"


def test_list_and_restore(tmp_path):
    store = BackupStore(str(tmp_path))
    p = store.snapshot([{"id": "a"}], label="x")
    listed = store.list_backups()
    assert len(listed) == 1 and listed[0]["path"] == str(p)
    restored = store.restore(str(p))
    assert restored == [{"id": "a"}]
