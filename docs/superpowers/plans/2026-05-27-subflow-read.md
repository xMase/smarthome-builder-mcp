# Subflow-Aware Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Node-RED subflows first-class in the read tools: a new `nr_get_subflow` tool plus subflow-awareness in `get_node_config`/`get_flow_summary`/`search_nodes`, all driven by one shared pure resolver.

**Architecture:** A single pure helper `_resolve_subflow(flows, def_id)` in `flows.py` interprets a subflow definition's `in`/`out`/`status` arrays (where subflow wiring actually lives) into resolved ports, annotated internal nodes, nested-subflow pointers, and instance usage. `get_subflow` resolves an identifier (name / definition id / instance id) to a `def_id` and calls it; the three existing read tools reuse the same helper. All read-only — no deploy gate.

**Tech Stack:** Python 3.11+, FastMCP (`mcp` SDK), `pytest`/`pytest-asyncio` (`asyncio_mode=auto`), `FakeNRClient` in-memory test double.

Spec: `docs/superpowers/specs/2026-05-27-subflow-read-design.md`. Work on branch `feat/subflow-read` (already created).

---

### Task 1: Pure resolver `_resolve_subflow` + helpers

**Files:**
- Modify: `src/nrha_mcp/flows.py` (add helpers + `_resolve_subflow`)
- Test: `tests/test_flows.py` (add `SUBFLOW_FLOWS` fixture + tests)

- [ ] **Step 1: Add the shared fixture and the failing resolver tests**

Append to `tests/test_flows.py`:

```python
# --- subflow fixtures/tests -------------------------------------------------

SUBFLOW_FLOWS = [
    {"id": "tabA", "type": "tab", "label": "Main"},
    {
        "id": "sfdef", "type": "subflow", "name": "seasonSwitch", "color": "#DDAA99",
        "in": [{"wires": [{"id": "getS"}]}],
        "out": [
            {"wires": [{"id": "nest", "port": 0}]},
            {"wires": [{"id": "nest", "port": 1}]},
        ],
        "status": {"wires": [{"id": "chg", "port": 0}]},
        "outputLabels": ["inverno", "estate"],
        "env": [],
    },
    {"id": "getS", "type": "api-current-state", "z": "sfdef", "name": "getSeason",
     "entity_id": "select.season", "wires": [["nest", "chg"]]},
    {"id": "nest", "type": "subflow:nestdef", "z": "sfdef", "name": "", "wires": [[], []]},
    {"id": "chg", "type": "change", "z": "sfdef", "name": "", "wires": [[]]},
    {"id": "nestdef", "type": "subflow", "name": "strSeasonSwitch",
     "in": [{"wires": [{"id": "sw"}]}],
     "out": [{"wires": [{"id": "sw", "port": 0}]}, {"wires": [{"id": "sw", "port": 1}]}],
     "outputLabels": ["inverno", "estate"]},
    {"id": "sw", "type": "switch", "z": "nestdef", "name": "", "wires": [[], []]},
    {"id": "inst1", "type": "subflow:sfdef", "z": "tabA", "name": "seasonSwitch",
     "wires": [[], ["dn1"]]},
    {"id": "dn1", "type": "debug", "z": "tabA", "name": "", "wires": [[]]},
]


@pytest.fixture
def sf_client():
    from tests.conftest import FakeNRClient
    return FakeNRClient(flows=SUBFLOW_FLOWS)


@pytest.mark.asyncio
async def test_get_subflow_outputs_resolved_with_labels(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    assert res["subflow"]["name"] == "seasonSwitch"
    assert [o["label"] for o in res["outputs"]] == ["inverno", "estate"]
    assert res["outputs"][1]["sources"] == [{"node_id": "nest", "port": 1}]
    assert res["status"]["sources"] == [{"node_id": "chg", "port": 0}]
    assert res["inputs"][0]["wires"] == [{"node_id": "getS"}]


@pytest.mark.asyncio
async def test_get_subflow_internal_node_annotations(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    by_id = {n["id"]: n for n in res["nodes"]}
    assert by_id["getS"]["_targets"] == ["nest", "chg"]
    assert by_id["nest"]["_feeds_outputs"] == [0, 1]
    assert by_id["chg"]["_feeds_status"] is True
    assert by_id["nest"]["_nested_subflow"] == {"def_id": "nestdef", "def_name": "strSeasonSwitch"}
    assert res["total_nodes"] == 3


@pytest.mark.asyncio
async def test_get_subflow_nested_pointer_not_expanded(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    assert res["nested_subflows"] == [
        {"instance_node_id": "nest", "def_id": "nestdef", "def_name": "strSeasonSwitch"}
    ]


@pytest.mark.asyncio
async def test_get_subflow_lists_instances_with_wired_outputs(sf_client):
    res = await flows.get_subflow(sf_client, "sfdef")
    assert res["instances"] == [
        {"node_id": "inst1", "flow_id": "tabA", "flow_label": "Main", "wired_outputs": [1]}
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_flows.py -k subflow -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.flows' has no attribute 'get_subflow'`.

- [ ] **Step 3: Add helpers + `_resolve_subflow` to `flows.py`**

Insert into `src/nrha_mcp/flows.py` (after `get_node_config`, before `_read_context`):

```python
def _subflow_name(flows: list[dict], def_id: str) -> str:
    d = next((f for f in flows if f.get("id") == def_id and f.get("type") == "subflow"), None)
    return d.get("name", "") if d else ""


def _port_sources(port: dict) -> list[dict]:
    return [{"node_id": w["id"], "port": w.get("port", 0)} for w in port.get("wires", [])]


def _resolve_subflow(flows: list[dict], def_id: str) -> dict:
    sf = next((f for f in flows if f.get("id") == def_id and f.get("type") == "subflow"), None)
    if sf is None:
        raise NRNotFoundError(f"Subflow not found: '{def_id}'")
    labels = sf.get("outputLabels", [])
    outputs = []
    for i, port in enumerate(sf.get("out", [])):
        label = labels[i] if i < len(labels) and labels[i] else f"out {i}"
        outputs.append({"index": i, "label": label, "sources": _port_sources(port)})
    inputs = [{"index": i, "wires": [{"node_id": w["id"]} for w in port.get("wires", [])]}
              for i, port in enumerate(sf.get("in", []))]
    status = {"sources": _port_sources(sf["status"])} if sf.get("status") else None

    feeds_out: dict[str, list[int]] = {}
    for o in outputs:
        for s in o["sources"]:
            feeds_out.setdefault(s["node_id"], []).append(o["index"])
    status_ids = {s["node_id"] for s in (status["sources"] if status else [])}

    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    nodes, nested = [], []
    for n in flows:
        if n.get("z") != def_id:
            continue
        targets = []
        for wl in n.get("wires", []):
            targets.extend(wl)
        node = dict(n)
        node["_targets"] = targets
        node["_feeds_outputs"] = feeds_out.get(n["id"], [])
        node["_feeds_status"] = n["id"] in status_ids
        ntype = n.get("type", "")
        if ntype.startswith("subflow:"):
            nd = ntype.split(":", 1)[1]
            ptr = {"def_id": nd, "def_name": _subflow_name(flows, nd)}
            node["_nested_subflow"] = ptr
            nested.append({"instance_node_id": n["id"], **ptr})
        nodes.append(node)

    instances = []
    inst_type = f"subflow:{def_id}"
    for f in flows:
        if f.get("type") == inst_type:
            wired = [i for i, wl in enumerate(f.get("wires", [])) if wl]
            instances.append({"node_id": f["id"], "flow_id": f.get("z", ""),
                              "flow_label": tabs.get(f.get("z", ""), ""), "wired_outputs": wired})

    return {
        "subflow": {"id": sf["id"], "name": sf.get("name", ""),
                    "category": sf.get("category", ""), "color": sf.get("color", ""),
                    "info": sf.get("info", "")},
        "inputs": inputs, "outputs": outputs, "status": status, "env": sf.get("env", []),
        "nodes": nodes, "nested_subflows": nested, "instances": instances,
        "total_nodes": len(nodes),
    }
```

Note: `NRNotFoundError` is already imported at the top of `flows.py`.

- [ ] **Step 4: Add the temporary shim so the resolver tests can call `get_subflow`**

The tests call `flows.get_subflow`, which Task 2 implements. To keep Task 1 self-contained and green, add this minimal version now (Task 2 replaces its body with the real identifier resolution):

```python
async def get_subflow(client: NRClient, name_or_id: str) -> dict:
    flows, _ = await client.get_flows()
    return _resolve_subflow(flows, name_or_id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flows.py -k subflow -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat(flows): _resolve_subflow pure helper + subflow fixtures

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Identifier resolution — `_find_subflow_def_id` + real `get_subflow`

**Files:**
- Modify: `src/nrha_mcp/flows.py` (add `_find_subflow_def_id`, rewrite `get_subflow` body)
- Test: `tests/test_flows.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flows.py`:

```python
@pytest.mark.asyncio
async def test_get_subflow_by_name(sf_client):
    res = await flows.get_subflow(sf_client, "seasonswitch")   # case-insensitive
    assert res["subflow"]["id"] == "sfdef"


@pytest.mark.asyncio
async def test_get_subflow_by_instance_id(sf_client):
    res = await flows.get_subflow(sf_client, "inst1")          # instance -> its definition
    assert res["subflow"]["id"] == "sfdef"


@pytest.mark.asyncio
async def test_get_subflow_unknown_raises(sf_client):
    with pytest.raises(NRNotFoundError):
        await flows.get_subflow(sf_client, "does-not-exist")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_flows.py -k "subflow_by_name or subflow_by_instance or subflow_unknown" -v`
Expected: FAIL — name/instance lookups currently hit the shim and raise "Subflow not found" (the shim treats the arg as a def id).

- [ ] **Step 3: Add `_find_subflow_def_id` and rewrite `get_subflow`**

Add `_find_subflow_def_id` just above `get_subflow` in `flows.py`:

```python
def _find_subflow_def_id(flows: list[dict], name_or_id: str) -> str:
    node = next((f for f in flows if f.get("id") == name_or_id), None)
    if node is not None:
        t = node.get("type", "")
        if t == "subflow":
            return name_or_id
        if t.startswith("subflow:"):
            return t.split(":", 1)[1]
    q = name_or_id.lower()
    defs = [f for f in flows if f.get("type") == "subflow"]
    exact = [d for d in defs if d.get("name", "").lower() == q]
    matches = exact or [d for d in defs if q in d.get("name", "").lower()]   # exact wins over substring
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        raise NRNotFoundError(f"Ambiguous subflow name '{name_or_id}'. "
                              f"Matches: {[m.get('name') for m in matches]}")
    raise NRNotFoundError(f"Subflow not found: '{name_or_id}'")
```

Replace the `get_subflow` body from Task 1 with:

```python
async def get_subflow(client: NRClient, name_or_id: str) -> dict:
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, name_or_id)
    return _resolve_subflow(flows, def_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flows.py -k subflow -v`
Expected: PASS (7 subflow tests — Task 1's 4 still pass; resolution by def id is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat(flows): get_subflow resolves by name, def id, or instance id

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Register the `nr_get_subflow` tool + smoke updates

**Files:**
- Modify: `src/nrha_mcp/server.py` (add tool after `nr_get_flow`, ~line 68)
- Modify: `tests/test_server_smoke.py` (`expected` set + count 30 → 31)
- Modify: `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`)

- [ ] **Step 1: Update the smoke test to expect the new tool (failing)**

In `tests/test_server_smoke.py`, add `"nr_get_subflow"` to the `expected` set (e.g. on the `nr_get_flow` line) and change the final assertion:

```python
    assert len(names) == 31
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `assert 30 == 31` (tool not registered yet).

- [ ] **Step 3: Register the tool in `server.py`**

Insert after the `nr_get_flow` tool (after line 68):

```python
@mcp.tool()
async def nr_get_subflow(name_or_id: str) -> str:
    """Resolve a subflow by name, definition id, or instance id. Returns the
    definition, its inputs/outputs/status resolved to the internal nodes that feed
    each port (with output labels), the internal nodes (annotated with where they
    actually wire, including subflow output ports), pointers to nested subflows, and
    every instance with which outputs it wires. Use this for subflows; nr_get_flow
    handles tabs only."""
    return await _wrap(lambda: flows.get_subflow(client, name_or_id))
```

(`flows` is already imported in `server.py`.)

- [ ] **Step 4: Add the tool to the e2e smoke list**

In `scripts/smoke_e2e.py`, add `"nr_get_subflow",` to the `EXPECTED_TOOLS` set (near `"nr_get_flow",`).

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): register nr_get_subflow tool (30->31)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Subflow-awareness in `get_node_config`

**Files:**
- Modify: `src/nrha_mcp/flows.py` (`get_node_config`)
- Test: `tests/test_flows.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flows.py`:

```python
@pytest.mark.asyncio
async def test_node_config_on_subflow_definition(sf_client):
    res = await flows.get_node_config(sf_client, "sfdef")
    assert set(res["_downstream"]) == {"nest", "chg"}
    assert res["_ports"]["inputs"] == 1
    assert res["_ports"]["has_status"] is True
    assert {o["label"] for o in res["_ports"]["outputs"]} == {"inverno", "estate"}


@pytest.mark.asyncio
async def test_node_config_on_internal_node_feeding_port(sf_client):
    res = await flows.get_node_config(sf_client, "nest")
    assert res["_feeds_outputs"] == [0, 1]
    assert res["_subflow_def"] == {"id": "nestdef", "name": "strSeasonSwitch"}  # also an instance


@pytest.mark.asyncio
async def test_node_config_on_subflow_instance(sf_client):
    res = await flows.get_node_config(sf_client, "inst1")
    assert res["_subflow_def"] == {"id": "sfdef", "name": "seasonSwitch"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_flows.py -k node_config -v`
Expected: FAIL — `KeyError: '_ports'` / `'_subflow_def'` (awareness not added).

- [ ] **Step 3: Add awareness to `get_node_config`**

In `src/nrha_mcp/flows.py`, just before `return result` in `get_node_config`, insert:

```python
    if node.get("type") == "subflow":
        resolved = _resolve_subflow(flows, node_id)
        ds = set(result["_downstream"])
        for o in resolved["outputs"]:
            ds.update(s["node_id"] for s in o["sources"])
        if resolved["status"]:
            ds.update(s["node_id"] for s in resolved["status"]["sources"])
        result["_downstream"] = list(ds)
        result["_ports"] = {
            "inputs": len(resolved["inputs"]),
            "outputs": [{"index": o["index"], "label": o["label"]} for o in resolved["outputs"]],
            "has_status": resolved["status"] is not None,
        }
    else:
        z = node.get("z", "")
        if any(f.get("id") == z and f.get("type") == "subflow" for f in flows):
            me = next((n for n in _resolve_subflow(flows, z)["nodes"] if n["id"] == node_id), {})
            result["_feeds_outputs"] = me.get("_feeds_outputs", [])
            result["_feeds_status"] = me.get("_feeds_status", False)
        ntype = node.get("type", "")
        if ntype.startswith("subflow:"):
            nd = ntype.split(":", 1)[1]
            result["_subflow_def"] = {"id": nd, "name": _subflow_name(flows, nd)}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flows.py -k node_config -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat(flows): subflow-aware get_node_config (ports, feeds, instance def)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: `subflows` section in `get_flow_summary`

**Files:**
- Modify: `src/nrha_mcp/flows.py` (`get_flow_summary`)
- Test: `tests/test_flows.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flows.py`:

```python
@pytest.mark.asyncio
async def test_summary_includes_subflows(sf_client):
    res = await flows.get_flow_summary(sf_client)
    by_id = {s["id"]: s for s in res["subflows"]}
    assert by_id["sfdef"]["name"] == "seasonSwitch"
    assert by_id["sfdef"]["node_count"] == 3
    assert by_id["sfdef"]["instance_count"] == 1
    assert by_id["nestdef"]["node_count"] == 1
    assert by_id["nestdef"]["instance_count"] == 1   # 'nest' inside sfdef
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_flows.py -k summary_includes_subflows -v`
Expected: FAIL — `KeyError: 'subflows'`.

- [ ] **Step 3: Add the `subflows` section to `get_flow_summary`**

In `src/nrha_mcp/flows.py`, replace the final `return {...}` of `get_flow_summary` with:

```python
    subflows = []
    for sf in (f for f in flows if f.get("type") == "subflow"):
        subflows.append({
            "id": sf["id"], "name": sf.get("name", ""),
            "node_count": sum(1 for f in flows if f.get("z") == sf["id"]),
            "instance_count": sum(1 for f in flows if f.get("type") == f"subflow:{sf['id']}"),
        })
    return {"total_nodes": len(nodes), "total_tabs": len(tabs),
            "tabs": list(tab_map.values()), "subflows": subflows}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_flows.py -k summary -v`
Expected: PASS (`test_summary_counts` and `test_summary_includes_subflows`).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat(flows): get_flow_summary lists subflows with node/instance counts

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Subflow-awareness in `search_nodes`

**Files:**
- Modify: `src/nrha_mcp/flows.py` (`search_nodes`)
- Test: `tests/test_flows.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flows.py`:

```python
@pytest.mark.asyncio
async def test_search_finds_subflow_definition(sf_client):
    res = await flows.search_nodes(sf_client, query="seasonSwitch")
    ids = {r["id"] for r in res["results"]}
    assert "sfdef" in ids        # the definition node is now searchable


@pytest.mark.asyncio
async def test_search_labels_internal_node_with_subflow_name(sf_client):
    res = await flows.search_nodes(sf_client, query="getSeason")
    hit = next(r for r in res["results"] if r["id"] == "getS")
    assert hit["flow_label"] == "seasonSwitch"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_flows.py -k "search_finds_subflow or search_labels" -v`
Expected: FAIL — `sfdef` absent (no `z`, excluded), and `getS`'s `flow_label` is `""`.

- [ ] **Step 3: Make `search_nodes` subflow-aware**

In `src/nrha_mcp/flows.py`, in `search_nodes`, replace the `tabs`/`nodes` setup (the two lines after `flows, _ = await client.get_flows()`) with:

```python
    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    subflow_names = {f["id"]: f.get("name", "") for f in flows if f.get("type") == "subflow"}

    def _label(z: str) -> str:
        return tabs.get(z) or subflow_names.get(z, "")

    nodes = [f for f in flows if f.get("type") not in ("tab", "group")
             and ("z" in f or f.get("type") == "subflow")]
```

Add `"info"` to the searched-fields tuple so subflow definitions match on their info text:

```python
        for field in ("name", "type", "func", "template", "action", "data", "info"):
```

And in the result dict, replace the `flow_label` value so internal-subflow nodes get the subflow name:

```python
                "flow_id": node.get("z", ""), "flow_label": _label(node.get("z", "")),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flows.py -k search -v`
Expected: PASS (existing `test_search_by_type` plus the two new tests).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat(flows): search includes subflow defs; labels internal nodes

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — all pre-existing tests plus the new subflow tests; no regressions in `test_flows.py`, `test_server_smoke.py`.

- [ ] **Step 2: (Optional, if Docker available) e2e smoke**

Run:
```bash
docker compose up -d nodered
NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py
docker compose down -v
```
Expected: `OK: 31 tools registered, all 31 expected present.`

- [ ] **Step 3: Final commit if anything was adjusted**

```bash
git add -A && git commit -m "test: full subflow-read suite green

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- `nr_get_subflow` (name/def-id/instance-id) → Tasks 1–3. ✓
- Resolved ports with labels, internal-node annotations, nested pointers, instances → Task 1. ✓
- `get_node_config` awareness → Task 4. ✓
- `get_flow_summary` subflows section → Task 5. ✓
- `search_nodes` defs + labels → Task 6. ✓
- Tool count 30→31 in the 3 sites → Task 3. ✓
- Edge cases (missing labels default `out i`, optional status, multi/zero sources, no-internal-nodes) handled in the Task 1 resolver code. ✓
- Non-goals (no write, no recursive expand, no env merge) → not implemented, by design. ✓

**Placeholder scan:** No TBD/TODO; every code/command step is complete. The Task 1 `get_subflow` shim is explicitly a temporary stub replaced in Task 2 (each task stays green). ✓

**Type consistency:** `_resolve_subflow`, `_find_subflow_def_id`, `_subflow_name`, `_port_sources`, `get_subflow` names are consistent across tasks; annotation keys (`_targets`, `_feeds_outputs`, `_feeds_status`, `_nested_subflow`, `_ports`, `_subflow_def`) match between resolver (Task 1) and consumers (Task 4). ✓
