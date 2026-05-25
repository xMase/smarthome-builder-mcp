# node-red-homeassistant-mcp — Design Spec

- **Date:** 2026-05-25
- **Status:** Approved (brainstorming) — pending written-spec review
- **Author:** xmase + Claude

---

## 1. Overview & Goal

A standalone, MIT-licensed **MCP (Model Context Protocol) server for Node-RED**, specialized
for **Home Assistant** and **safe by default**. It lets an AI assistant (Claude, or any MCP
client) read, author, and deploy Node-RED flows through Node-RED's Admin API — without
silently corrupting a working installation.

The project is a **detached/hard fork** of [`nr-mcp`](https://github.com/Texan-NXTassist/nr-mcp)
(MIT): it reuses nr-mcp's proven *safe-deploy* logic as a starting point but is published as an
**independent, renamed project** with its own identity, README and roadmap (see §4).

**One-line description (for search / AI discovery):**
> MCP server for Node-RED with Home Assistant awareness and safe, backup-first deploy.

## 2. Audience & primary use case

- **Audience:** the broad **Home Assistant + Node-RED** community. The tool is *general purpose*,
  not wired to any single home's logic.
- **Driving use case / real-world test bed:** an existing **complex, production Node-RED
  installation** with many interdependent Home Assistant flows. Such a setup is the acceptance
  test for "does it manage real, non-trivial flows without breaking them" — domain logic always
  stays as *user content* (flows), never baked into the tool.

## 3. Scope

**In scope (v1):**
- Read/inspect flows and nodes (summary, search, get config, function code, debug output).
- Author changes: create/update/delete nodes; edit function code.
- **Safe deploy** with optimistic locking + mandatory pre-deploy backup.
- **Propose → confirm → deploy** workflow (configurable; default requires confirmation).
- **Home Assistant awareness:** recognize `node-red-contrib-home-assistant-websocket` nodes,
  surface their configuration meaningfully, and (optionally) validate HA service/entity
  references against a live HA instance.
- Module management (list/install Node-RED modules).
- Inject-node triggering for testing.
- Three deployment faces from one codebase: **stdio**, **Docker (HTTP/SSE)**, **HA add-on**.

**Out of scope (v1) — YAGNI:**
- Hard-coding any specific home-automation domain logic as built-in features.
- A GUI/dashboard of its own (Node-RED already has one).
- Managing non-Node-RED parts of HA (entities, dashboards, supervisor) — that's other tools'
  job (`ha-mcp`, official `/api/mcp`).
- Multi-instance / multi-tenant orchestration.
- Editing files inside the Node-RED add-on container.

## 4. Origin & licensing

- Base: `nr-mcp` (MIT). We keep the original copyright notice in `LICENSE` and add our own.
- We also incorporate the (open) **nr-mcp PR #3** — `nr_get_global_context`,
  `nr_safe_deploy_many`, `nr_add_wire`, and the `fields` param on `nr_get_flow` — cherry-picked
  into the fork and crediting its author.
- `README` and `NOTICE` credit nr-mcp (and PR #3's author) as the origin ("based on nr-mcp").
- Our additions are MIT as well, to stay maximally adoptable.
- This is **not** a GitHub "Fork" (linked, in-the-shadow). It is a fresh, independently named
  repository so it can attract its own community and rank in search.

## 5. Architecture

Python + [FastMCP](https://github.com/jlowin/fastmcp). The code is split into small,
independently testable units with clear boundaries. Each unit answers: *what does it do, how do
you use it, what does it depend on?*

```
MCP client (Claude Code / Desktop / others)
        │  (stdio | HTTP/SSE)
        ▼
┌─────────────────────────────────────────────┐
│  Transport / packaging layer                 │  ← stdio · Docker(HTTP/SSE) · HA add-on
├─────────────────────────────────────────────┤
│  MCP tool layer (FastMCP tools & resources)  │  ← the public interface (§6)
├───────────────┬───────────────┬─────────────┤
│ Safe-deploy   │ HA-awareness  │ Backup store │
│ engine        │ module        │              │
├───────────────┴───────────────┴─────────────┤
│  Node-RED Admin API client (HTTP)            │  ← auth, GET/POST /flows, modules, inject
└───────────────────────────────────────────────┘
        │  HTTP (Admin API)        │ optional HTTP (HA REST API)
        ▼                          ▼
   Node-RED                   Home Assistant (optional, for validation)
```

### 5.1 Node-RED Admin API client
- Pure HTTP wrapper around Node-RED's Admin API. Handles auth (token / basic / none).
- Methods: `get_flows()`, `post_flows(flows, rev, deployment_type)`, `get_settings()`,
  `get_modules()`, `install_module()`, `inject(node_id)`, `get_debug()`.
- Depends on: `httpx`, config. Knows nothing about MCP or HA. Fully unit-testable against a
  mocked HTTP server.

### 5.2 Safe-deploy engine
- Implements the **GET → mutate-in-place → POST /flows** pattern with **optimistic locking**:
  reads the current `rev`, applies changes, posts with that `rev`; on `409 Conflict` it re-reads
  and retries once, else returns a clear conflict error. **Never** uses `PUT /flow/:id` (which
  reorders tabs).
- Supports **batched multi-node patches in a single GET→POST cycle** (one optimistic-locking
  round for many edits, instead of N conflict-prone deploys) — powers `nr_safe_deploy_many`.
- Exposes the **deploy gate** (compute proposed flows → diff → propose/confirm → backup → post)
  that every mutating tool routes through. nr-mcp's tools deploy *immediately*; this project
  wraps that logic with the backup + propose-gate described in §8.
- Always asks the **Backup store** to snapshot the affected flow(s) *before* posting.
- Depends on: Admin API client, Backup store. No MCP/HA knowledge.

### 5.3 Backup store
- Saves a timestamped JSON snapshot of flows before every mutating deploy.
- Lists and restores snapshots. Location configurable (default: `./backups`, or a mounted
  volume in Docker / add-on).
- Pure, independently testable.

### 5.4 HA-awareness module
- Recognizes HA node types from `node-red-contrib-home-assistant-websocket`
  (e.g. `server`, `api-call-service`, `server-state-changed`, `poll-state`, `trigger-state`, …).
- Summarizes HA nodes (which entity/service/event they touch).
- **Optional live validation:** if `HA_URL` + `HA_TOKEN` are configured, query HA's REST API to
  list entities/services and flag references to non-existent entities/services *before* deploy.
- Depends on: an optional HA REST client. Degrades gracefully (static recognition only) when HA
  credentials are absent.

### 5.5 MCP tool layer
- FastMCP app exposing the tools/resources of §6. Thin: validates inputs, calls the engines,
  formats results. Enforces the deploy **mode** (propose vs auto).

### 5.6 Transport / packaging
- One entrypoint, transport chosen by config/flag: `--stdio` (default) or `--http` (HTTP/SSE,
  host/port from env).
- **Docker image:** runs the HTTP/SSE server.
- **HA add-on:** the same image + `config.yaml` (options schema) + ingress; reaches Node-RED
  over the **internal Docker network**, so port 1880 need not be exposed to the LAN.

## 6. MCP tools (the interface)

Naming prefix `nr_` (kept from nr-mcp for familiarity); HA-specific tools use `ha_`.

**Inspect (read-only):**
- `nr_get_flow_summary` — tabs + node counts overview.
- `nr_get_flow` — full flow by id/tab; optional `fields` to return only selected node
  attributes (smaller payload for large flows).
- `nr_search_nodes` — find nodes by type/name/property.
- `nr_get_node_config` — a node's config.
- `nr_get_function_code` — a function node's code.
- `nr_get_flow_context` — context vars for a flow.
- `nr_get_global_context` — global runtime context vars shared across flows (optional key).
- `nr_get_debug_output` — recent debug messages.
- `ha_list_ha_nodes` — all Home Assistant nodes in the flows, with the entity/service/event each uses.
- `ha_get_server_config` — the HA server config node(s) Node-RED uses.

**Author + deploy (each is GET→modify→POST through the safety gate of §8):**
- `nr_safe_deploy` — patch one node's `fields` (merge; immutable `id`/`type`/`z` rejected).
- `nr_safe_deploy_many` — patch many nodes' fields in one atomic deploy (single locking cycle).
- `nr_add_wire` — idempotently add a wire `from_id[output_index] → to_id` (preserves existing).
- `nr_create_nodes` — add new nodes/groups (validates ids, target tabs, duplicates).
- `nr_delete_nodes` — delete nodes; cleans up `group.nodes` and `wires` references.

All mutating tools accept `confirm: bool` and route through the gate: in `propose` mode they
return a diff **preview** and write nothing unless `confirm=true`; every real write is
backup-first.

**Backups & safety:**
- `nr_backup_flow` — manual snapshot.
- `nr_list_backups` — list snapshots.
- `nr_restore_backup` — restore a snapshot (itself a safe deploy).

**Validation & testing:**
- `ha_validate_calls` — validate HA service/entity references against live HA (if configured).
- `nr_inject` — trigger an inject node to test.

**Modules:**
- `nr_get_installed_modules`, `nr_install_module`.

## 7. Connection & authentication

- **To Node-RED:** `NR_URL` (default `http://localhost:1880`) + auth resolved in order
  `token → basic → none`: `NR_TOKEN`, or `NR_USER`/`NR_PASS`.
- **To Home Assistant (optional, for validation only):** `HA_URL` + `HA_TOKEN` (long-lived).

> ⚠️ **Open item — HA Node-RED add-on auth.** The community add-on ties editor auth to the HA
> login. Determining how a programmatic client authenticates to its Admin API (obtain a token,
> use basic auth, or — last resort, LAN-only — `leave_front_door_open: true`) must be resolved
> empirically during implementation against the author's instance. The client supports all three
> so we can pick the most secure option that works.

## 8. Safety workflow

Every mutating tool deploys through a shared **safety gate**:
1. GET current flows (+ `rev`).
2. Compute the *proposed* flows in memory — deterministically from the tool's arguments.
3. Diff current vs proposed → a human-readable **preview** (added / removed / changed nodes).
4. If `DEPLOY_MODE=propose` **and** `confirm` is not `true` → return the preview and **stop**
   (nothing is written).
5. Otherwise: snapshot a **backup** of the current flows, then POST with optimistic locking
   (on `409 Conflict` refetch, recompute the proposal against fresh state, retry once).

This is **stateless** — there is no hidden pending changeset. Re-calling the same tool with
`confirm=true` recomputes against the current state, so two concurrent edits can never be
silently mixed. `DEPLOY_MODE=auto` skips step 4 (still backup-first). A backup is **always**
taken before any real write, in both modes.

## 9. Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `NR_URL` | `http://localhost:1880` | Node-RED base URL |
| `NR_TOKEN` / `NR_USER` / `NR_PASS` | — | Node-RED Admin API auth |
| `HA_URL` / `HA_TOKEN` | — | Optional HA REST for validation |
| `DEPLOY_MODE` | `propose` | `propose` \| `auto` |
| `BACKUP_DIR` | `./backups` | Snapshot location |
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `0.0.0.0` / `8080` | HTTP/SSE bind |

## 10. Deployment targets

1. **stdio** — `pipx install` / `uvx`; MCP client spawns it locally.
2. **Docker** — published image, `MCP_TRANSPORT=http`; runs anywhere with network access to Node-RED.
3. **HA add-on** — wrapper repo (`config.yaml`, `Dockerfile` reusing the image, ingress);
   one-click install for the HA community; talks to Node-RED over the internal network.

## 11. Testing strategy

- **TDD** throughout (red → green → refactor).
- **Unit tests** for every engine against a **mocked Node-RED Admin API** (`respx`/`httpx`
  mock): optimistic-locking happy path, 409-retry, tab-order preservation, backup-before-write,
  HA-node recognition, propose-mode gating.
- **Integration test** against a **real Node-RED in Docker** (compose) seeded with sample flows
  including HA nodes: full create → propose → deploy → restore cycle.
- **Acceptance:** point it at a real, complex installation (read-only first), confirm summaries
  and HA-node listing are correct before any write.

## 12. Success criteria

1. Can read and summarize a real, complex Node-RED install accurately.
2. A deploy never reorders tabs and never silently overwrites a concurrent manual edit
   (conflict surfaced).
3. Every write is preceded by a restorable backup.
4. `propose` mode never writes without explicit confirmation.
5. HA nodes are recognized and (with HA creds) bad entity/service references are flagged.
6. Runs unchanged as stdio, Docker HTTP/SSE, and HA add-on.

## 13. Open questions & risks

- **HA add-on Admin API auth** (see §7) — main unknown; resolve early.
- **HA-awareness depth** — v1 = recognition + optional validation; deeper "suggest a flow"
  generation (à la joolui) is a later milestone.
- **nr-mcp code shape** — confirm how cleanly its modules separate; we may restructure into the
  units of §5 during the fork.
- **Python 3.14** on this dev box is bleeding-edge; verify nr-mcp deps have wheels, else pin a
  Python.

## 14. Future (post-v1)

- "Suggest/scaffold a Node-RED flow for HA goal X" (generative, joolui-style).
- Publish to PyPI + Docker Hub/GHCR + a HA add-on repository.
- Optional read-only "explain this flow" mode for onboarding.
