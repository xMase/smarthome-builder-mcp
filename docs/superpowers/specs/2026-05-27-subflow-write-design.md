# Subflow Write (structure-aware) for the nrha MCP — Design

- **Date:** 2026-05-27
- **Status:** Approved (pending spec review)
- **Branch:** `feat/subflow-write`

## Problem

Raw mutating tools already let an agent touch subflows as ordinary nodes:
`nr_create_nodes` accepts `type:"subflow"` (a definition) and `subflow:<id>` instances,
`nr_safe_deploy` can patch `out`/`outputLabels`, `nr_add_wire`/`nr_delete_nodes`
operate on internal nodes. What they **cannot** do correctly is the *structure-aware*
work, because a subflow's shape is split across the definition and every instance:

- **Output ports are not auto-synced.** A subflow instance's `wires` array has one
  slot per definition output port (verified: `seasonSwitch` has 6 outputs and its
  instance `…e02` has a 6-slot `wires`). The Node-RED **editor** keeps instances in
  sync when you add/remove a port; the **Admin API** (which this MCP POSTs to) does
  not. Add a port and forget an instance → that output is silently unreachable for
  it; remove port `i` from the definition but leave the instance's slot → index shift
  and silent mis-wiring.
- **`nr_delete_nodes` orphans instances.** It cleans wires *pointing to* a deleted
  id, but an instance is bound to its definition through its `type` string
  (`subflow:<id>`), not a wire. Delete the definition alone and the instances remain
  with a `type` pointing nowhere — a broken flow that wire-cleanup can't detect.
- **Instantiation slot count is error-prone.** A correct instance needs exactly
  `len(out)` empty `wires` slots; getting it wrong mis-aligns outputs.

This feature adds four structure-aware, deploy-gated tools that own these invariants.
It builds directly on sub-project A's read resolver (`_resolve_subflow`,
`_find_subflow_def_id`).

## Goals

- `nr_instantiate_subflow` — place an instance on a tab with the correct output slot
  count and optional `env`.
- `nr_add_subflow_port` — append an output port, synced to the definition
  (`out` + `outputLabels`) **and** every instance's `wires`.
- `nr_remove_subflow_port` — remove an output port at an index, synced the same way.
- `nr_delete_subflow` — delete a subflow definition + its internal nodes safely:
  refuse when instances exist unless `cascade=true`, which also removes the instances.
- All four reuse the existing deploy gate (`deploy.commit`): pure `rebuild(flows)`
  closure → propose/confirm preview, backup-first POST, optimistic-lock retry.

## Non-goals

- **`nr_create_subflow` (build a definition from scratch** with ports + internal
  nodes). `nr_create_nodes` covers the raw case; a structure-aware authoring helper
  is a **separate, later spec**.
- **Input ports and the status port.** Subflows almost always have one input; adding
  inputs / rewiring status is out of scope (follow-up). This feature is output-port
  centric.
- **Editing existing port→internal wiring** (rewiring a port already present).
  `nr_add_subflow_port` can wire a *new* port to an internal node via `source`;
  rewiring an existing port is a follow-up.
- **Per-instance `env` semantics beyond passthrough** — `env` passed to
  `nr_instantiate_subflow` is stored verbatim; no validation against the definition's
  declared env.

## Design

### A. New module — `src/nrha_mcp/subflow_edit.py`

Keeps subflow-write logic cohesive and leaves `edit.py` focused. Imports:

```python
import copy, secrets
from .client import NRError, NRNotFoundError
from .deploy import commit
from .flows import _resolve_subflow, _find_subflow_def_id
```

Each public function is `async def fn(client, store, settings, *, …, confirm=False)`,
builds a **pure** `rebuild(flows)` closure, and returns `await commit(client, store,
settings, rebuild=rebuild, summary=…, confirm=confirm)` — exactly the `edit.py`
pattern. Server wrappers mirror the mutating tools:
`return await _wrap(lambda: subflow_edit.fn(client, store, settings, …, confirm=confirm))`.

Shared helper:

```python
def _new_node_id(existing: set[str]) -> str:
    nid = secrets.token_hex(8)            # 16 hex chars, Node-RED style
    while nid in existing:
        nid = secrets.token_hex(8)
    return nid
```

### B. `instantiate_subflow(client, store, settings, *, subflow, z, name=None, env=None, confirm=False)`

- `def_id = _find_subflow_def_id(flows, subflow)` (raises `NRNotFoundError` if unknown).
- Validate `z` is an existing `tab` → else `NRError(f"Target tab '{z}' not found")`.
- `n_out = len(_resolve_subflow(flows, def_id)["outputs"])`.
- `new_id = _new_node_id({f["id"] for f in flows})` — generated **once, before**
  `commit`, so a conflict-retry stays deterministic.
- Node built: `{"id": new_id, "type": f"subflow:{def_id}", "z": z, "name": name or "",
  "env": env or [], "wires": [[] for _ in range(n_out)], "x": 100, "y": 100}`.
- `rebuild(fl)` returns `copy.deepcopy(fl) + [copy.deepcopy(node)]`.
- Returns `commit`'s dict with `result["node_id"] = new_id` added (the id that will be
  / was created).

### C. `add_subflow_port(client, store, settings, *, subflow, label=None, source=None, confirm=False)`

`source` (optional): `{"node_id": <internal node id>, "port": <int, default 0>}` —
wires the new output port to an internal node. (This wiring lives in the definition's
`out` array, so `nr_add_wire` cannot do it — hence it belongs here.)

- `def_id = _find_subflow_def_id(flows, subflow)`.
- If `source`: validate `source["node_id"]` exists with `z == def_id` → else `NRError`.
- `rebuild(fl)`:
  - deepcopy; find the `subflow` definition `sf` by `def_id`.
  - `out = sf.setdefault("out", [])`; append
    `{"x": 600, "y": 40 + 60*len(out), "wires": ([{"id": source["node_id"], "port": source.get("port", 0)}] if source else [])}`.
  - `labels = sf.setdefault("outputLabels", [])`; pad with `""` to `len(out)-1`, then
    append `label or f"out {len(out)-1}"` so `len(labels) == len(out)`.
  - For every node with `type == f"subflow:{def_id}"`: `f.setdefault("wires", []).append([])`.

### D. `remove_subflow_port(client, store, settings, *, subflow, index, confirm=False)`

- `def_id = _find_subflow_def_id(flows, subflow)`.
- `rebuild(fl)`:
  - find `sf`; `out = sf.get("out", [])`; if not `0 <= index < len(out)` →
    `NRError(f"Output port {index} out of range (0..{len(out)-1})")`.
  - `del out[index]`; if `index < len(sf.get("outputLabels", []))`: `del outputLabels[index]`.
  - For every instance (`type == f"subflow:{def_id}"`): if `index < len(f["wires"])`,
    `del f["wires"][index]`.
- Internal nodes that fed only this port are left in place (they may be used elsewhere).

### E. `delete_subflow(client, store, settings, *, subflow, cascade=False, confirm=False)`

- `def_id = _find_subflow_def_id(flows, subflow)`.
- `rebuild(fl)`:
  - `instances = [f["id"] for f in fl if f.get("type") == f"subflow:{def_id}"]`.
  - If `instances and not cascade` →
    `NRError(f"Subflow '{def_id}' has {len(instances)} instance(s): {instances}. Pass cascade=true to delete them too, or remove them first.")`
    (raised even in propose mode, so the agent learns immediately).
  - `remove = {def_id} | {f["id"] for f in fl if f.get("z") == def_id} | (set(instances) if cascade else set())`.
  - `new = [deepcopy(f) for f in fl if f["id"] not in remove]`, then for each remaining
    node clean group membership (`f["nodes"]`) and `wires` of any reference in
    `remove` — identical to `edit.delete_nodes`.
- A nested subflow *instance* inside this subflow (its `z == def_id`) is removed; the
  nested *definition* is left intact (may be used elsewhere).

Two independent flags: `cascade` (semantic — also drop instances) and `confirm`
(deploy-gate apply).

## Error handling

Read + pure `rebuild`; `NRError`/`NRNotFoundError` propagate through `_wrap` to a
structured `{"error": …}`. Unknown subflow → `NRNotFoundError`; missing target tab,
port index out of range, `source` node not internal → `NRError`. The deploy gate
always snapshots a backup before any write and re-runs `rebuild` on a conflict.

## Testing

`tests/test_subflow_edit.py` (new), mirroring `tests/test_edit.py`'s setup
(`FakeNRClient`, a `BackupStore` built from the `settings_*` fixtures). Fixture: a
`subflow` definition with 2 outputs + `outputLabels`, one internal node, and **two**
instances on a tab (one wiring some outputs).

- **instantiate:** new instance node has `type == "subflow:<def_id>"`, `len(wires) ==
  len(out)`, correct `z`, `env` applied; returned dict carries `node_id`.
- **add_port:** `out` and `outputLabels` grow by one; **every** instance's `wires`
  grows by one `[]`; with `source`, the new `out` entry wires to the internal node;
  `source` referencing a non-internal id raises.
- **remove_port:** `out`/`outputLabels` lose the given index and **every** instance's
  `wires` loses that slot; out-of-range index raises.
- **delete:** instances present + `cascade=false` raises (and writes nothing);
  `cascade=true` removes definition + internal nodes + instances and cleans dangling
  wires; no instances → removes definition + internal nodes.
- **propose mode** (`settings_propose`, `confirm=false`): returns `deployed: false`
  with a `preview`, and `FakeNRClient.post_calls` stays empty (no write).
- **apply mode** (`settings_auto` or `confirm=true`): `post_flows` called once with the
  proposed flows; a backup snapshot is written.

`tests/test_server_smoke.py`: count **31 → 35**; the four tool names in `expected`.
`scripts/smoke_e2e.py`: add the four names to `EXPECTED_TOOLS`.

## Deployment

Same as prior features: rebuild/redeploy the Docker image to expose the tools live;
local `.venv` for tests.

## Follow-ups

- **`nr_create_subflow`:** structure-aware authoring of a definition (ports + internal
  nodes) from a high-level spec.
- **Input/status ports** and **rewiring an existing output port** to a different
  internal node.
- **`env` validation** of an instance against the definition's declared env.
