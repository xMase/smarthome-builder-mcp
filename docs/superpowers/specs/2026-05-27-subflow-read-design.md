# Subflow-Aware Read for the nrha MCP — Design

- **Date:** 2026-05-27
- **Status:** Approved (pending spec review)
- **Branch:** `feat/subflow-read`

## Problem

Subflows are second-class in the read tools because none of them treat a subflow
the way they treat a tab. Three blind spots in `flows.py`, all stemming from the
fact that a subflow's structure lives in `in`/`out`/`status` arrays on the
**definition** node (not in `wires`):

1. `get_flow` filters `type == "tab"` only → on a subflow id it raises
   "Tab not found".
2. `get_node_config` derives `_downstream`/`_upstream` from a node's `wires`. For a
   subflow **definition** node those are empty (the wiring is in `in`/`out`/`status`),
   and an internal node that feeds an output port shows no target at all (the
   connection is recorded on the definition's `out`, not on the node) — the
   "empty wires / false dead-end" trap.
3. `get_flow_summary` and `search_nodes` ignore subflow definitions entirely (they
   have no `z`).

The data is already present in `client.get_flows()`; what's missing is a resolver
that interprets `in`/`out`/`status`. This bit a real investigation: reconstructing
one subflow (`seasonSwitch` → reads `select.season`, routes to 6 labelled outputs
via a nested `strSeasonSwitch`) took six manual `nr_get_node_config` calls and a
wrong-tool detour through `nr_get_flow`.

## Goals

- A `nr_get_subflow` tool that resolves a subflow — by **name**, **definition id**,
  or **instance id** — into one payload: definition meta, ports resolved to their
  internal endpoints (with `outputLabels`), internal nodes annotated with their real
  targets, pointers to nested subflows, and the list of instances across the project
  with which outputs each wires.
- Make the existing read tools subflow-aware (`get_node_config`, `get_flow_summary`,
  `search_nodes`) so an agent that reaches for the "wrong" tool still gets correct,
  subflow-aware answers.
- **Single source of truth:** all subflow interpretation lives in one pure helper,
  `_resolve_subflow`, reused by every consumer. This is also the foundation the
  later subflow-**write** work (sub-project B) will build on.

## Non-goals

- **Writing/creating/editing subflows** (definitions, ports, instances). That is
  sub-project B and touches the deploy gate; this feature is read-only.
- **Recursive expansion of nested subflows.** A nested subflow instance is reported
  as a node carrying its `def_id`/`def_name` (a pointer); the agent calls
  `nr_get_subflow` again to drill down. An optional `expand`/`max_depth` is a trivial
  later add if a real need appears — deferred (YAGNI).
- **Per-instance `env`.** We surface the definition's declared `env` (in the
  resolved subflow). We do NOT surface each instance's `env` overrides, nor compute
  the effective merged environment — that is a follow-up.

## Design

### A. Pure resolver — `src/nrha_mcp/flows.py`

```python
def _resolve_subflow(flows: list[dict], def_id: str) -> dict:
    ...
```

Pure (no I/O), takes the full flow list and a subflow definition id, returns the
resolved structure. Steps:

- **definition**: the node with `id == def_id` and `type == "subflow"`.
- **inputs**: from `def["in"]` — each `{ "index": i, "wires": [{"node_id": ...}] }`.
- **outputs**: zip `def["out"]` with `def.get("outputLabels", [])` —
  `{ "index": i, "label": <label or "out i">, "sources": [{"node_id", "port"}] }`.
  `sources` is a **list** because a port may be fed by zero or several internal nodes.
- **status**: from `def.get("status")` if present → `{ "sources": [...] }`, else `null`.
- **env**: `def.get("env", [])` (declared defaults).
- **internal nodes**: `[f for f in flows if f.get("z") == def_id]`, each annotated with
  - `_targets`: its `wires` flattened (intra-subflow downstream);
  - `_feeds_outputs`: indices of output ports whose `sources` reference this node;
  - `_feeds_status`: `true` if it is the status source;
  - `_nested_subflow`: `{def_id, def_name}` if its `type` starts with `"subflow:"`.
- **nested_subflows**: pointers built from those internal nodes whose type is
  `subflow:<id>` → `{instance_node_id, def_id, def_name}`.
- **instances**: every node in `flows` with `type == f"subflow:{def_id}"` →
  `{node_id, flow_id, flow_label, wired_outputs}` where `wired_outputs` are the
  indices of that instance's non-empty `wires` entries.

### B. Orchestration — `flows.py`

```python
async def get_subflow(client: NRClient, name_or_id: str) -> dict:
    flows, _ = await client.get_flows()
    def_id = _find_subflow_def_id(flows, name_or_id)   # raises NRNotFoundError
    return _resolve_subflow(flows, def_id)
```

`_find_subflow_def_id` resolves `name_or_id`, in order:

1. a node with that id and `type == "subflow"` → that id (direct definition);
2. a node with that id whose `type` is `subflow:<defid>` → `<defid>` (an **instance**,
   resolved to its definition);
3. a unique `subflow` definition whose `name` contains `name_or_id`
   (case-insensitive, mirroring `get_flow`'s name match) → its id; ambiguous matches
   raise `NRNotFoundError` listing the candidates;
4. otherwise `NRNotFoundError(f"Subflow not found: '{name_or_id}'")`.

### C. Tool — `server.py`

```python
@mcp.tool()
async def nr_get_subflow(name_or_id: str) -> str:
    """Resolve a subflow by name, definition id, or instance id. Returns the
    definition, its inputs/outputs/status resolved to the internal nodes that feed
    each port (with output labels), the internal nodes (annotated with where they
    actually wire — including subflow output ports), pointers to nested subflows,
    and every instance of the subflow with which outputs it wires. Use this instead
    of nr_get_flow for subflows (nr_get_flow handles tabs only)."""
    return await _wrap(lambda: flows.get_subflow(client, name_or_id))
```

`flows` is already imported in `server.py`.

**Tool count goes 30 → 31.** Update both hardcoded sites (per the CLAUDE.md gotcha):
`tests/test_server_smoke.py` (`expected` set **and** `len(names) == 31`) and
`scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

### D. Subflow-awareness in the existing read tools (reuse the resolver)

- **`get_node_config`** — after building the current result, add subflow context:
  - if the node `type == "subflow"` (a definition): fill `_downstream` from the
    resolved `out`/`status` source node ids, and attach a compact `_ports`
    (`{inputs, outputs:[{index,label}], has_status}`);
  - if the node's `z` is a subflow definition id (an internal node): attach
    `_feeds_outputs` / `_feeds_status` for that parent definition;
  - if the node `type` is `subflow:<defid>` (an instance): attach
    `_subflow_def` `{id, name}`.
- **`get_flow_summary`** — add a `subflows` section:
  `[{id, name, node_count, instance_count}]` (definitions with their internal node
  count and how many times instantiated).
- **`search_nodes`** — include subflow **definition** nodes in the searchable set
  (currently excluded for lacking `z`; match on `name`/`info`), and set `flow_label`
  for nodes living inside a subflow to that subflow's name (today it is empty because
  only tab labels are mapped).

## Output shape

`nr_get_subflow` on `seasonSwitch` (real data), abridged:

```jsonc
{
  "subflow": { "id": "1228514ee161a4fd", "name": "seasonSwitch", "color": "#DDAA99", "info": "" },
  "inputs":  [ { "index": 0, "wires": [ { "node_id": "614c0b5a2ea265cc" } ] } ],
  "outputs": [
    { "index": 0, "label": "inverno",   "sources": [ { "node_id": "fe040a12647c5240", "port": 0 } ] },
    { "index": 2, "label": "estate",    "sources": [ { "node_id": "fe040a12647c5240", "port": 2 } ] },
    { "index": 3, "label": "altro",     "sources": [ { "node_id": "fe040a12647c5240", "port": 3 } ] }
    /* …inverno, manuale, estate, altro, primavera, autunno… */
  ],
  "status": { "sources": [ { "node_id": "1bf6dcc1d96aa713", "port": 0 } ] },
  "env": [],
  "nodes": [
    { "id": "614c0b5a2ea265cc", "type": "api-current-state", "name": "getSeason",
      "_targets": [ "fe040a12647c5240", "1bf6dcc1d96aa713" ] },
    { "id": "fe040a12647c5240", "type": "subflow:40de2cb85bb4958d", "name": "",
      "_feeds_outputs": [0,1,2,3,4,5],
      "_nested_subflow": { "def_id": "40de2cb85bb4958d", "def_name": "strSeasonSwitch" } },
    { "id": "1bf6dcc1d96aa713", "type": "change", "name": "", "_feeds_status": true }
  ],
  "nested_subflows": [ { "instance_node_id": "fe040a12647c5240",
                         "def_id": "40de2cb85bb4958d", "def_name": "strSeasonSwitch" } ],
  "instances": [
    { "node_id": "5e0a501100000e02", "flow_id": "5e0a501100000a01",
      "flow_label": "Esposizione Solare", "wired_outputs": [2] }
    /* …e11, e20, f02… */
  ],
  "total_nodes": 3
}
```

## Error handling

Read-only (`get_flows`); Node-RED errors surface through the usual `NRError` →
`_wrap` mapping. `_find_subflow_def_id` raises `NRNotFoundError` for no/ambiguous
match. The resolver tolerates: `outputLabels` shorter than `out` (default
`"out i"`); a missing `status`; ports with zero or multiple sources (`sources` is a
list); a subflow with no internal nodes or no outputs (empty lists, no crash). It
assumes each flow entry is a dict (consistent with `flows.py`/`edit.py`).

## Testing

`tests/test_flows.py` (extend) — add a subflow fixture to the flow list: a `subflow`
definition with `in`/`out`/`status`/`outputLabels`, internal nodes including one
**nested** subflow instance, plus one **external** instance on a tab. Cases:

- `get_subflow` resolves by definition id, by name, and by instance id.
- output port → label mapping and `sources` are correct.
- internal node annotations: `_targets`, `_feeds_outputs`, `_feeds_status`.
- `nested_subflows` pointer present and **not** expanded.
- `instances` listed with correct `wired_outputs`.
- not-found id and ambiguous-name both raise `NRNotFoundError`.
- `get_node_config` on the definition → non-empty `_downstream` + `_ports`; on an
  internal node feeding a port → `_feeds_outputs` set; on an instance → `_subflow_def`.
- `get_flow_summary` includes the `subflows` section with right counts.
- `search_nodes` finds the subflow definition by name and labels internal nodes with
  the subflow's name.

`tests/conftest.py`: `FakeNRClient.get_flows` already returns `self.flows`; tests set
`fake_client.flows` to the fixture — no new fake methods needed.

`tests/test_server_smoke.py`: count is 31; `nr_get_subflow` in `expected`.

`scripts/smoke_e2e.py`: add `"nr_get_subflow"` to `EXPECTED_TOOLS`.

## Deployment

Same as prior features: rebuild and redeploy the Docker image to expose the tool
live; local `.venv` is for tests.

## Follow-ups

- **Recursive expansion:** optional `expand`/`max_depth` on `nr_get_subflow` to
  inline nested subflows when wanted.
- **Effective env:** compute the merged environment for an instance (definition
  defaults ∪ instance overrides).
- **Sub-project B (subflow write):** create/edit subflow definitions, ports, and
  instances through the deploy gate, built on `_resolve_subflow`.
```
