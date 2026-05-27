# Subflow Create (from scratch) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `nr_create_subflow` — one deploy-gated tool that assembles a subflow definition (definition node + internal nodes + validated port wiring) from a high-level spec, in a single atomic operation.

**Architecture:** A new `create_subflow` function in `src/nrha_mcp/subflow_edit.py`, reusing `_new_node_id` and the `deploy.commit` gate (pure `rebuild` closure). Internal node ids are caller-supplied; the tool generates only the definition id, sets `z` on internals, builds the `in`/`out`/`status`/`outputLabels` arrays, and validates that every port reference points to a provided internal node.

**Tech Stack:** Python 3.11+, FastMCP (`mcp` SDK), `pytest`/`pytest-asyncio`, `FakeNRClient` + `BackupStore` test doubles.

Spec: `docs/superpowers/specs/2026-05-27-subflow-create-design.md`. Branch `feat/subflow-create` (off `main`, which has 35 tools). `tests/test_subflow_edit.py` already exists with `FakeNRClient`, `BackupStore`, `NRError`/`NRNotFoundError` imports, and `settings_auto`/`settings_propose` fixtures.

---

### Task 1: `create_subflow` domain function

**Files:**
- Modify: `src/nrha_mcp/subflow_edit.py`
- Test: `tests/test_subflow_edit.py`

- [ ] **Step 1: Append these tests to `tests/test_subflow_edit.py`:**

```python
@pytest.mark.asyncio
async def test_create_subflow_builds_definition_and_ports(settings_auto, tmp_path):
    client = FakeNRClient(flows=[{"id": "tab1", "type": "tab", "label": "Main"}])
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.create_subflow(
        client, store, settings_auto,
        name="newSub",
        internal_nodes=[
            {"id": "a", "type": "switch", "wires": [[], []]},
            {"id": "b", "type": "change", "wires": [[]]},
        ],
        outputs=[
            {"label": "lo", "source": {"node_id": "a", "port": 0}},
            {"label": "hi", "source": {"node_id": "a", "port": 1}},
        ],
        input_to="a",
        status_from={"node_id": "b", "port": 0},
        confirm=True)
    assert res["deployed"] is True
    def_id = res["subflow_id"]
    flows, _ = await client.get_flows()
    sf = next(f for f in flows if f["id"] == def_id)
    assert sf["type"] == "subflow"
    assert sf["name"] == "newSub"
    assert sf["in"][0]["wires"] == [{"id": "a"}]
    assert sf["outputLabels"] == ["lo", "hi"]
    assert sf["out"][1]["wires"] == [{"id": "a", "port": 1}]
    assert sf["status"]["wires"] == [{"id": "b", "port": 0}]
    a = next(f for f in flows if f["id"] == "a")
    assert a["z"] == def_id          # internal node attached to the definition


@pytest.mark.asyncio
async def test_create_subflow_bad_output_source_raises(settings_auto, tmp_path):
    client = FakeNRClient(flows=[{"id": "tab1", "type": "tab", "label": "Main"}])
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.create_subflow(
            client, store, settings_auto, name="x",
            internal_nodes=[{"id": "a", "type": "change", "wires": [[]]}],
            outputs=[{"source": {"node_id": "ghost"}}], confirm=True)


@pytest.mark.asyncio
async def test_create_subflow_internal_id_clash_raises(settings_auto, tmp_path):
    client = FakeNRClient(flows=[
        {"id": "tab1", "type": "tab", "label": "Main"},
        {"id": "dup", "type": "debug", "z": "tab1", "wires": [[]]},
    ])
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await subflow_edit.create_subflow(
            client, store, settings_auto, name="x",
            internal_nodes=[{"id": "dup", "type": "change", "wires": [[]]}],
            outputs=[], confirm=True)


@pytest.mark.asyncio
async def test_create_subflow_propose_writes_nothing(settings_propose, tmp_path):
    client = FakeNRClient(flows=[{"id": "tab1", "type": "tab", "label": "Main"}])
    store = BackupStore(str(tmp_path))
    res = await subflow_edit.create_subflow(
        client, store, settings_propose, name="x",
        internal_nodes=[{"id": "a", "type": "change", "wires": [[]]}],
        outputs=[{"source": {"node_id": "a"}}], confirm=False)
    assert res["deployed"] is False
    assert "subflow_id" in res
    assert client.post_calls == []
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k create_subflow -v`
Expected: FAIL — `AttributeError: module 'nrha_mcp.subflow_edit' has no attribute 'create_subflow'`.

- [ ] **Step 3: Add `create_subflow` to `src/nrha_mcp/subflow_edit.py`** (after `delete_subflow`):

```python
async def create_subflow(client, store, settings, *, name, internal_nodes, outputs,
                         input_to=None, status_from=None, env=None, confirm=False):
    for n in internal_nodes:
        if not n.get("id") or not n.get("type"):
            raise NRError(f"internal node missing 'id' or 'type': {n}")
    internal_ids = [n["id"] for n in internal_nodes]
    if len(set(internal_ids)) != len(internal_ids):
        raise NRError(f"duplicate internal node ids: {internal_ids}")
    input_list = ([] if input_to is None
                  else [input_to] if isinstance(input_to, str) else list(input_to))

    flows, _ = await client.get_flows()
    def_id = _new_node_id({f["id"] for f in flows} | set(internal_ids))

    id_set = set(internal_ids)
    for o in outputs:
        if o.get("source", {}).get("node_id") not in id_set:
            raise NRError(f"output source '{o.get('source', {}).get('node_id')}' "
                          f"is not one of the internal nodes")
    for t in input_list:
        if t not in id_set:
            raise NRError(f"input_to '{t}' is not one of the internal nodes")
    if status_from is not None and status_from.get("node_id") not in id_set:
        raise NRError(f"status_from '{status_from.get('node_id')}' "
                      f"is not one of the internal nodes")

    def rebuild(fl):
        existing = {f["id"] for f in fl}
        clash = (set(internal_ids) | {def_id}) & existing
        if clash:
            raise NRError(f"node id(s) already exist: {sorted(clash)}")
        def_node = {
            "id": def_id, "type": "subflow", "name": name,
            "in": ([{"x": 60, "y": 60, "wires": [{"id": t} for t in input_list]}]
                   if input_list else []),
            "out": [{"x": 600, "y": 40 + 60 * i,
                     "wires": [{"id": o["source"]["node_id"],
                                "port": o["source"].get("port", 0)}]}
                    for i, o in enumerate(outputs)],
            "outputLabels": [o.get("label") or f"out {i}" for i, o in enumerate(outputs)],
            "env": env or [], "color": "#DDAA99",
        }
        if status_from is not None:
            def_node["status"] = {"x": 600, "y": 40 + 60 * len(outputs),
                                  "wires": [{"id": status_from["node_id"],
                                             "port": status_from.get("port", 0)}]}
        internals = []
        for n in internal_nodes:
            m = copy.deepcopy(n)
            m["z"] = def_id
            internals.append(m)
        return copy.deepcopy(fl) + [def_node] + internals

    result = await commit(client, store, settings, rebuild=rebuild,
                          summary=f"create subflow {def_id} ({name})", confirm=confirm)
    result["subflow_id"] = def_id
    return result
```

- [ ] **Step 4: Run to verify pass.**

Run: `.venv/bin/pytest tests/test_subflow_edit.py -k create_subflow -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit.**

```bash
git add src/nrha_mcp/subflow_edit.py tests/test_subflow_edit.py
git commit -m "feat(subflow_edit): create_subflow assembles definition + ports

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

Then run the FULL suite (`.venv/bin/pytest -q`) and confirm no regressions.

---

### Task 2: Register the `nr_create_subflow` tool + smoke updates

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py` (`expected` set + count 35 → 36)
- Modify: `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`)

- [ ] **Step 1: Update the smoke test (fails first).** In `tests/test_server_smoke.py`, add `"nr_create_subflow"` to the `expected` set and change the final assertion to:

```python
    assert len(names) == 36
```

- [ ] **Step 2: Run to verify failure.**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `assert 35 == 36`.

- [ ] **Step 3: Register the tool in `server.py`** (after `nr_delete_subflow`, end of the mutating section):

```python
@mcp.tool()
async def nr_create_subflow(name: str, internal_nodes: list[dict], outputs: list[dict],
                            input_to: str | list[str] | None = None,
                            status_from: dict | None = None,
                            env: list[dict] | None = None, confirm: bool = False) -> str:
    """Create a subflow definition from scratch: assembles the definition node +
    internal nodes (z auto-set) + validated port wiring in one atomic deploy.
    `outputs`=[{label?, source:{node_id, port?}}]; `input_to`=internal id or list;
    `status_from`={node_id, port?}. Returns subflow_id. Confirm-gated, backup-first."""
    return await _wrap(lambda: subflow_edit.create_subflow(client, store, settings,
        name=name, internal_nodes=internal_nodes, outputs=outputs,
        input_to=input_to, status_from=status_from, env=env, confirm=confirm))
```

(`subflow_edit` is already imported in `server.py`.)

- [ ] **Step 4: Add the tool to the e2e smoke list.** In `scripts/smoke_e2e.py`, add `"nr_create_subflow",` to the `EXPECTED_TOOLS` set.

- [ ] **Step 5: Run the smoke test to verify pass.**

Run: `.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): register nr_create_subflow tool (35->36)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit suite.**

Run: `.venv/bin/pytest -q`
Expected: PASS — pre-existing tests plus the 4 new `create_subflow` tests and the updated smoke count (36).

- [ ] **Step 2: Final commit if anything was adjusted.**

```bash
git add -A && git commit -m "test: full subflow-create suite green

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- `create_subflow` signature (`name`, `internal_nodes`, `outputs`, `input_to`, `status_from`, `env`, `confirm`) → Task 1. ✓
- Caller-supplied internal ids; tool generates only `def_id` (before `commit`); returns `subflow_id` → Task 1 code. ✓
- Builds `in`/`out`/`status`/`outputLabels`; sets `z` on internals → Task 1 code + happy-path test. ✓
- Validation: missing id/type, duplicate internal ids, id clash, port reference not internal → Task 1 code + the bad-source and id-clash tests. ✓
- `input_to` single-or-list normalisation → Task 1 code (happy path uses single). ✓
- Deploy-gate reuse + propose mode → Task 1 propose test. ✓
- Tool registration + count 35→36 in the 3 sites → Task 2. ✓
- Non-goals (no internal-id generation/remap, no internal-wire validation, single input, no category/color params) → not implemented, by design. ✓

**Placeholder scan:** No TBD/TODO; every code/command step is complete. ✓

**Type consistency:** `create_subflow` keyword params (`name`, `internal_nodes`, `outputs`, `input_to`, `status_from`, `env`, `confirm`) match between the domain function (Task 1) and the server wrapper (Task 2). The `outputs` element shape `{label?, source:{node_id, port?}}` and `status_from` shape `{node_id, port?}` are consistent between the code, the tests, and the tool docstring. Returns `subflow_id` in both code and tests. ✓
