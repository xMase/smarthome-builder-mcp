# Flow Diff vs Backup for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/diff-backup`

## Problem

The MCP snapshots flows to disk before every deploy (`BackupStore`), and the
deploy gate already computes a diff internally (`deploy.diff_flows`), but there is
no way for an agent to **see what changed** between a backup and the current
flows, or between two backups. That makes "what did I (or something) change since
snapshot X?" and rollback decisions blind.

## Goals

- A `nr_diff` tool that diffs a backup against the current flows, or two backups
  against each other, and returns a readable structural diff.

## Non-goals

- Diffing arbitrary flow JSON not produced by `BackupStore` (use a backup path).
- A textual/line-level diff — this is a node-structural diff (added/removed/changed
  by node id), reusing the existing `diff_flows`.

## Design

### A. Reused engine

`deploy.diff_flows(before, after)` already returns:

```json
{"added": ["id", ...], "removed": ["id", ...],
 "changed": {"id": {"field": {"before": x, "after": y}}}}
```

`added` = ids present in `after` but not `before`; `removed` = in `before` not
`after`; `changed` = field-level differences for ids in both. This is reused
unchanged.

### B. Orchestrator — `deploy.py`

Co-located with `diff_flows` (DRY):

```python
async def diff_backup(client, store, *, path, path_b=None) -> dict:
    before = _load_backup(store, path)
    if path_b is None:
        after, _ = await client.get_flows()
        after_label = "current"
    else:
        after = _load_backup(store, path_b)
        after_label = path_b
    raw = diff_flows(before, after)
    return _enrich(raw, before, after, before_label=path, after_label=after_label)
```

- `_load_backup(store, p)` wraps `store.restore(p)`, converting its `ValueError`
  (bad/missing path) into `NRError` for a clean tool error.
- `_enrich(raw, before, after, ...)` turns `added`/`removed` from bare id lists
  into `[{"id", "type", "name"}]` (added ids looked up in `after`, removed ids in
  `before`); keeps `changed` field-level; adds `summary` and labels:

```json
{
  "before": "<path>",
  "after": "current",
  "summary": {"added": 1, "removed": 0, "changed": 2},
  "added":   [{"id": "n9", "type": "debug", "name": "out"}],
  "removed": [],
  "changed": {"n2": {"name": {"before": "calc", "after": "calc2"}}}
}
```

### C. Tool — `server.py`

```python
@mcp.tool()
async def nr_diff(path: str, path_b: str | None = None) -> str:
    """Diff a backup against the current flows, or against another backup.

    `path` is a backup path from nr_list_backups. With only `path`: before=that
    backup, after=current flows (what changed since the snapshot). With `path_b`:
    before=path, after=path_b. Returns added/removed (with id/type/name) and
    field-level changed, plus a summary."""
    return await _wrap(lambda: deploy.diff_backup(client, store, path=path, path_b=path_b))
```

`server.py` already has `client` and `store` (the `BackupStore`); it already
imports `from .deploy import ...`? No — `server.py` calls deploy logic via `edit`.
Add `deploy` to the `from . import ...` line.

**Tool count goes 25 → 26.** Update both hardcoded sites: `tests/test_server_smoke.py`
(`expected` + `len(names) == 26`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

A bad/missing backup path raises `NRError` (mapped from `store.restore`'s
`ValueError` by `_load_backup`) → surfaced as a structured `{"error": ...}` via
`_wrap`. `client.get_flows()` errors surface through the usual `NRError` mapping.

## Testing

`tests/test_diff.py` (new) — orchestrator tests with `FakeNRClient` and a real
`BackupStore` on `tmp_path`:
- snapshot the current flows, mutate the fake's flows (add a node, change a field,
  remove a node), `diff_backup(path)` → assert `added`/`removed`/`changed` and that
  `added`/`removed` entries carry `id`/`type`/`name`; `summary` counts match.
- two-backup diff: snapshot A, change flows, snapshot B, `diff_backup(A, B)`.
- bad path → `NRError`.
- `diff_flows` itself already has unit coverage; do not duplicate.

`tests/test_server_smoke.py`: count 26; `nr_diff` registered.

`scripts/smoke_e2e.py`: add `"nr_diff"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild and redeploy the Docker image to expose the tool live. Local `.venv` is
for tests.

## Follow-ups

- None required. (A future textual/JSON-patch view could be added if structural
  diff proves insufficient, but YAGNI for now.)
