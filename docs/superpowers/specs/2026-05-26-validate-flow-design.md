# Flow Validation for the nrha MCP — Design

- **Date:** 2026-05-26
- **Status:** Approved (pending spec review)
- **Branch:** `feat/validate-flow`

## Problem

The MCP's existing validation is scattered across the mutating tools in `edit.py`
(each `create_nodes`/`safe_deploy`/`delete_nodes` checks a few things at deploy
time). There is no holistic, proactive check an agent can run **before** writing
to catch the structural errors that otherwise only surface at runtime: a wire
pointing at a node that doesn't exist, a duplicate id, a `z` pointing at a
non-existent tab, an unregistered node type, a group listing a missing member.

## Goals

- A `nr_validate_flow` tool that checks flow structure and reports errors and
  warnings, usable **pre-deploy** (validate nodes about to be created, merged with
  current flows) **and** for **audit** (validate the current deployed flows).
- High-precision errors (no false positives) so the result is trustworthy;
  heuristic findings are warnings.

## Non-goals

- **Per-node-type field validation** (required fields per type, catching
  deprecated fields like `state_type`). That needs the node-schema catalog, which
  is the deferred milestone C (`2026-05-26-ha-aware-authoring-catalog-analysis.md`).
  This feature is **structural and catalog-free**.
- **Deploy-gate integration.** v1 is a standalone tool. Auto-running validation
  inside `deploy.commit`'s `propose` preview is a sensible later step but riskier
  (could block legitimate deploys); deferred.
- Validating a full flow JSON as a **replacement** (import). `nodes` are additions
  (the `create_nodes` semantic); replacement/import is feature #6's concern.

## Design

### A. Pure validation engine — `src/nrha_mcp/validate.py`

```python
def validate_flows(flows: list[dict], *, focus_ids: set[str] | None = None,
                   registered_types: set[str] | None = None) -> dict:
    ...
```

- `flows`: the full node list to validate (current, or current ∪ proposed).
- `focus_ids`: ids of the "new" nodes, echoed on findings that involve them (so
  the caller can tell which findings the proposed nodes caused). Does not change
  which checks run.
- `registered_types`: installed node types (from `get_nodes()`); when `None`, the
  type check is skipped (keeps the engine pure/testable without a client).

Pure (no I/O); takes and returns plain dicts.

**Errors** (high-precision, unambiguous breakages):

| code | condition |
|---|---|
| `missing_id` | a node has no `id` |
| `missing_type` | a node has no `type` |
| `duplicate_id` | two nodes share an `id` |
| `wire_to_missing` | a node's `wires` references an id not in `flows` |
| `bad_z` | a node's `z` is non-empty and is not the id of an existing `tab`/`subflow` |
| `group_member_missing` | a `group` node's `nodes` lists an id not in `flows` |

**Warnings** (heuristic — may be intentional):

| code | condition |
|---|---|
| `unregistered_type` | node `type` not in `registered_types` (skips types starting with `subflow:`); only when `registered_types` is provided |
| `server_ref_missing` | a node has a non-empty `server` field whose value is not an existing node id (HA-aware: HA nodes reference a `server` config node) |

**Return shape:**

```json
{
  "valid": true,
  "errors": [{"code": "wire_to_missing", "node_id": "n2", "message": "..."}],
  "warnings": [{"code": "unregistered_type", "node_id": "n5", "message": "..."}],
  "checked": 12
}
```

`valid` is `true` when `errors` is empty (warnings do not affect `valid`). Each
finding includes `code`, the offending `node_id`, and a human-readable `message`.
Findings whose `node_id` is in `focus_ids` also carry `"focus": true`.

### B. Orchestration — `validate.py`

```python
async def validate_flow(client, *, nodes: list[dict] | None = None) -> dict:
    flows, _ = await client.get_flows()
    if nodes:
        candidate = flows + nodes              # additions (create_nodes semantic)
        focus = {n["id"] for n in nodes if n.get("id")}
    else:
        candidate, focus = flows, None
    registered = _registered_types(await client.get_nodes())  # best-effort, may be empty
    return validate_flows(candidate, focus_ids=focus, registered_types=registered or None)
```

`_registered_types(nodes_payload)` derives the set of installed node `types` from
the `/nodes` payload (union of each set's `types`). Best-effort: if the shape is
unexpected it returns an empty set and the type check is skipped.

### C. Tool — `server.py`

```python
@mcp.tool()
async def nr_validate_flow(nodes: list[dict] | None = None) -> str:
    """Validate flow structure and report errors/warnings WITHOUT deploying.

    With `nodes`: validates (current flows + those nodes) — run it before
    nr_create_nodes/nr_safe_deploy to catch problems early. Without `nodes`:
    audits the current deployed flows. Checks references (wires, z/tab, group
    members), duplicate ids, and node types; it does NOT check per-type fields."""
    return await _wrap(lambda: validate.validate_flow(client, nodes=nodes))
```

Add `validate` to the `from . import ...` line in `server.py`.

**Tool count goes 24 → 25.** Update both hardcoded sites: `tests/test_server_smoke.py`
(`expected` set + `len(names) == 25`) and `scripts/smoke_e2e.py` (`EXPECTED_TOOLS`).

## Error handling

`validate_flow` performs only reads (`get_flows`, `get_nodes`); a Node-RED error
surfaces through the usual `NRError` → `_wrap` mapping. The engine tolerates a
malformed `wires` (a non-list output entry is skipped, avoiding char-by-char
false positives on agent-authored nodes). It assumes each flow entry is a dict
(true for Admin-API flows and consistent with `flows.py`/`edit.py`); a non-dict
entry surfaces as an `_wrap` error rather than crashing the server.

## Testing

`tests/test_validate.py` (new) — pure-engine tests with plain dicts:
- valid flow → `valid: true`, empty errors.
- one test per error code: `duplicate_id`, `wire_to_missing`, `bad_z`,
  `group_member_missing`, `missing_type`.
- `unregistered_type` produces a warning (and `subflow:*` is exempt).
- `server_ref_missing` produces a warning; a valid `server` ref does not.
- `focus_ids` marks findings on those nodes with `"focus": true`.
- orchestration test with `FakeNRClient`: `nodes` are merged with current and a
  wire from a new node to a missing id is reported; no `nodes` audits current.

`tests/conftest.py`: `FakeNRClient.get_nodes` already returns `self.nodes` (`[]`
by default); tests set `fake_client.nodes` to exercise `registered_types`.

`tests/test_server_smoke.py`: count is 25; `nr_validate_flow` registered.

`scripts/smoke_e2e.py`: add `"nr_validate_flow"` to `EXPECTED_TOOLS`.

## Deployment

Same as prior features: rebuild and redeploy the Docker image to expose the tool
live. Local `.venv` is for tests.

## Follow-ups

- **Deploy-gate integration:** optionally run `validate_flows` inside
  `deploy.commit` and surface warnings in the `propose` preview.
- **Catalog-based checks:** once milestone C lands, add per-type field validation
  (required fields, deprecated fields) as additional findings.
