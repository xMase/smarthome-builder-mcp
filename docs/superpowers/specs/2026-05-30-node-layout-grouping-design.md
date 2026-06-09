# Node alignment & grouping for authored flows — Design

- **Date:** 2026-05-30
- **Status:** Part A implemented (working tree, uncommitted); Part B proposed
- **Branch:** `main` (Part A); Part B TBD

## Problem

When an AI agent authors or edits Node-RED flows through this MCP — especially flows
using Home Assistant nodes — the resulting layout is messy: nodes overlap, aren't
aligned to a grid, and related nodes aren't grouped. Two distinct pain points:

1. **Repetition** — the user has to re-state layout/grouping preferences in *every*
   session/prompt. There is no place where "lay out tidily and group" is remembered.
2. **Imprecision** — even when told, the model computes `x`/`y` "by eye". On non-trivial
   tabs it overlaps nodes, breaks the grid, or forgets the group's dual reference
   (`group.nodes[]` ↔ member `g`).

Today the server does **no layout at all**: `edit.create_nodes` (`edit.py:83`) appends
nodes with whatever `x`/`y` the caller supplies. Node-RED **groups are already
supported** as plain `type:"group"` nodes — `create_nodes` can create them and
`delete_nodes` cleans their refs (`edit.py:130`) — but nothing *guides* their use.

These are different problems with different fixes, hence the two-part design. Decision
(user, 2026-05-30): **ship A first, see how it performs, add B only if A proves
insufficient on larger tabs.**

## Background: the relevant Node-RED schema

A group is a node:

```json
{ "id": "grp1", "type": "group", "z": "<tab>", "name": "Living-room lights",
  "style": {"label": true}, "nodes": ["n1","n2","n3"], "x":80,"y":40,"w":420,"h":180 }
```

Each member node must *also* point back: `"g": "grp1"`. Both references must agree —
`tests/test_validate.py` already flags `group_member_missing` when they don't.

`x`/`y` are **mutable** — `IMMUTABLE = {"id","type","z"}` (`edit.py:12`) — so repositioning
existing nodes is just a field patch via `nr_safe_deploy_many`.

## Part A — bake layout & grouping guidance into the server (DONE)

The lever that kills the *repetition* problem: the server's `instructions` string is
injected into every client session automatically (it already carries the HA-authoring
guidance). The model also reads tool docstrings at call time.

**Implemented** in `server.py`:

- **`HA_AUTHORING_GUIDANCE`** (`server.py:16`) — appended layout + grouping rules:
  - Layout: lay nodes left→right by data flow; columns ~180px apart on `x`, rows ~80px
    apart on `y`; snap every `x`/`y` to a 20px grid; never overlap.
  - Grouping: wrap logically related nodes (one automation, or all nodes for one entity)
    in a `type:"group"` node with descriptive `name`, `style:{label:true}`, member ids in
    `nodes`, **and** `g=<groupId>` on every member (both refs must agree); give the group
    `x/y/w/h` bounding its members.
- **`nr_create_nodes` docstring** (`server.py:~198`) — same rules, condensed, so they're
  in front of the model exactly when it builds nodes.

No tool added/removed → tool count unchanged (39); no test asserts on the guidance
string, so the change is contained. `pytest -q` → 184 passed.

**Limitation (by design):** A only *steers* the LLM. Coordinates are still model-computed,
so precision on large/complex tabs is not guaranteed. That's what B is for.

### Tidying an existing messy tab with A only

No dedicated tool; the agent chains existing ones:

1. `nr_get_flow(name_or_id)` → nodes, current coords, `wires`.
2. Compute a tidy grid layout (topological left→right).
3. `nr_safe_deploy_many` with `[{node_id, fields:{x,y}}, …]` — preview, then `confirm=true`.
4. (Optional) create group nodes (`nr_create_nodes`) + set `g` on members
   (`nr_safe_deploy_many`).

A backup is written before any real change (deploy gate), so `nr_restore_backup` undoes
a bad layout.

## Part B — deterministic server-side layout (PROPOSED, not yet built)

Removes the *imprecision* problem by taking coordinate computation away from the LLM.

### New tool — `nr_auto_layout(tab, group_by=None, confirm=False)`

A mutating tool (confirm-gated, backup-first) that, given a tab, recomputes every node's
`x`/`y` deterministically and optionally builds/refreshes groups. Lives in `edit.py` on
the deploy gate like every other mutator (pure `rebuild(flows)` closure handed to
`commit`), declared in `server.py`.

**Algorithm sketch:**

- Resolve the tab (reuse `flows._resolve_tab` if extracted, else match by id/label).
- Build a DAG from `wires` over the tab's nodes.
- Assign a **column** per node = longest-path depth from any source (topological rank);
  nodes with no inputs start at column 0.
- Within a column, stack nodes top→bottom in a stable order (by existing `y`, then id).
- Coordinates: `x = X0 + col * COL_W` (≈180), `y = Y0 + row * ROW_H` (≈80), snapped to a
  20px grid. Constants match the Part-A guidance so manual and automatic layout agree.
- Cycles (Node-RED allows feedback wires): break by ignoring back-edges when ranking;
  log/return a note that a cycle was detected.

**Grouping (`group_by`):** optional. Candidate strategies (pick during B's brainstorm):
- `none` — reposition only.
- `entity` — group nodes sharing a resolved HA `entity_id`.
- explicit — caller supplies `[{name, node_ids:[…]}]`.
For each group, emit/refresh the `type:"group"` node with a bounding `x/y/w/h` and set
`g` on members (keeping both refs consistent).

**Return:** the deploy-gate preview/diff (moved nodes, created groups), same two-call
propose→confirm UX as the other mutators.

### Open design questions (resolve in B's brainstorm before coding)

- Group automatically, or only on explicit `group_by`? (Lean: explicit/opt-in.)
- Column metric: longest-path vs. simple BFS depth — behavior on diamonds/joins.
- How to lay out nodes not connected to anything (orphans) — parking lane?
- Preserve manually-set positions if "good enough", or always recompute?
- Subflow instances and link nodes — treat as ordinary nodes for ranking?

### Tool-count bookkeeping (when B lands)

`nr_auto_layout` is a new tool → **39 → 40**. Update the three sites per CLAUDE.md:
`@mcp.tool()` in `server.py`, `tests/test_server_smoke.py` (`expected` set + `len(names)`),
and `EXPECTED_TOOLS` in `scripts/smoke_e2e.py`. `tests/test_tool_count_sync.py` guards it.

## Testing

- **Part A:** no behavior change beyond strings; `pytest -q` green (184). No new tests
  needed; deliberately no assertion pinning the guidance text (keeps it editable).
- **Part B (future):** unit tests on the layout function with `FakeNRClient` — a linear
  chain lands on one row with increasing `x`; a fan-out/fan-in lands on correct columns;
  grid-snapping holds; a cycle doesn't hang; grouping sets both `nodes[]` and member `g`
  consistently and passes `nr_validate_flow` (no `group_member_missing`).

## Deployment

Part A is a code change to the in-process guidance/docstrings — rebuild & redeploy the
Docker image so the running MCP serves the updated `instructions`. Part B, when built,
ships the same way plus the tool-count updates.

## Follow-ups

- Decide A-vs-B sufficiency after real use on a messy tab.
- If B is built, consider a read-only `nr_preview_layout` that returns proposed coords
  without deploying (cheap dry-run independent of the confirm gate).
