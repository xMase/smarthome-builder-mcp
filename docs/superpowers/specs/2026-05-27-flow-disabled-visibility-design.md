# Flow / node "disabled" visibility in nrha reads — Design

- **Date:** 2026-05-27
- **Status:** Approved (pending spec review)
- **Branch:** `feat/flow-disabled-visibility`

## Problem

A disabled tab (`{"type":"tab", "disabled":true}`) silently makes **all** its nodes
non-executing; an individually disabled node (`{"d":true}`) does the same for that one
node — even on an enabled tab. Node-RED's runtime simply doesn't load disabled things,
so an inject never fires, a `flow.set` never runs, an HA node never connects.

None of nrha's read tools surface this. `nr_get_flow_summary` lists every tab
identically; `nr_search_nodes` returns `flow_label` but not the tab's state;
`nr_get_node_config` returns `_flow_label` (and the raw `d`, but cryptically). The only
signal today is `nr_inject` relaying the runtime's bare *"Inject node not found"* —
which reads like a missing node, not a disabled flow.

This blind spot cost a full debugging session: an agent built a one-shot on a tab it
didn't realise was disabled, then chased onceDelay, observability, tokens, and a
restart — all downstream of the one invisible fact. This feature makes "can this node
actually run?" visible in the read tools.

## Goals

- `nr_get_flow_summary`: each tab reports its `disabled` state.
- `nr_search_nodes` and `nr_get_node_config`: report both the node's own disabled state
  and whether its tab is disabled.
- `nr_inject`: when the target inject node exists but cannot run because it (or its tab)
  is disabled, return a clear, actionable error instead of the runtime's bare
  "not found".

## Non-goals

- **No enable/disable WRITE tool.** Toggling `disabled`/`d` is already possible via
  `nr_safe_deploy` (`fields={"disabled":false}` / `{"d":false}`); this feature is
  read-side visibility only.
- **No new MCP tool.** All changes enrich existing tools; **tool count stays 37**
  (no change to `tests/test_server_smoke.py` count or `scripts/smoke_e2e.py`).
- **No subflow-internal "effective runnability".** Whether a subflow's *internal* node
  runs depends on whether any *instance* sits on an enabled tab — too deep to compute
  per-node. Internal nodes (whose `z` is a subflow def) report `flow_disabled = false`;
  this is documented, not computed.

## Design

All read changes live in `src/nrha_mcp/flows.py`; the inject change in
`src/nrha_mcp/modules.py`. The `disabled`/`d` fields are usually **absent** on
enabled tabs/nodes (Node-RED only writes them when true), so every read uses
`bool(x.get(...))` / `.get(..., False)` — never bare indexing.

### A. `get_flow_summary` — `flows.py:6`

Each tab entry (built at `flows.py:13`) gains a `disabled` key:

```python
tab_map[tab["id"]] = {"id": tab["id"], "label": tab.get("label", ""),
                      "disabled": bool(tab.get("disabled")),
                      "node_count": 0, "groups": []}
```

### B. `search_nodes` — `flows.py:67`

Add a tab-disabled lookup beside the existing `tabs` map (`flows.py:70`):

```python
tab_disabled = {f["id"]: bool(f.get("disabled")) for f in flows if f.get("type") == "tab"}
```

Each result dict (built at `flows.py:96-100`) gains two keys, using this function's
**existing un-prefixed convention** (`flow_id`/`flow_label`):

```python
"flow_id": node.get("z", ""), "flow_label": _label(node.get("z", "")),
"flow_disabled": tab_disabled.get(node.get("z", ""), False),
"node_disabled": bool(node.get("d")),
```

### C. `get_node_config` — `flows.py:120`

Add the same tab-disabled lookup beside the `tabs` map (`flows.py:125`):

```python
tab_disabled = {f["id"]: bool(f.get("disabled")) for f in flows if f.get("type") == "tab"}
```

After `_flow_label` is set (`flows.py:142`), add two keys, using this function's
**existing underscore convention** (`_flow_label`/`_group_label`):

```python
result["_flow_disabled"] = tab_disabled.get(node.get("z", ""), False)
result["_disabled"] = bool(node.get("d"))
```

**Note on the cross-function naming asymmetry:** `search_nodes` already uses
`flow_label`/`flow_id` (no underscore) while `get_node_config` uses `_flow_label`
(underscore). The new fields follow each function's *own* existing convention
(`flow_disabled`/`node_disabled` in search; `_flow_disabled`/`_disabled` in node
config) rather than introducing a third style. Semantics mirror the raw Node-RED
fields: `true` = off (tab `disabled`, node `d`).

### D. `inject_node` — `modules.py:33`

`inject_node` already fetches flows and has the node in hand, so the pre-checks are
free (no extra fetch). Insert between the type check (`modules.py:39`) and
`await client.inject(...)` (`modules.py:40`):

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

`NRError` and `NRNotFoundError` are already imported in `modules.py`. This short-circuits
*before* the runtime POST for the disabled cases (which previously surfaced as the
runtime's bare 404). Enabled nodes on enabled tabs are unaffected.

## Error handling

Read enrichments cannot fail (additive, guarded with `.get`). The only behavior change
is `inject_node` raising a clear `NRError` for a disabled node / disabled tab before the
runtime call. Everything still propagates through `_wrap` to structured JSON.

## Testing

**`tests/test_flows.py`** — extend the fixtures with (a) a **disabled tab**
(`{"type":"tab","id":...,"disabled":true}`) carrying a node, and (b) an
**individually disabled node** (`"d":true`) on an enabled tab. Assert:
- `get_flow_summary`: the disabled tab's entry has `disabled: true`; a normal tab has
  `disabled: false` (key always present).
- `search_nodes`: a node on the disabled tab → `flow_disabled: true`; the `d:true` node
  → `node_disabled: true`; a normal node → both `false`.
- `get_node_config`: node on disabled tab → `_flow_disabled: true`; `d:true` node →
  `_disabled: true`; subflow-internal node (`z` = subflow def) → `_flow_disabled:
  false`.

**`tests/test_modules.py`** — extend the inject fixtures:
- inject node on a disabled tab → `inject_node` raises `NRError` mentioning "disabled
  flow"; `client.inject` is **not** called.
- inject node with `d:true` → raises `NRError` mentioning "disabled"; not called.
- existing `test_inject_ok` (enabled) still passes unchanged.

**Regression watch:** any existing assertion that compares a `get_flow_summary` /
`search_nodes` / `get_node_config` result against an **exact** dict must add the new
keys. Subset/`["key"]` assertions are unaffected. The implementer must run the full
suite and fix any exact-dict comparisons.

## Deployment

No new tool, no count change. Rebuild/redeploy the Docker image to expose the enriched
output live; local `.venv` for tests. (This is one of the improvements that would have
made the disabled-tab debugging session a single read.)

## Follow-ups

- **Disabled-group visibility** (Node-RED groups can't be disabled today, so N/A).
- **Effective runnability for subflow internals** (cross-referencing instance tabs) —
  deferred as over-engineering until a real need appears.
