"""Filesystem backup store: snapshot flows before any deploy; list & restore."""

import json
from datetime import datetime, timezone
from pathlib import Path


class BackupStore:
    def __init__(self, backup_dir: str):
        self.dir = Path(backup_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self, flows: list[dict], label: str = "") -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = self.dir / f"flows-{ts}.json"
        path.write_text(json.dumps(
            {"saved_at": ts, "label": label, "flows": flows},
            indent=2, ensure_ascii=False,
        ))
        return path

    def list_backups(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("flows-*.json")):
            meta = json.loads(p.read_text())
            out.append({"path": str(p), "saved_at": meta.get("saved_at"), "label": meta.get("label", "")})
        return out

    def restore(self, path: str) -> list[dict]:
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"Cannot read backup '{path}': {e}")
        flows = data.get("flows")
        if not isinstance(flows, list):
            raise ValueError(f"Backup '{path}' has no valid 'flows' list")
        return flows
