# Flow Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `nr_validate_flow` tool that structurally validates a Node-RED flow (current, or current ∪ proposed nodes) and reports errors and warnings without deploying.

**Architecture:** A pure, catalog-free engine `validate.validate_flows(flows, ...)` returns `{valid, errors, warnings, checked}`. A thin async `validate.validate_flow(client, nodes=None)` fetches current flows + registered types and runs the engine. `server.py` exposes it as `nr_validate_flow`.

**Tech Stack:** Python 3.11+, FastMCP (`mcp`), pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-05-26-validate-flow-design.md`

---

## File structure

- Create `src/nrha_mcp/validate.py` — pure `validate_flows` engine + async `validate_flow` orchestrator + `_registered_types` helper.
- Modify `src/nrha_mcp/server.py` — import `validate`; add the `nr_validate_flow` tool.
- Create `tests/test_validate.py` — engine + orchestrator tests.
- Modify `tests/test_server_smoke.py` — tool count 24 → 25.
- Modify `scripts/smoke_e2e.py` — add the tool to `EXPECTED_TOOLS`.

All commands from `/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`. Tests: `./.venv/bin/pytest` (uv venv, no `pip`). Branch: `feat/validate-flow`.

---

## Task 1: Pure validation engine

**Files:**
- Create: `src/nrha_mcp/validate.py`
- Test: `tests/test_validate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_validate.py`:

```python
from nrha_mcp.validate import validate_flows


def _tab(id="t1"):
    return {"id": id, "type": "tab"}


def test_valid_flow():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1", "wires": [[]]}]
    res = validate_flows(flows)
    assert res["valid"] is True
    assert res["errors"] == []
    assert res["checked"] == 2


def test_duplicate_id():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1"},
             {"id": "n1", "type": "debug", "z": "t1"}]
    res = validate_flows(flows)
    assert res["valid"] is False
    assert any(e["code"] == "duplicate_id" for e in res["errors"])


def test_wire_to_missing():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1", "wires": [["ghost"]]}]
    res = validate_flows(flows)
    assert any(e["code"] == "wire_to_missing" and e["node_id"] == "n1" for e in res["errors"])


def test_bad_z():
    flows = [{"id": "n1", "type": "function", "z": "nope", "wires": [[]]}]
    res = validate_flows(flows)
    assert any(e["code"] == "bad_z" for e in res["errors"])


def test_missing_type():
    flows = [_tab(), {"id": "n1", "z": "t1"}]
    res = validate_flows(flows)
    assert any(e["code"] == "missing_type" for e in res["errors"])


def test_group_member_missing():
    flows = [_tab(), {"id": "g1", "type": "group", "z": "t1", "nodes": ["ghost"]}]
    res = validate_flows(flows)
    assert any(e["code"] == "group_member_missing" for e in res["errors"])


def test_unregistered_type_is_warning():
    flows = [_tab(), {"id": "n1", "type": "weird-node", "z": "t1", "wires": [[]]}]
    res = validate_flows(flows, registered_types={"function", "debug"})
    assert res["valid"] is True  # warnings don't fail
    assert any(w["code"] == "unregistered_type" for w in res["warnings"])


def test_subflow_instance_and_structural_types_exempt_from_type_warning():
    flows = [_tab(),
             {"id": "n1", "type": "function", "z": "t1", "wires": [[]]},
             {"id": "n2", "type": "subflow:abc", "z": "t1", "wires": [[]]}]
    res = validate_flows(flows, registered_types={"function"})
    assert not any(w["code"] == "unregistered_type" for w in res["warnings"])


def test_server_ref_missing_is_warning():
    flows = [_tab(), {"id": "n1", "type": "server-state-changed", "z": "t1",
                      "server": "ghost", "wires": [[]]}]
    res = validate_flows(flows)
    assert any(w["code"] == "server_ref_missing" for w in res["warnings"])


def test_valid_server_ref_no_warning():
    flows = [_tab(),
             {"id": "srv1", "type": "server", "z": "", "name": "HA"},
             {"id": "n1", "type": "server-state-changed", "z": "t1",
              "server": "srv1", "wires": [[]]}]
    res = validate_flows(flows)
    assert not any(w["code"] == "server_ref_missing" for w in res["warnings"])


def test_focus_ids_marks_findings():
    flows = [_tab(), {"id": "n1", "type": "function", "z": "t1", "wires": [["ghost"]]}]
    res = validate_flows(flows, focus_ids={"n1"})
    err = next(e for e in res["errors"] if e["code"] == "wire_to_missing")
    assert err.get("focus") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_validate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nrha_mcp.validate'`

- [ ] **Step 3: Implement the engine**

Create `src/nrha_mcp/validate.py`:

```python
"""Structural, catalog-free validation of Node-RED flow node lists.

Pure functions (no I/O): given a node list, report errors (high-precision,
unambiguous breakages) and warnings (heuristic). See
docs/superpowers/specs/2026-05-26-validate-flow-design.md.
"""

_CONTAINER_TYPES = {"tab", "subflow"}        # valid targets for a node's `z`
_STRUCTURAL_TYPES = {"tab", "subflow", "group"}  # not real node "sets" -> skip type check


def _finding(code, node_id, message, focus_ids):
    f = {"code": code, "node_id": node_id, "message": message}
    if focus_ids and node_id in focus_ids:
        f["focus"] = True
    return f


def validate_flows(flows, *, focus_ids=None, registered_types=None):
    errors = []
    warnings = []
    ids = set()
    container_ids = set()

    # first pass: identity + container ids
    for n in flows:
        nid = n.get("id")
        if not nid:
            errors.append(_finding("missing_id", None,
                                   f"Node missing 'id' (type {n.get('type', '?')})", focus_ids))
            continue
        if nid in ids:
            errors.append(_finding("duplicate_id", nid, f"Duplicate node id '{nid}'", focus_ids))
        ids.add(nid)
        if n.get("type") in _CONTAINER_TYPES:
            container_ids.add(nid)

    # second pass: per-node checks
    for n in flows:
        nid = n.get("id")
        if not nid:
            continue
        ntype = n.get("type")
        if not ntype:
            errors.append(_finding("missing_type", nid, f"Node '{nid}' missing 'type'", focus_ids))
        z = n.get("z")
        if z and z not in container_ids:
            errors.append(_finding("bad_z", nid,
                                   f"Node '{nid}' has z='{z}', not an existing tab/subflow", focus_ids))
        for out in n.get("wires", []) or []:
            for target in out:
                if target not in ids:
                    errors.append(_finding("wire_to_missing", nid,
                                           f"Node '{nid}' wires to missing node '{target}'", focus_ids))
        if ntype == "group":
            for member in n.get("nodes", []) or []:
                if member not in ids:
                    errors.append(_finding("group_member_missing", nid,
                                           f"Group '{nid}' lists missing member '{member}'", focus_ids))
        # warnings (heuristic)
        if (registered_types is not None and ntype
                and ntype not in _STRUCTURAL_TYPES and not ntype.startswith("subflow:")
                and ntype not in registered_types):
            warnings.append(_finding("unregistered_type", nid,
                                     f"Node '{nid}' has unregistered type '{ntype}'", focus_ids))
        server = n.get("server")
        if isinstance(server, str) and server and server not in ids:
            warnings.append(_finding("server_ref_missing", nid,
                                     f"Node '{nid}' references missing server config '{server}'", focus_ids))

    return {"valid": not errors, "errors": errors, "warnings": warnings, "checked": len(flows)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_validate.py -v`
Expected: all 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/validate.py tests/test_validate.py
git commit -m "feat(validate): pure structural flow-validation engine"
```

---

## Task 2: Orchestrator `validate_flow`

**Files:**
- Modify: `src/nrha_mcp/validate.py`
- Test: `tests/test_validate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_validate.py`:

```python
import pytest

from nrha_mcp import validate


@pytest.mark.asyncio
async def test_validate_flow_audits_current(fake_client):
    # conftest SAMPLE_FLOWS is structurally valid
    res = await validate.validate_flow(fake_client)
    assert res["valid"] is True


@pytest.mark.asyncio
async def test_validate_flow_merges_proposed_nodes(fake_client):
    new = [{"id": "x1", "type": "function", "z": "tab1", "wires": [["ghost"]]}]
    res = await validate.validate_flow(fake_client, nodes=new)
    assert res["valid"] is False
    err = next(e for e in res["errors"] if e["code"] == "wire_to_missing")
    assert err["node_id"] == "x1"
    assert err.get("focus") is True


@pytest.mark.asyncio
async def test_validate_flow_uses_registered_types(fake_client):
    fake_client.nodes = [{"types": ["function", "debug"]}]
    new = [{"id": "x1", "type": "mystery", "z": "tab1", "wires": [[]]}]
    res = await validate.validate_flow(fake_client, nodes=new)
    assert any(w["code"] == "unregistered_type" and w["node_id"] == "x1"
               for w in res["warnings"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_validate.py -k "validate_flow" -v`
Expected: FAIL with `AttributeError: module 'nrha_mcp.validate' has no attribute 'validate_flow'`

- [ ] **Step 3: Implement the orchestrator**

Append to `src/nrha_mcp/validate.py`:

```python
def _registered_types(nodes_payload) -> set:
    """Best-effort: union of node 'types' from the /nodes payload."""
    types = set()
    if isinstance(nodes_payload, list):
        for entry in nodes_payload:
            if isinstance(entry, dict):
                for t in entry.get("types", []) or []:
                    types.add(t)
    return types


async def validate_flow(client, *, nodes=None) -> dict:
    """Validate the current flows, or (current + `nodes`) when nodes are given."""
    flows, _ = await client.get_flows()
    if nodes:
        candidate = flows + nodes
        focus = {n["id"] for n in nodes if n.get("id")}
    else:
        candidate, focus = flows, None
    registered = _registered_types(await client.get_nodes())
    return validate_flows(candidate, focus_ids=focus, registered_types=registered or None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_validate.py -v`
Expected: all pass (11 engine + 3 orchestrator).

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/validate.py tests/test_validate.py
git commit -m "feat(validate): validate_flow orchestrator (current or current+proposed)"
```

---

## Task 3: Register the `nr_validate_flow` tool

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Modify: `tests/test_server_smoke.py`
- Modify: `scripts/smoke_e2e.py`

- [ ] **Step 1: Update the expected-tools test (fails first)**

In `tests/test_server_smoke.py`, add `"nr_validate_flow",` to the `expected` set (e.g. right after `"nr_get_runtime_events",`) and change the count:

```python
        "nr_capture_debug", "nr_get_runtime_events", "nr_validate_flow", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
```

```python
    assert len(names) == 25
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: FAIL — `nr_validate_flow` not registered; `len(names)` is 24, not 25.

- [ ] **Step 3: Import the module**

In `src/nrha_mcp/server.py`, change the import line:

```python
from . import flows, edit, modules, ha, runtime
```

to:

```python
from . import flows, edit, modules, ha, runtime, validate
```

- [ ] **Step 4: Add the tool**

In `src/nrha_mcp/server.py`, immediately AFTER the `nr_get_runtime_events` function (after its closing `))` line), add:

```python


@mcp.tool()
async def nr_validate_flow(nodes: list[dict] | None = None) -> str:
    """Validate flow structure and report errors/warnings WITHOUT deploying.

    With `nodes`: validates (current flows + those nodes) — run it before
    nr_create_nodes/nr_safe_deploy to catch problems early (findings caused by the
    new nodes are marked `focus`). Without `nodes`: audits the current deployed
    flows. Checks references (wires, z/tab, group members), duplicate ids, and node
    types; it does NOT check per-type fields."""
    return await _wrap(lambda: validate.validate_flow(client, nodes=nodes))
```

- [ ] **Step 5: Run the smoke test to verify it passes**

Run: `./.venv/bin/pytest tests/test_server_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 6: Update the e2e smoke script**

In `scripts/smoke_e2e.py`, add `"nr_validate_flow",` to the `EXPECTED_TOOLS` set (e.g. right after `"nr_capture_debug",`). The script asserts `len(EXPECTED_TOOLS)` tools are present, so adding the name keeps it consistent.

- [ ] **Step 7: Commit**

```bash
git add src/nrha_mcp/server.py tests/test_server_smoke.py scripts/smoke_e2e.py
git commit -m "feat(server): add nr_validate_flow tool"
```

---

## Task 4: Full verification

**Files:** none (verification + operator step)

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Confirm the tool surface**

Run: `./.venv/bin/python -c "import nrha_mcp.server as s; n=set(s.mcp._tool_manager._tools); print('validate:', 'nr_validate_flow' in n); print('count:', len(n))"`
Expected: `validate: True` and `count: 25`.

- [ ] **Step 3: Rebuild and redeploy the container (operator step)**

The live MCP on `:8090` runs from the Docker image; rebuild to expose the tool. From the project root: `docker compose build && docker compose up -d` (or redeploy the Portainer stack). This restarts the MCP, so do it when ready.

---

## Follow-ups (deferred, per spec)

- Deploy-gate integration: run `validate_flows` inside `deploy.commit` and surface warnings in the `propose` preview.
- Catalog-based per-type field checks once milestone C lands.

---

## Self-review

- **Spec coverage:** engine (errors + warnings + focus) → Task 1; orchestrator (`validate_flow`, `_registered_types`, merge semantics) → Task 2; tool + 24→25 in both assertion sites → Task 3; verify + deploy → Task 4; gate-integration & catalog → Follow-ups. No gaps.
- **Placeholders:** none — every step has full code and exact commands.
- **Type consistency:** `validate_flows(flows, *, focus_ids, registered_types)` and `validate_flow(client, *, nodes)` signatures are identical across Tasks 1–3 and the `server.py` wrapper. Finding shape (`code`/`node_id`/`message`/optional `focus`) is consistent between engine, tests, and the tool docstring. Result keys (`valid`/`errors`/`warnings`/`checked`) match across tasks. Tool count 25 consistent in Task 3 and Task 4.
