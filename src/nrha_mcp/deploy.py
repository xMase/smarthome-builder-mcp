"""Deploy safety gate: diff preview + propose/confirm + backup-first post."""

from datetime import datetime, timezone

from .client import NRConflictError, NRError


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
    """Run the safety gate.

    rebuild(flows) -> proposed_flows : pure, must NOT mutate `flows`.
    Returns a dict describing either the preview (no write) or the deploy result.
    """
    flows, rev = await client.get_flows()
    proposed = rebuild(flows)
    preview = diff_flows(flows, proposed)

    if settings.deploy_mode == "propose" and not confirm:
        return {
            "deployed": False,
            "mode": "propose",
            "summary": summary,
            "preview": preview,
            "next": "Re-call this tool with confirm=true to apply.",
        }

    backup_path = store.snapshot(flows, summary)
    try:
        new_rev = await client.post_flows(proposed, rev)
    except NRConflictError:
        flows, rev = await client.get_flows()
        proposed = rebuild(flows)
        preview = diff_flows(flows, proposed)
        backup_path = store.snapshot(flows, summary)
        new_rev = await client.post_flows(proposed, rev)

    return {
        "deployed": True,
        "rev": new_rev,
        "backup": str(backup_path),
        "deployed_at": _now_iso(),
        "summary": summary,
        "changes": preview,
    }


def _summary_node(node: dict) -> dict:
    return {"id": node.get("id"), "type": node.get("type"), "name": node.get("name", "")}


def _load_backup(store, path: str) -> list[dict]:
    try:
        return store.restore(path)
    except ValueError as e:
        raise NRError(str(e))


def _enrich(raw: dict, before: list[dict], after: list[dict], *, before_label: str, after_label: str) -> dict:
    bmap = {n["id"]: n for n in before if "id" in n}
    amap = {n["id"]: n for n in after if "id" in n}
    added = [_summary_node(amap[i]) for i in raw["added"] if i in amap]
    removed = [_summary_node(bmap[i]) for i in raw["removed"] if i in bmap]
    return {
        "before": before_label,
        "after": after_label,
        "summary": {"added": len(added), "removed": len(removed), "changed": len(raw["changed"])},
        "added": added,
        "removed": removed,
        "changed": raw["changed"],
    }


async def diff_backup(client, store, *, path: str, path_b: str | None = None) -> dict:
    """Diff a backup against the current flows (path_b=None) or against another backup."""
    before = _load_backup(store, path)
    if path_b is None:
        after, _ = await client.get_flows()
        after_label = "current"
    else:
        after = _load_backup(store, path_b)
        after_label = path_b
    raw = diff_flows(before, after)
    return _enrich(raw, before, after, before_label=path, after_label=after_label)
