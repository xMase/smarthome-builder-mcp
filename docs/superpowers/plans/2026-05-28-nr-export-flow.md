# nr_export_flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `nr_export_flow(name_or_id)` tool that returns one tab as an import-ready Node-RED flow array `[tab, ...groups, ...nodes]`, so an agent no longer hand-reshapes `nr_get_flow` output.

**Architecture:** Extract the tab-resolution from `flows.get_flow` into a shared `_resolve_tab` helper (DRY); add `flows.export_flow` returning `{flow, tab_id, node_count, group_count}`; register a thin `nr_export_flow` tool in `server.py`.

**Tech Stack:** Python 3.11+, FastMCP, pytest/pytest-asyncio (`asyncio_mode=auto`). Run tests with `.venv/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-05-28-nr-export-flow-design.md`

---

## File Structure

- `src/nrha_mcp/flows.py` — `_resolve_tab` helper (extracted from `get_flow`), `export_flow`.
- `src/nrha_mcp/server.py` — `nr_export_flow` tool.
- `tests/test_flows.py` — `_resolve_tab` + `export_flow` tests.
- `tests/test_server_smoke.py` + `scripts/smoke_e2e.py` — register the tool, count 38 → 39.
- `README.md` — add to the Inspect (read-only) row, bump tool count.

---

## Task 1: Extract `_resolve_tab` and refactor `get_flow`

**Files:**
- Modify: `src/nrha_mcp/flows.py` (the `get_flow` function)
- Test: `tests/test_flows.py` (append)

- [ ] **Step 1: Write a test that locks the extracted helper's behavior**

Append to `tests/test_flows.py`:

```python
# --- nr_export_flow: _resolve_tab helper ---

def test_resolve_tab_by_id_and_name():
    from nrha_mcp.flows import _resolve_tab
    flows_list = [
        {"id": "tabA", "type": "tab", "label": "Heating"},
        {"id": "tabB", "type": "tab", "label": "Cooling"},
    ]
    assert _resolve_tab(flows_list, "tabA")["id"] == "tabA"
    assert _resolve_tab(flows_list, "cool")["id"] == "tabB"

def test_resolve_tab_ambiguous_and_missing():
    from nrha_mcp.flows import _resolve_tab
    flows_list = [
        {"id": "t1", "type": "tab", "label": "Solar east"},
        {"id": "t2", "type": "tab", "label": "Solar west"},
    ]
    with pytest.raises(NRNotFoundError):
        _resolve_tab(flows_list, "solar")      # ambiguous
    with pytest.raises(NRNotFoundError):
        _resolve_tab(flows_list, "nope")       # missing
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_flows.py -k "resolve_tab" -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_tab'`

- [ ] **Step 3: Extract the helper and refactor `get_flow`**

In `src/nrha_mcp/flows.py`, add the helper just above `get_flow`:

```python
def _resolve_tab(all_flows: list[dict], name_or_id: str) -> dict:
    """Find a tab by id, else by unique label substring. Raises NRNotFoundError on
    ambiguous or no match."""
    tabs = [f for f in all_flows if f.get("type") == "tab"]
    for t in tabs:
        if t["id"] == name_or_id:
            return t
    q = name_or_id.lower()
    matches = [t for t in tabs if q in t.get("label", "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise NRNotFoundError(
            f"Ambiguous tab name '{name_or_id}'. Matches: {[m.get('label') for m in matches]}")
    raise NRNotFoundError(f"Tab not found: '{name_or_id}'")
```

Then in `get_flow`, REPLACE the inline resolution block:

```python
    flows, _ = await client.get_flows()
    tabs = [f for f in flows if f.get("type") == "tab"]
    tab = None
    q = name_or_id.lower()
    for t in tabs:
        if t["id"] == name_or_id:
            tab = t
            break
    if not tab:
        matches = [t for t in tabs if q in t.get("label", "").lower()]
        if len(matches) == 1:
            tab = matches[0]
        elif len(matches) > 1:
            raise NRNotFoundError(f"Ambiguous tab name '{name_or_id}'. Matches: {[m.get('label') for m in matches]}")
        else:
            raise NRNotFoundError(f"Tab not found: '{name_or_id}'")
    tab_id = tab["id"]
```

with:

```python
    flows, _ = await client.get_flows()
    tab = _resolve_tab(flows, name_or_id)
    tab_id = tab["id"]
```

Leave the rest of `get_flow` (nodes/groups slicing, `fields` filter, return) unchanged.

- [ ] **Step 4: Run the new tests AND the existing flows tests (regression guard)**

Run: `.venv/bin/pytest tests/test_flows.py -v`
Expected: PASS — both new `resolve_tab` tests and all existing `get_flow` tests green.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "refactor: extract _resolve_tab from get_flow"
```

---

## Task 2: `flows.export_flow`

**Files:**
- Modify: `src/nrha_mcp/flows.py` — add `export_flow` (after `get_flow`)
- Test: `tests/test_flows.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flows.py`:

```python
# --- nr_export_flow: export_flow ---

@pytest.mark.asyncio
async def test_export_flow_builds_importable_array(fake_client):
    res = await flows.export_flow(fake_client, "Heating")
    assert res["flow"][0]["id"] == "tab1"                 # tab first
    ids = {n["id"] for n in res["flow"]}
    assert ids == {"tab1", "g1", "n1", "n2", "n3"}
    assert res["tab_id"] == "tab1"
    assert res["node_count"] == 3
    assert res["group_count"] == 1
    assert len(res["flow"]) == 5

@pytest.mark.asyncio
async def test_export_flow_excludes_global_config_nodes(fake_client):
    res = await flows.export_flow(fake_client, "tab1")     # resolve by id too
    ids = {n["id"] for n in res["flow"]}
    assert "srv1" not in ids                               # z="" config node not bundled

@pytest.mark.asyncio
async def test_export_flow_is_verbatim(fake_client):
    res = await flows.export_flow(fake_client, "Heating")
    n3 = next(n for n in res["flow"] if n["id"] == "n3")
    assert n3["domain"] == "switch" and n3["service"] == "turn_on"
    assert n3["entityId"] == "switch.boiler"

@pytest.mark.asyncio
async def test_export_flow_unknown_raises(fake_client):
    with pytest.raises(NRNotFoundError):
        await flows.export_flow(fake_client, "does-not-exist")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_flows.py -k "export_flow" -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.flows' has no attribute 'export_flow'`

- [ ] **Step 3: Implement `export_flow`**

In `src/nrha_mcp/flows.py`, add after `get_flow`:

```python
async def export_flow(client: NRClient, name_or_id: str) -> dict:
    """Export one tab as an import-ready flat flow array [tab, ...groups, ...nodes].
    Verbatim (no field filtering). Does not bundle global config nodes or subflow
    definitions the tab references."""
    all_flows, _ = await client.get_flows()
    tab = _resolve_tab(all_flows, name_or_id)
    tab_id = tab["id"]
    nodes = [f for f in all_flows if f.get("z") == tab_id and f.get("type") != "group"]
    groups = [f for f in all_flows if f.get("z") == tab_id and f.get("type") == "group"]
    return {
        "flow": [tab, *groups, *nodes],
        "tab_id": tab_id,
        "node_count": len(nodes),
        "group_count": len(groups),
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_flows.py -k "export_flow" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat: flows.export_flow builds import-ready single-tab array"
```

---

## Task 3: Register the `nr_export_flow` tool

**Files:**
- Modify: `src/nrha_mcp/server.py` — add the tool after `nr_get_flow` (read-only group)
- Modify: `tests/test_server_smoke.py` — add to `expected`, bump count to 39
- Modify: `scripts/smoke_e2e.py` — add to `EXPECTED_TOOLS`

- [ ] **Step 1: Update the smoke test FIRST (so it fails)**

In `tests/test_server_smoke.py`, add `"nr_export_flow"` to the `expected` set (e.g. on the first line, after `"nr_get_flow"`):

```python
        "nr_get_flow_summary", "nr_get_flow", "nr_export_flow", "nr_get_subflow", "nr_search_nodes", "nr_get_function_code",
```

and change the count:

```python
    assert len(names) == 39
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `assert 38 == 39`

- [ ] **Step 3: Add the tool**

In `src/nrha_mcp/server.py`, immediately after the `nr_get_flow` tool definition (its `return await _wrap(...)` line):

```python
@mcp.tool()
async def nr_export_flow(name_or_id: str) -> str:
    """Export one tab as an import-ready Node-RED flow array.

    Returns {flow, tab_id, node_count, group_count} where `flow` is the flat
    [tab, ...groups, ...nodes] array you can paste into Node-RED's Import dialog or
    write to a .json file. Full fidelity (no field filtering). Does NOT bundle subflow
    definitions or global config nodes the tab references — use nr_backup_flow for a
    complete all-flows snapshot."""
    return await _wrap(lambda: flows.export_flow(client, name_or_id))
```

- [ ] **Step 4: Add the tool to the e2e expected set**

In `scripts/smoke_e2e.py`, inside `EXPECTED_TOOLS`, add a line:

```python
    "nr_export_flow",
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat: register nr_export_flow tool (38 -> 39)"
```

---

## Task 4: Full suite + README

**Files:**
- Modify: `README.md` (Tools table)
- Test: whole suite

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/pytest -q`
Expected: all green. If anything fails, STOP and report.

- [ ] **Step 2: Update the README Tools table**

In `README.md`:
(a) change the count line `38 MCP tools, grouped:` to `39 MCP tools, grouped:`
(b) in the **Inspect** (read-only) row, add `nr_export_flow` right after the `nr_get_flow` entry, e.g.:

```
`nr_get_flow` (one tab; optional `fields` filter), `nr_export_flow` (one tab as an import-ready flat array), `nr_get_subflow` ...
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add nr_export_flow to README Tools table (39)"
```

---

## Deployment (after merge)

Rebuild the Docker image so the running MCP exposes `nr_export_flow` live.

## Self-Review notes

- **Spec coverage:** §A→Task 1; §B→Task 2; §C→Task 3; error handling→Tasks 1-2 (NRNotFoundError) + smoke; testing→Tasks 1-3; README→Task 4.
- **Type consistency:** `_resolve_tab(all_flows, name_or_id) -> dict` and `export_flow(client, name_or_id) -> dict` used identically across tasks; tool delegates to `flows.export_flow`. Count 38→39 consistent across server smoke + e2e + README.
- **No placeholders.**
