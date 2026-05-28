# Single-tab flow export for the nrha MCP — Design

- **Date:** 2026-05-28
- **Status:** Approved (pending spec review)
- **Branch:** `feat/nr-export-flow`

## Problem

No tool emits an **import-ready** flow. `nr_get_flow` returns a *wrapper*
`{tab, nodes[], groups[], total_nodes}`, but Node-RED's **Import** (and a flows file)
expects a **flat array** `[tab, group…, node…]`. So an agent that wants to export /
back up / copy a single tab has to reshape `nr_get_flow`'s output by hand — observed
in the field: pulling a ~70K tab JSON and rebuilding `[tab, ...nodes]` with `jq`. That
works but is fiddly and easy to get subtly wrong. `nr_backup_flow` is not a substitute:
it snapshots **all** flows server-side to a backup file, not one importable tab.

## Goals

- `nr_export_flow(name_or_id)` returns, in **one call**, the import-ready flat array
  for a single tab, **verbatim** (full fidelity), ready to paste into Node-RED's Import
  or write to a `.json` file.

## Non-goals

- **Bundling subflow *definitions*** used by instances on the tab, or **global config
  nodes** (`z=""`, e.g. the HA `server` node) the tab references. Same scope as
  `nr_get_flow`'s tab slice and the manual workaround — a faithful export of the tab's
  own objects. For a complete cross-instance snapshot use `nr_backup_flow` (all flows).
  This is a documented limitation, not a bug.
- A `fields` filter — an export must be complete; no trimming.
- Writing the file server-side — the tool returns the array; the caller writes it.

## Design

### A. Extract `_resolve_tab` (DRY)

`flows.get_flow` inlines tab resolution (match by `id`, else unique `label` substring
match; ambiguous or none → `NRNotFoundError`). Extract that block into a helper and
have both `get_flow` and the new `export_flow` call it. Behavior is unchanged — the
existing `get_flow` tests guard it.

```python
def _resolve_tab(all_flows: list[dict], name_or_id: str) -> dict:
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

`get_flow` is refactored to `tab = _resolve_tab(flows, name_or_id)` (dropping its inline
resolution), keeping everything else identical.

### B. Module function — `flows.export_flow(client, name_or_id) -> dict`

```python
async def export_flow(client: NRClient, name_or_id: str) -> dict:
    all_flows, _ = await client.get_flows()
    tab = _resolve_tab(all_flows, name_or_id)
    tab_id = tab["id"]
    nodes = [f for f in all_flows if f.get("z") == tab_id and f.get("type") != "group"]
    groups = [f for f in all_flows if f.get("z") == tab_id and f.get("type") == "group"]
    return {
        "flow": [tab, *groups, *nodes],   # import-ready flat array
        "tab_id": tab_id,
        "node_count": len(nodes),
        "group_count": len(groups),
    }
```

Nodes/groups are the raw flow dicts (verbatim, full fidelity). `flow` is the
import-ready array. **Return-shape decision:** a dict wrapper (not a bare array) keeps
success/error shapes both dicts — consistent with every other tool (`_wrap` returns
`{"error": …}` on failure) — and carries handy `tab_id`/counts. The caller reads
`result["flow"]` to import.

### C. Tool — `server.py` (read-only group, next to `nr_get_flow`)

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

**Tool count goes 38 → 39.** Update the three sites: `tests/test_server_smoke.py`
(`expected` set + `len(names) == 39`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

Not-found / ambiguous tab → `NRNotFoundError`, surfaced as structured error JSON by
`_wrap` (same as `get_flow`). No creds guard needed — this is a Node-RED Admin API read
like the other `nr_*` tools.

## Testing

`tests/test_flows.py` (extend; uses the `fake_client` fixture whose `SAMPLE_FLOWS` has
tab `tab1` "Heating" with nodes `n1`/`n2`/`n3`, group `g1`, and a global config node
`srv1` with `z=""`):

- `export_flow(fake_client, "Heating")` → `flow[0]["id"] == "tab1"`; the set of ids in
  `flow` equals `{"tab1", "g1", "n1", "n2", "n3"}`; `node_count == 3`;
  `group_count == 1`; `len(flow) == 5`.
- **Config-node scope:** `"srv1"` is NOT in the exported ids (global config node not
  bundled — documents the limitation).
- **Verbatim fidelity:** the exported `n3` keeps its arbitrary fields (`domain`,
  `service`, `entityId`).
- Resolution: by id `"tab1"` works; unknown name → `NRNotFoundError`.
- `_resolve_tab` refactor: existing `get_flow` tests still pass (regression guard).

`tests/test_server_smoke.py`: count 39; `nr_export_flow` registered.
`scripts/smoke_e2e.py`: add `"nr_export_flow"` to `EXPECTED_TOOLS`.

## Deployment

Rebuild and redeploy the Docker image so the running MCP exposes the tool live (the
session-attached build is older).

## Follow-ups

- An optional self-contained mode (`include_config_nodes=true` and/or bundling subflow
  definitions referenced by instances) for cross-instance portability. YAGNI now — the
  tab-scoped export matches the current need and the manual workaround it replaces.
