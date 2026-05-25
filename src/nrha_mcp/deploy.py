"""Deploy safety gate: diff preview + propose/confirm + backup-first post."""

from datetime import datetime, timezone

from .client import NRConflictError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def diff_flows(before: list[dict], after: list[dict]) -> dict:
    """Human-readable diff of two flow lists, keyed by node id."""
    bmap = {n["id"]: n for n in before if "id" in n}
    amap = {n["id"]: n for n in after if "id" in n}
    added = [i for i in amap if i not in bmap]
    removed = [i for i in bmap if i not in amap]
    changed: dict[str, dict] = {}
    for i in bmap.keys() & amap.keys():
        b, a = bmap[i], amap[i]
        fields = {
            k: {"before": b.get(k), "after": a.get(k)}
            for k in (b.keys() | a.keys())
            if b.get(k) != a.get(k)
        }
        if fields:
            changed[i] = fields
    return {"added": added, "removed": removed, "changed": changed}


async def commit(client, store, settings, *, rebuild, summary, confirm=False):
    raise NotImplementedError("commit not yet implemented")
