# Flow Diff vs Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `nr_diff` tool that diffs a backup against the current flows (or against another backup) and returns a readable structural diff.

**Architecture:** Reuse `deploy.diff_flows`. Add an async `deploy.diff_backup(client, store, *, path, path_b=None)` orchestrator that loads before/after (from `BackupStore.restore` and/or `client.get_flows`), runs `diff_flows`, and enriches added/removed with `{id,type,name}`. Expose it as `nr_diff`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-diff-backup-design.md`

---

## File structure

- Modify `src/nrha_mcp/deploy.py` — add `diff_backup` + `_load_backup` + `_enrich` + `_summary_node`; widen the client import to include `NRError`.
- Modify `src/nrha_mcp/server.py` — import `deploy`; add the `nr_diff` tool.
- Create `tests/test_diff.py` — orchestrator tests.
- Modify `tests/test_server_smoke.py` — tool count 25 → 26.
- Modify `scripts/smoke_e2e.py` — add the tool to `EXPECTED_TOOLS`.

All commands from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv venv, no `pip`). Branch: `feat/diff-backup`.

---

## Task 1: `diff_backup` orchestrator

**Files:**
- Modify: `src/nrha_mcp/deploy.py`
- Test: `tests/test_diff.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_diff.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_diff.py -v`
Expected: FAIL with `AttributeError: module 'nrha_mcp.deploy' has no attribute 'diff_backup'`

- [ ] **Step 3: Widen the client import in `deploy.py`**

In `src/nrha_mcp/deploy.py`, change:

```python
from .client import NRConflictError
```

to:

```python
from .client import NRConflictError, NRError
```

- [ ] **Step 4: Implement the orchestrator**

Append to `src/nrha_mcp/deploy.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_diff.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/deploy.py tests/test_diff.py
git commit -m "feat(deploy): diff_backup (backup vs current / backup vs backup)"
```

---

## Task 2: Register the `nr_diff` tool

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"nr_diff",` to the `expected` set (e.g. on the backups line, right after `"nr_restore_backup",`) and change the count. The backups line becomes:

```python
        "nr_backup_flow", "nr_list_backups", "nr_restore_backup", "nr_diff",
```

and:

```python
    assert len(names) == 26
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `nr_diff` not registered; `len(names)` is 25, not 26.

- [ ] **Step 3: Import `deploy` in `server.py`**

In `src/nrha_mcp/server.py`, change:

```python
from . import flows, edit, modules, ha, runtime, validate
```

to:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy
```

- [ ] **Step 4: Add the tool**

In `src/nrha_mcp/server.py`, immediately AFTER the `nr_restore_backup` function (after its closing `))` line), add:

```python


@mcp.tool()
async def nr_diff(path: str, path_b: str | None = None) -> str:
    """Diff a backup against the current flows, or against another backup.

    `path` is a backup path from nr_list_backups. With only `path`: before=that
    backup, after=current flows (what changed since the snapshot). With `path_b`:
    before=path, after=path_b. Returns added/removed (each with id/type/name) and
    field-level `changed`, plus a `summary` count."""
    return await _wrap(lambda: deploy.diff_backup(client, store, path=path, path_b=path_b))
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 6: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"nr_diff",` to the `EXPECTED_TOOLS` set (e.g. right after `"nr_restore_backup",`). The script asserts `len(EXPECTED_TOOLS)` tools are present.

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add nr_diff tool"
```

---

## Task 3: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('diff:', 'nr_diff' in n); print('count:', len(n))"`
Expected: `diff: True` and `count: 26`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack). This restarts the MCP, so do it when ready.

---

## Self-review

- **Spec coverage:** reused `diff_flows` + orchestrator (`diff_backup`, `_load_backup`, `_enrich`, `_summary_node`) → Task 1; backup↔current and backup↔backup both covered (Task 1 tests); enrichment with id/type/name + summary + labels → Task 1 (`_enrich`); tool + import + 25→26 in both assertion sites → Task 2; bad-path → `NRError` → Task 1 test; verify + deploy → Task 3. No gaps.
- **Placeholders:** none — full code and exact commands throughout.
- **Type consistency:** `diff_backup(client, store, *, path, path_b=None)` identical in Task 1 (def), its tests, and the Task 2 `server.py` wrapper. Output keys (`before`/`after`/`summary`/`added`/`removed`/`changed`) consistent between `_enrich`, the tests, and the tool docstring. `added`/`removed` element shape `{id,type,name}` from `_summary_node` matches the test assertions. Tool count 26 consistent across Task 2 and Task 3.
