# Flow / Node "disabled" Visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface tab/node `disabled` state in nrha's read tools (`nr_get_flow_summary`, `nr_search_nodes`, `nr_get_node_config`) and give `nr_inject` a clear error when a node can't run because it (or its tab) is disabled.

**Architecture:** Purely additive fields in `flows.py` read functions + two pre-checks in `modules.inject_node` (which already fetches flows). No new tool — tool count stays 37. Disabled state is read with `bool(x.get(...))` because Node-RED omits the key when false.

**Tech Stack:** Python 3.11+, pytest (`asyncio_mode=auto`), `FakeNRClient` test double (`tests/conftest.py`).

---

## File Structure

- **Modify:** `src/nrha_mcp/flows.py` — `get_flow_summary`, `search_nodes`, `get_node_config`.
- **Modify:** `src/nrha_mcp/modules.py` — `inject_node` pre-checks.
- **Test:** `tests/test_flows.py` — new `DISABLED_FLOWS` fixture + `dis_client` + 3 tests.
- **Test:** `tests/test_modules.py` — 2 inject tests.

No change to `tests/test_server_smoke.py` or `scripts/smoke_e2e.py` (no new tool).

---

## Task 1: Surface `disabled` in the three read functions

**Files:**
- Test: `tests/test_flows.py`
- Modify: `src/nrha_mcp/flows.py` (`get_flow_summary` @6, `search_nodes` @67, `get_node_config` @120)

- [ ] **Step 1: Add the shared fixture to `tests/test_flows.py`**

Add near the top (after the imports), mirroring the existing `sf_client` pattern:

```python
DISABLED_FLOWS = [
    {"id": "tabOn", "type": "tab", "label": "Active"},
    {"id": "tabOff", "type": "tab", "label": "Parked", "disabled": True},
    {"id": "nOn", "type": "inject", "z": "tabOn", "name": "live", "wires": [[]]},
    {"id": "nOff", "type": "inject", "z": "tabOff", "name": "on parked tab", "wires": [[]]},
    {"id": "nDis", "type": "function", "z": "tabOn", "name": "disabled node",
     "d": True, "func": "", "wires": [[]]},
    {"id": "sfdef", "type": "subflow", "name": "SF"},
    {"id": "nInternal", "type": "change", "z": "sfdef", "name": "inside sf", "wires": [[]]},
]


@pytest.fixture
def dis_client():
    from tests.conftest import FakeNRClient
    return FakeNRClient(flows=DISABLED_FLOWS)
```

- [ ] **Step 2: Write the failing test for `get_flow_summary`**

```python
@pytest.mark.asyncio
async def test_summary_marks_disabled_tabs(dis_client):
    res = await flows.get_flow_summary(dis_client)
    by_id = {t["id"]: t for t in res["tabs"]}
    assert by_id["tabOff"]["disabled"] is True
    assert by_id["tabOn"]["disabled"] is False
```

- [ ] **Step 3: Run it, confirm it fails**

Run: `.venv/bin/pytest tests/test_flows.py::test_summary_marks_disabled_tabs -v`
Expected: FAIL with `KeyError: 'disabled'`.

- [ ] **Step 4: Implement — `get_flow_summary` (`flows.py:13`)**

Add `"disabled"` to the tab_map entry:

```python
        tab_map[tab["id"]] = {"id": tab["id"], "label": tab.get("label", ""),
                              "disabled": bool(tab.get("disabled")),
                              "node_count": 0, "groups": []}
```

- [ ] **Step 5: Run it, confirm it passes**

Run: `.venv/bin/pytest tests/test_flows.py::test_summary_marks_disabled_tabs -v` → PASS.

- [ ] **Step 6: Write the failing test for `search_nodes`**

```python
@pytest.mark.asyncio
async def test_search_surfaces_disabled_state(dis_client):
    res = await flows.search_nodes(dis_client, query="")
    by_id = {r["id"]: r for r in res["results"]}
    assert by_id["nOff"]["flow_disabled"] is True
    assert by_id["nOn"]["flow_disabled"] is False
    assert by_id["nDis"]["node_disabled"] is True
    assert by_id["nOn"]["node_disabled"] is False
```

- [ ] **Step 7: Run it, confirm it fails**

Run: `.venv/bin/pytest tests/test_flows.py::test_search_surfaces_disabled_state -v`
Expected: FAIL with `KeyError: 'flow_disabled'`.

- [ ] **Step 8: Implement — `search_nodes`**

Add the lookup beside the `tabs` map (after `flows.py:70`):

```python
    tab_disabled = {f["id"]: bool(f.get("disabled")) for f in flows if f.get("type") == "tab"}
```

Add the two keys to the result dict (`flows.py:96-100`):

```python
            results.append({
                "id": node["id"], "name": node.get("name", ""), "type": node.get("type", ""),
                "flow_id": node.get("z", ""), "flow_label": _label(node.get("z", "")),
                "flow_disabled": tab_disabled.get(node.get("z", ""), False),
                "node_disabled": bool(node.get("d")),
                "match_in": match_in, "snippet": snippet,
            })
```

- [ ] **Step 9: Run it, confirm it passes**

Run: `.venv/bin/pytest tests/test_flows.py::test_search_surfaces_disabled_state -v` → PASS.

- [ ] **Step 10: Write the failing test for `get_node_config`**

```python
@pytest.mark.asyncio
async def test_node_config_surfaces_disabled(dis_client):
    on_off_tab = await flows.get_node_config(dis_client, "nOff")
    assert on_off_tab["_flow_disabled"] is True
    dnode = await flows.get_node_config(dis_client, "nDis")
    assert dnode["_disabled"] is True
    assert dnode["_flow_disabled"] is False
    internal = await flows.get_node_config(dis_client, "nInternal")
    assert internal["_flow_disabled"] is False
```

- [ ] **Step 11: Run it, confirm it fails**

Run: `.venv/bin/pytest tests/test_flows.py::test_node_config_surfaces_disabled -v`
Expected: FAIL with `KeyError: '_flow_disabled'`.

- [ ] **Step 12: Implement — `get_node_config`**

Add the lookup beside the `tabs` map (after `flows.py:125`):

```python
    tab_disabled = {f["id"]: bool(f.get("disabled")) for f in flows if f.get("type") == "tab"}
```

After `result["_flow_label"] = ...` (`flows.py:142`), add:

```python
    result["_flow_disabled"] = tab_disabled.get(node.get("z", ""), False)
    result["_disabled"] = bool(node.get("d"))
```

- [ ] **Step 13: Run it, confirm it passes**

Run: `.venv/bin/pytest tests/test_flows.py::test_node_config_surfaces_disabled -v` → PASS.

- [ ] **Step 14: Run the FULL suite — catch exact-dict regressions**

Run: `.venv/bin/pytest -q`
Expected: all PASS. If any existing test compared a `get_flow_summary`/`search_nodes`/`get_node_config` result against an **exact** dict, add the new keys to that expected dict. (Existing tests use subset/`["key"]` assertions, so none should break — but verify.)

- [ ] **Step 15: Commit**

```bash
git add -A
git commit -m "feat: surface tab/node disabled state in flows reads

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `inject_node` clear error for disabled node / disabled tab

**Files:**
- Test: `tests/test_modules.py`
- Modify: `src/nrha_mcp/modules.py` (`inject_node` @33)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_modules.py`:

```python
@pytest.mark.asyncio
async def test_inject_on_disabled_tab_raises():
    from tests.conftest import FakeNRClient
    client = FakeNRClient(flows=[
        {"id": "tabOff", "type": "tab", "label": "Parked", "disabled": True},
        {"id": "inj", "type": "inject", "z": "tabOff", "name": "x", "wires": [[]]},
    ])
    with pytest.raises(NRError, match="disabled flow"):
        await modules.inject_node(client, "inj")
    assert client.injected == []


@pytest.mark.asyncio
async def test_inject_disabled_node_raises():
    from tests.conftest import FakeNRClient
    client = FakeNRClient(flows=[
        {"id": "tabOn", "type": "tab", "label": "Active"},
        {"id": "inj", "type": "inject", "z": "tabOn", "name": "x", "d": True, "wires": [[]]},
    ])
    with pytest.raises(NRError, match="disabled"):
        await modules.inject_node(client, "inj")
    assert client.injected == []
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv/bin/pytest tests/test_modules.py -k "disabled" -v`
Expected: FAIL — `inject_node` currently calls `client.inject` and returns success (no raise; `client.injected` is non-empty).

- [ ] **Step 3: Implement the pre-checks — `modules.py:33`**

Between the type check (`raise NRError(... not 'inject')`) and `await client.inject(node_id)`, insert:

```python
    if node.get("d"):
        raise NRError(f"Inject node '{node_id}' is disabled — enable the node before injecting.")
    z = node.get("z", "")
    tab = next((f for f in flows if f.get("id") == z and f.get("type") == "tab"), None)
    if tab and tab.get("disabled"):
        raise NRError(
            f"Inject node '{node_id}' is on disabled flow '{tab.get('label', '')}' — "
            f"enable the flow before injecting.")
```

(`NRError` is already imported in `modules.py`.)

- [ ] **Step 4: Run them, confirm they pass**

Run: `.venv/bin/pytest tests/test_modules.py -k "disabled" -v` → PASS.

- [ ] **Step 5: Run the full module + flows suite**

Run: `.venv/bin/pytest tests/test_modules.py tests/test_flows.py -q`
Expected: all PASS — including the existing `test_inject_ok` (enabled inject still triggers) and `test_inject_wrong_type`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: nr_inject errors clearly on disabled node/flow

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **`disabled`/`d` are usually absent** on enabled tabs/nodes — always `bool(x.get(...))`, never bare indexing.
- **Naming asymmetry is intentional:** `search_nodes` results use `flow_disabled`/`node_disabled` (matching its existing `flow_label`/`flow_id`); `get_node_config` uses `_flow_disabled`/`_disabled` (matching its existing `_flow_label`/`_group_label`). Do not "harmonize" them.
- **Subflow internals** (`z` = a subflow def) correctly get `flow_disabled=false` because the def id isn't in the `tab_disabled` map — no special-casing needed.
