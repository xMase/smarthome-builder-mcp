"""ESPHome Device Builder over Home Assistant ingress (esphome_* tools).

Each call opens the HA websocket (auth with HA_TOKEN), reads the add-on's
ingress_url, asks for an ingress session, then opens a second websocket to the
Device Builder's /ws through the ingress proxy with the ingress_session cookie.
Both transports sit behind injectable connect callables so tests need no network.
"""
import asyncio
import collections
import contextlib
import difflib
import hashlib
import itertools
import re
from datetime import datetime, timezone
from pathlib import Path

from . import ha_ws
from .client import NRAuthError, NRConflictError, NRError, NRNotFoundError


class ESPHomeError(NRError):
    """Device Builder / ingress failure (an expected, surfaced error)."""


_NAME_RE = re.compile(r"[^/\\.\x00][^/\\\x00]*\.ya?ml")
_SECRETS = {"secrets.yaml", "secrets.yml"}


def check_configuration(name, settings) -> str:
    """Bare *.yaml/*.yml name, no '/', '\\' or leading '.'; secrets blocked unless allowed."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ESPHomeError(
            f"invalid configuration name {name!r}: use a bare file name ending in .yaml "
            "or .yml, without '/' and without a leading '.'")
    if name.lower() in _SECRETS and not settings.esphome_allow_secrets:
        raise ESPHomeError(f"{name} is blocked; set ESPHOME_ALLOW_SECRETS=true to allow it.")
    return name


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_BACKUP_RE = re.compile(r"(?P<cfg>[^/\\.\x00][^/\\\x00]*\.ya?ml)\.(?P<ts>\d{8}T\d{12}Z)\.yaml")


class YamlBackupStore:
    """Text backups of ESPHome files: BACKUP_DIR/esphome/<cfg>.<UTC ts>.yaml, bytes as read."""

    def __init__(self, backup_dir: str):
        self.dir = Path(backup_dir) / "esphome"

    def snapshot(self, configuration: str, content: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        while True:  # exclusive create: two snapshots in the same microsecond never collide
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.dir / f"{configuration}.{ts}.yaml"
            try:
                with open(path, "xb") as f:
                    f.write(content.encode("utf-8"))
                return path.name
            except FileExistsError:
                continue

    def list(self, configuration: str | None = None) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.iterdir():
            m = _BACKUP_RE.fullmatch(p.name)
            if m and configuration in (None, m["cfg"]):
                out.append({"backup_id": p.name, "configuration": m["cfg"],
                            "timestamp": m["ts"], "size": p.stat().st_size})
        return sorted(out, key=lambda b: b["timestamp"], reverse=True)

    def read(self, backup_id: str) -> tuple[str, str]:
        m = _BACKUP_RE.fullmatch(backup_id or "")
        if not m:
            raise ESPHomeError(f"invalid backup_id {backup_id!r}; use an id from esphome_list_backups.")
        path = self.dir / backup_id
        if not path.is_file():
            raise NRNotFoundError(f"backup {backup_id} not found in {self.dir}")
        return m["cfg"], path.read_bytes().decode("utf-8")
