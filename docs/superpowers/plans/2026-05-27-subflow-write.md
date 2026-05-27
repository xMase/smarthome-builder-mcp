# Subflow Write (structure-aware) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four structure-aware, deploy-gated subflow-write tools — `nr_instantiate_subflow`, `nr_add_subflow_port`, `nr_remove_subflow_port`, `nr_delete_subflow` — that keep a subflow's definition and all its instances in sync.

**Architecture:** New module `src/nrha_mcp/subflow_edit.py`. Each function builds a pure `rebuild(flows)` closure and hands it to `deploy.commit` (propose/confirm preview, backup-first POST, optimistic-lock retry) — the same pattern as `edit.py`. Reuses `_resolve_subflow` and `_find_subflow_def_id` from `flows.py` (sub-project A). Server wrappers mirror the existing mutating tools.

**Tech Stack:** Python 3.11+, FastMCP (`mcp` SDK), `pytest`/`pytest-asyncio` (`asyncio_mode=auto`), `FakeNRClient` + `BackupStore` test doubles.

Spec: `docs/superpowers/specs/2026-05-27-subflow-write-design.md`. Branch `feat/subflow-write` (already created off `main`, which has 31 tools).

**File structure:**
- Create `src/nrha_mcp/subflow_edit.py` — the four domain functions + `_new_node_id`.
- Create `tests/test_subflow_edit.py` — domain-level tests with `FakeNRClient`.
- Modify `src/nrha_mcp/server.py` — register the four tools.
- Modify `tests/test_server_smoke.py` and `scripts/smoke_e2e.py` — tool inventory (31→35).

---

### Task 1: Module + `_new_node_id` + `instantiate_subflow`

**Files:**
- Create: `src/nrha_mcp/subflow_edit.py`
- Test: `tests/test_subflow_edit.py`

- [ ] **Step 1: Write the failing tests + shared fixture.** Create `tests/test_subflow_edit.py`:

```python
# tests/test_subflow_edit.py
import pytest

from nrha_mcp import subflow_edit
from nrha_mcp.backup import BackupStore
from nrha_mcp.client import NRError, NRNotFoundError
from tests.conftest import FakeNRClient

SF_FLOWS = [
    {"id": "tab1", "type": "tab", "label": "Main"},
    {"id": "sf", "type": "subflow", "name": "mySub",
     "in": [{"x": 80, "y": 80, "wires": [{"id": "inode"}]}],
     "out": [
         {"x": 600, "y": 40, "wires": [{"id": "inode", "port": 0}]},
         {"x": 600, "y": 100, "wires": [{"id": "inode", "port": 1}]},
     ],
     "outputLabels": ["a", "b"]},
    {"id": "inode", "type": "function", "z": "sf", "name": "fn", "wires": [[], []]},
    {"id": "inst1", "type": "subflow:sf", "z": "tab1", "name": "i1", "wires": [["d1"], []]},
    {"id": "inst2", "type": "subflow:sf", "z": "tab1", "name": "i2", "wires": [[], []]},
    {"id": "d1", "type": "debug", "z": "tab1", "wires": [[]]},
]


def _client():
    return FakeNRClient(flows=SF_FLOWS)


@pytest.mark.asyncio
async def test_instantiate_creates_instance_with_correct_slots(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.instantiate_subflow(
        client, store, settings_auto,
        subflow="mySub", z="tab1", name="i3",
        env=[{"name": "X", "value": "1"}], confirm=True)
    assert res["deployed"] is True
    new_id = res["node_id"]
    flows, _ = await client.get_flows()
    node = next(f for f in flows if f["id"] == new_id)
    assert node["type"] == "subflow:sf"
    assert node["z"] == "tab1"
    assert node["name"] == "i3"
    assert node["wires"] == [[], []]          # one empty slot per output (2 outputs)
    assert node["env"] == [{"name": "X", "value": "1"}]


@pytest.mark.asyncio
async def test_instantiate_unknown_tab_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.instantiate_subflow(
            client, store, settings_auto, subflow="mySub", z="nope", confirm=True)


@pytest.mark.asyncio
async def test_instantiate_unknown_subflow_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRNotFoundError):
        await subflow_edit.instantiate_subflow(
            client, store, settings_auto, subflow="ghost", z="tab1", confirm=True)
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nrha_mcp.subflow_edit'`.

- [ ] **Step 3: Create the module with `_new_node_id` + `instantiate_subflow`.** Create `src/nrha_mcp/subflow_edit.py`:

```python
"""Structure-aware subflow write tools, on the deploy gate (deploy.commit).

Each function builds a pure rebuild(flows) closure (re-run on optimistic-lock
conflict) and reuses the sub-project A read resolvers in flows.py.
"""

import copy
import secrets

from .client import NRError, NRNotFoundError  # noqa: F401  (NRNotFoundError via flows helpers)
from .deploy import commit
from .flows import _resolve_subflow, _find_subflow_def_id


def _new_node_id(existing: set[str]) -> str:
    nid = secrets.token_hex(8)                 # 16 hex chars, Node-RED style
    while nid in existing:
        nid = secrets.token_hex(8)
    return nid


async def instantiate_subflow(client, store, settings, *, subflow, z, name=None,
                              env=None, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)          # raises NRNotFoundError
    if not any(f.get("id") == z and f.get("type") == "tab" for f in flows):
        raise NRError(f"Target tab '{z}' not found")
    n_out = len(_resolve_subflow(flows, def_id)["outputs"])
    new_id = _new_node_id({f["id"] for f in flows})        # once, before commit
    node = {"id": new_id, "type": f"subflow:{def_id}", "z": z, "name": name or "",
            "env": env or [], "wires": [[] for _ in range(n_out)], "x": 100, "y": 100}

    def rebuild(fl):
        return copy.deepcopy(fl) + [copy.deepcopy(node)]

    result = await commit(client, store, settings, rebuild=rebuild,
                          summary=f"instantiate subflow {def_id} on {z} as {new_id}",
                          confirm=confirm)
    result["node_id"] = new_id
    return result
```

- [ ] **Step 4: Run to verify pass.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit.**

```bash
git add src/nrha_mcp/subflow_edit.py tests/test_subflow_edit.py
git commit -m "feat(subflow_edit): instantiate_subflow with correct output slot count

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

Then run the FULL suite (`.venv/bin/pytest -q`) and confirm no regressions.

---

### Task 2: `add_subflow_port`

**Files:**
- Modify: `src/nrha_mcp/subflow_edit.py`
- Test: `tests/test_subflow_edit.py`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_subflow_edit.py`:

```python
@pytest.mark.asyncio
async def test_add_port_syncs_definition_and_all_instances(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.add_subflow_port(
        client, store, settings_auto, subflow="sf", label="c", confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == "sf")
    assert len(sf["out"]) == 3
    assert sf["outputLabels"] == ["a", "b", "c"]
    for iid in ("inst1", "inst2"):
        inst = next(f for f in flows if f["id"] == iid)
        assert len(inst["wires"]) == 3
        assert inst["wires"][2] == []          # new slot present and empty


@pytest.mark.asyncio
async def test_add_port_with_source_wires_internal_node(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    await subflow_edit.add_subflow_port(
        client, store, settings_auto, subflow="sf",
        source={"node_id": "inode", "port": 1}, confirm=True)
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == "sf")
    assert sf["out"][2]["wires"] == [{"id": "inode", "port": 1}]


@pytest.mark.asyncio
async def test_add_port_bad_source_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        # d1 is on tab1, not an internal node of subflow sf
        await subflow_edit.add_subflow_port(
            client, store, settings_auto, subflow="sf",
            source={"node_id": "d1"}, confirm=True)


@pytest.mark.asyncio
async def test_add_port_propose_mode_writes_nothing(settings_propose, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.add_subflow_port(
        client, store, settings_propose, subflow="sf", label="c", confirm=False)
    assert res["deployed"] is False
    assert "preview" in res
    assert client.post_calls == []
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k add_port -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.subflow_edit' has no attribute 'add_subflow_port'`.

- [ ] **Step 3: Add `add_subflow_port` to `subflow_edit.py`** (after `instantiate_subflow`):

```python
async def add_subflow_port(client, store, settings, *, subflow, label=None,
                           source=None, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)
    if source is not None:
        sid = source.get("node_id")
        if not any(f.get("id") == sid and f.get("z") == def_id for f in flows):
            raise NRError(f"source node '{sid}' is not an internal node of subflow '{def_id}'")

    def rebuild(fl):
        new = copy.deepcopy(fl)
        sf = next(f for f in new if f.get("id") == def_id and f.get("type") == "subflow")
        out = sf.setdefault("out", [])
        wires = [{"id": source["node_id"], "port": source.get("port", 0)}] if source else []
        out.append({"x": 600, "y": 40 + 60 * len(out), "wires": wires})
        labels = sf.setdefault("outputLabels", [])
        while len(labels) < len(out) - 1:        # pad any pre-existing missing labels
            labels.append("")
        labels.append(label if label is not None else f"out {len(out) - 1}")
        for f in new:
            if f.get("type") == f"subflow:{def_id}":
                f.setdefault("wires", []).append([])
        return new

    return await commit(client, store, settings, rebuild=rebuild,
                        summary=f"add output port to subflow {def_id}", confirm=confirm)
```

- [ ] **Step 4: Run to verify pass.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k add_port -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit.**

```bash
git add src/nrha_mcp/subflow_edit.py tests/test_subflow_edit.py
git commit -m "feat(subflow_edit): add_subflow_port syncs definition + all instances

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

Then run the FULL suite (`.venv/bin/pytest -q`) and confirm no regressions.

---

### Task 3: `remove_subflow_port`

**Files:**
- Modify: `src/nrha_mcp/subflow_edit.py`
- Test: `tests/test_subflow_edit.py`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_subflow_edit.py`:

```python
@pytest.mark.asyncio
async def test_remove_port_syncs_definition_and_all_instances(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.remove_subflow_port(
        client, store, settings_auto, subflow="sf", index=0, confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == "sf")
    assert len(sf["out"]) == 1
    assert sf["outputLabels"] == ["b"]
    inst1 = next(f for f in flows if f["id"] == "inst1")
    assert inst1["wires"] == [[]]            # old slot 0 (->d1) gone; slot 1 ([]) remains


@pytest.mark.asyncio
async def test_remove_port_out_of_range_raises(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.remove_subflow_port(
            client, store, settings_auto, subflow="sf", index=5, confirm=True)
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k remove_port -v`
Expected: FAIL — `AttributeError: ... no attribute 'remove_subflow_port'`.

- [ ] **Step 3: Add `remove_subflow_port` to `subflow_edit.py`:**

```python
async def remove_subflow_port(client, store, settings, *, subflow, index, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)

    def rebuild(fl):
        new = copy.deepcopy(fl)
        sf = next(f for f in new if f.get("id") == def_id and f.get("type") == "subflow")
        out = sf.get("out", [])
        if not (0 <= index < len(out)):
            raise NRError(f"Output port {index} out of range (0..{len(out) - 1})")
        del out[index]
        labels = sf.get("outputLabels", [])
        if index < len(labels):
            del labels[index]
        for f in new:
            if f.get("type") == f"subflow:{def_id}":
                w = f.get("wires", [])
                if index < len(w):
                    del w[index]
        return new

    return await commit(client, store, settings, rebuild=rebuild,
                        summary=f"remove output port {index} from subflow {def_id}",
                        confirm=confirm)
```

- [ ] **Step 4: Run to verify pass.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k remove_port -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit.**

```bash
git add src/nrha_mcp/subflow_edit.py tests/test_subflow_edit.py
git commit -m "feat(subflow_edit): remove_subflow_port syncs definition + all instances

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

Then run the FULL suite (`.venv/bin/pytest -q`) and confirm no regressions.

---

### Task 4: `delete_subflow`

**Files:**
- Modify: `src/nrha_mcp/subflow_edit.py`
- Test: `tests/test_subflow_edit.py`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_subflow_edit.py`:

```python
@pytest.mark.asyncio
async def test_delete_refuses_with_instances(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.delete_subflow(
            client, store, settings_auto, subflow="sf", cascade=False, confirm=True)
    assert client.post_calls == []           # nothing written


@pytest.mark.asyncio
async def test_delete_cascade_removes_everything(settings_auto, tmp_path):
    client = _client()
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.delete_subflow(
        client, store, settings_auto, subflow="sf", cascade=True, confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    ids = {f["id"] for f in flows}
    assert ids == {"tab1", "d1"}             # def sf, inode, inst1, inst2 all removed


@pytest.mark.asyncio
async def test_delete_no_instances_removes_def_and_internals(settings_auto, tmp_path):
    flows0 = [
        {"id": "tab1", "type": "tab", "label": "Main"},
        {"id": "sf", "type": "subflow", "name": "mySub", "in": [], "out": [], "outputLabels": []},
        {"id": "inode", "type": "function", "z": "sf", "wires": [[]]},
    ]
    client = FakeNRClient(flows=flows0)
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.delete_subflow(
        client, store, settings_auto, subflow="sf", confirm=True)
    assert res["deployed"] is True
    flows, _ = await client.get_flows()
    assert {f["id"] for f in flows} == {"tab1"}
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k delete -v`
Expected: FAIL — `AttributeError: ... no attribute 'delete_subflow'`.

- [ ] **Step 3: Add `delete_subflow` to `subflow_edit.py`:**

```python
async def delete_subflow(client, store, settings, *, subflow, cascade=False, confirm=False):
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, subflow)

    def rebuild(fl):
        inst_type = f"subflow:{def_id}"
        instances = [f["id"] for f in fl if f.get("type") == inst_type]
        if instances and not cascade:
            raise NRError(f"Subflow '{def_id}' has {len(instances)} instance(s): "
                          f"{instances}. Pass cascade=true to delete them too, or "
                          f"remove them first.")
        remove = {def_id} | {f["id"] for f in fl if f.get("z") == def_id}
        if cascade:
            remove |= set(instances)
        new = [copy.deepcopy(f) for f in fl if f["id"] not in remove]
        for f in new:
            if f.get("type") == "group" and "nodes" in f:
                f["nodes"] = [n for n in f["nodes"] if n not in remove]
            if "wires" in f:
                f["wires"] = [[w for w in out if w not in remove] for out in f["wires"]]
        return new

    summary = f"delete subflow {def_id}" + (" (cascade)" if cascade else "")
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)
```

- [ ] **Step 4: Run to verify pass.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k delete -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit.**

```bash
git add src/nrha_mcp/subflow_edit.py tests/test_subflow_edit.py
git commit -m "feat(subflow_edit): delete_subflow safe (refuse/cascade on instances)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

Then run the FULL suite (`.venv/bin/pytest -q`) and confirm no regressions.

---

### Task 5: Register the four tools + smoke updates

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py` (`expected` set + count 31 → 35)
- Modify: `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`)

- [ ] **Step 1: Update the smoke test to expect the four tools (fails first).** In `tests/test_server_smoke.py`, add to the `expected` set:
`"nr_instantiate_subflow", "nr_add_subflow_port", "nr_remove_subflow_port", "nr_delete_subflow"`, and change the final assertion to:

```python
    assert len(names) == 35
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `assert 31 == 35`.

- [ ] **Step 3: Register the tools in `server.py`.** First add `subflow_edit` to the package import line:

```python
from . import flows, edit, modules, ha, runtime, validate, deploy, diagnostics, ha_stats, subflow_edit
```

Then, after the `nr_create_nodes` tool (end of the mutating section), add:

```python
@mcp.tool()
async def nr_instantiate_subflow(subflow: str, z: str, name: str | None = None, env: list[dict] | None = None, confirm: bool = False) -> str:
    """Instantiate a subflow (by name/def-id/instance-id) on tab `z` with the correct number of output wire slots. Returns the new node_id. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.instantiate_subflow(client, store, settings, subflow=subflow, z=z, name=name, env=env, confirm=confirm))


@mcp.tool()
async def nr_add_subflow_port(subflow: str, label: str | None = None, source: dict | None = None, confirm: bool = False) -> str:
    """Append an output port to a subflow, synced to the definition AND every instance's wires. `source`={node_id,port} wires the new port to an internal node. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.add_subflow_port(client, store, settings, subflow=subflow, label=label, source=source, confirm=confirm))


@mcp.tool()
async def nr_remove_subflow_port(subflow: str, index: int, confirm: bool = False) -> str:
    """Remove output port `index` from a subflow, synced to the definition AND every instance's wires. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.remove_subflow_port(client, store, settings, subflow=subflow, index=index, confirm=confirm))


@mcp.tool()
async def nr_delete_subflow(subflow: str, cascade: bool = False, confirm: bool = False) -> str:
    """Delete a subflow definition + its internal nodes. Refuses if instances exist unless cascade=true (which also deletes the instances). Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.delete_subflow(client, store, settings, subflow=subflow, cascade=cascade, confirm=confirm))
```

- [ ] **Step 4: Add the four names to the e2e smoke list.** In `scripts/smoke_e2e.py`, add to `EXPECTED_TOOLS`:
`"nr_instantiate_subflow", "nr_add_subflow_port", "nr_remove_subflow_port", "nr_delete_subflow"`.

- [ ] **Step 5: Run the smoke test to verify pass.**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): register 4 subflow-write tools (31->35)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit suite.**

Run: `.venv/bin/pytest -q`
Expected: PASS — pre-existing tests plus the new `test_subflow_edit.py` (12 new tests) and the updated smoke count (35).

- [ ] **Step 2: Final commit if anything was adjusted.**

```bash
git add -A && git commit -m "test: full subflow-write suite green

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- `nr_instantiate_subflow` (correct slot count, env, returns node_id) → Task 1 + Task 5. ✓
- `nr_add_subflow_port` (sync def + all instances, optional `source`) → Task 2 + Task 5. ✓
- `nr_remove_subflow_port` (sync def + all instances, range check) → Task 3 + Task 5. ✓
- `nr_delete_subflow` (refuse vs cascade, clean wires) → Task 4 + Task 5. ✓
- Deploy-gate reuse (propose/confirm, backup) → every function calls `commit`; propose-mode tested in Task 2. ✓
- Reuse of `_resolve_subflow`/`_find_subflow_def_id` → imported in Task 1, used throughout. ✓
- Tool count 31→35 in the 3 sites → Task 5. ✓
- Error handling (unknown subflow/tab, out-of-range, bad source) → tested in Tasks 1-3. ✓
- Non-goals (no `nr_create_subflow`, no input/status ports, no rewire-existing-port) → not implemented, by design. ✓

**Placeholder scan:** No TBD/TODO; every code/command step is complete. ✓

**Type consistency:** Function names (`instantiate_subflow`, `add_subflow_port`, `remove_subflow_port`, `delete_subflow`, `_new_node_id`) and signatures `(client, store, settings, *, …, confirm=False)` are consistent between the domain module (Tasks 1-4) and the server wrappers (Task 5). Param names (`subflow`, `z`, `label`, `source`, `index`, `cascade`) match across tasks. The `source` dict shape `{node_id, port}` is consistent between Task 2 code and its tests. ✓
