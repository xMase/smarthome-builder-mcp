# Subflow Create (from scratch) for the nrha MCP — Design

- **Date:** 2026-05-27
- **Status:** Approved (pending spec review)
- **Branch:** `feat/subflow-create`

## Problem

Creating a Node-RED subflow definition from scratch is *possible* today with raw
`nr_create_nodes` — you can hand it a `type:"subflow"` definition dict plus internal
nodes with `z=<def-id>`. But the caller must hand-author the fragile part: the
`in`/`out`/`status` arrays that reference internal node ids by position, keep
`outputLabels` aligned to `out`, set `z` correctly on every internal node, and
generate a non-colliding definition id. This is exactly the kind of structure-aware
assembly that is easy to get wrong (the same class of problem the subflow-write tools
addressed for editing).

`nr_create_subflow` is a **convenience + safety layer** over `nr_create_nodes`: it
owns the definition assembly and port wiring so the agent declares intent rather than
positional wiring. It is **not** a new capability.

## Goals

- One tool, `nr_create_subflow`, that takes a high-level spec — `name`, the internal
  nodes, and a declarative port mapping (which internal node feeds the input, which
  internal node+port feeds each labelled output, optional status source) — and
  produces a correct subflow definition + internal nodes in a single atomic,
  validated, deploy-gated operation. Returns the new `subflow_id`.
- Validate the fragile references (every output/input/status source is one of the
  provided internal nodes) and reject id clashes.
- Reuse the existing module/pattern: live in `subflow_edit.py`, pure `rebuild` +
  `deploy.commit` (propose/confirm, backup-first, optimistic-lock retry).

## Non-goals

- **Generating internal node ids / remapping references.** Internal node ids are
  caller-supplied (the caller is authoring those nodes anyway); the tool generates
  only the definition id. This avoids reference remapping.
- **Validating internal node→node wiring.** Internal nodes' own `wires` are passed
  through verbatim (the `nr_create_nodes` contract); the tool validates only the
  port→internal references it assembles.
- **Multiple input ports** beyond a single input that may fan out to several internal
  nodes. A second input port is a follow-up.
- **Editing an existing subflow's internals** — that is `nr_create_nodes`/`nr_add_wire`
  (for internal nodes) and the subflow-write tools (for ports), already shipped.

## Design

### A. `create_subflow` — `src/nrha_mcp/subflow_edit.py`

Added to the existing subflow-write module. Reuses `_new_node_id` and `commit`.

```python
async def create_subflow(client, store, settings, *, name, internal_nodes, outputs,
                         input_to=None, status_from=None, env=None, confirm=False):
```

- `name` — subflow name.
- `internal_nodes` — list of node dicts, each with its own `id` and `type` (+ config,
  + internal `wires`). `z` is omitted; the tool sets it to the new def id.
- `outputs` — ordered list of `{label?, source: {node_id, port?}}`. Defines the output
  ports; `label` defaults to `"out i"`; `source.node_id` must be one of the internal ids.
- `input_to` — internal node id, or list of ids, the single input port wires to;
  `None` → no input port.
- `status_from` — `{node_id, port?}` for the status port; `None` → no status port.
- `env` — definition-level declared env vars (default `[]`).

**Pre-commit (input-only, deterministic):**
- Each internal node has `id` and `type`, else `NRError`; internal ids are unique,
  else `NRError`.
- `input_to` normalised to a list (`[]` / `[id]` / `list(ids)`).
- `def_id = _new_node_id({existing ids} | {internal ids})` — generated once, before
  `commit`, so a conflict-retry is deterministic; returned as `subflow_id`.
- Every `outputs[].source.node_id`, every `input_to` id, and `status_from.node_id`
  (if given) must be in the internal id set, else `NRError`.

**`rebuild(fl)` (pure):**
- Clash check against current flows: if any internal id or `def_id` already exists →
  `NRError` (re-checked here because flows can change on retry).
- Build the definition node:
  ```python
  def_node = {
      "id": def_id, "type": "subflow", "name": name,
      "in": ([{"x": 60, "y": 60, "wires": [{"id": t} for t in input_list]}] if input_list else []),
      "out": [{"x": 600, "y": 40 + 60 * i,
               "wires": [{"id": o["source"]["node_id"], "port": o["source"].get("port", 0)}]}
              for i, o in enumerate(outputs)],
      "outputLabels": [o.get("label") or f"out {i}" for i, o in enumerate(outputs)],
      "env": env or [], "color": "#DDAA99",
  }
  if status_from is not None:
      def_node["status"] = {"x": 600, "y": 40 + 60 * len(outputs),
                            "wires": [{"id": status_from["node_id"], "port": status_from.get("port", 0)}]}
  ```
- Deep-copy each internal node and set `z = def_id` (internal `wires` preserved as given).
- Return `copy.deepcopy(fl) + [def_node] + internals`.

After `commit`, set `result["subflow_id"] = def_id` and return.

### B. Tool — `server.py`

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

`subflow_edit` is already imported in `server.py`.

**Tool count 35 → 36.** Update `tests/test_server_smoke.py` (`expected` set + `len(names) == 36`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

Read + pure `rebuild`; `NRError` for: an internal node missing `id`/`type`, duplicate
internal ids, an internal/def id that already exists, or a port reference
(`outputs[].source`, `input_to`, `status_from`) not among the internal nodes.
Deploy gate snapshots a backup before any write and re-runs `rebuild` on conflict.

## Testing

`tests/test_subflow_edit.py` (extend):
- **happy path:** create a subflow (`name`, 2 internal nodes, `input_to` one node, 2
  outputs from a node's ports, status from a node) → definition node is
  `type:"subflow"` with correct `in`/`out`/`status`/`outputLabels`, internal nodes
  carry `z == subflow_id`, result has `subflow_id`, `deployed: true`.
- **bad output source** (references an id not in internal_nodes) → `NRError`.
- **internal id clash** (an internal id already exists in flows) → `NRError`.
- **propose mode** (`settings_propose`, `confirm=false`) → `deployed: false`,
  `subflow_id` present, `post_calls == []`.

`tests/test_server_smoke.py`: count 36; `nr_create_subflow` in `expected`.
`scripts/smoke_e2e.py`: add `"nr_create_subflow"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild/redeploy the Docker image to expose the tool live; local `.venv` for tests.

## Follow-ups

- **A second input port** / per-input labelling.
- **Validating internal node→node wires** (currently passed through, matching
  `nr_create_nodes`).
- **`category`/`color`** as tool params (currently a default color).
