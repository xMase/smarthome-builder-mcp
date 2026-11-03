# ESPHome official MCP server — impact on our `esphome_*` tools

Date: 2026-11-03. Status: direction agreed; design in `2026-11-03-esphome-mcp-proxy-design.md`.

## What shipped upstream

The ESPHome Device Builder now has a built-in MCP server at `POST /api/mcp`
(device-builder #2733, first in Device Builder 1.15.0; ESPHome 2026.10.0 bundles 1.25.0).
The add-on announces it to Home Assistant via Supervisor discovery (`esphome/esphome#19413`).

- Transport: stateless streamable HTTP. One JSON-RPC request per POST, JSON reply, no session id,
  no server stream, no batches. Protocol versions `2025-06-18` / `2025-11-25`.
- Auth: the ingress site is trusted (no auth). The public site (port 6052) uses the dashboard
  password (`Basic` or `Bearer` session token). On the add-on, the ingress port accepts only
  loopback and the Supervisor.
- Tools return text plus `isError`; failures start with the error code (`precondition_failed`,
  `not_found`, `invalid_args`, `unavailable`, `internal_error`). No tool annotations.

### Verified live (2026-11-03)

Our existing ingress path reaches it. `_open_ingress()` → `POST {HA_URL}/{ingress_url}/api/mcp`
with cookie `ingress_session=<id>`, plain `httpx`:

- `initialize` → 200, `serverInfo: {"name": "ESPHome Device Builder", "version": "1.25.0"}`
- `tools/list` → 200, 21 tools.

So a remote client of our server can use the upstream tools with no new auth and no new dependency.

## Tool comparison

| Area | Upstream (`/api/mcp`) | Ours (`esphome_*`) |
|---|---|---|
| Inventory | `list_devices` | `esphome_list_devices` |
| Read YAML | `get_config` (also reads `secrets.yaml`) | `esphome_get_config` (+ sha256; `secrets.yaml` blocked unless `ESPHOME_ALLOW_SECRETS`) |
| Write YAML | `update_config {expected}` — no preview, undo via Device Builder version history | `esphome_update_config` — diff preview → `confirm` per `DEPLOY_MODE`, local backup, sha256 check, also sends `expected` |
| Undo | version history (Device Builder side, needs `git`) | `esphome_list_backups` / `esphome_restore_backup` (local files) |
| Validate | `validate_config {tail_lines}` — concealed values replaced by `<removed>` | `esphome_validate` — raw output tail |
| Build / flash | `compile`, `install` (no confirm), `get_job`, `cancel_job` | `esphome_compile`, `esphome_install` (always `confirm`), `esphome_job_status` (with wait), `esphome_cancel_job` |
| Device logs | **none** | `esphome_logs` |
| Create device | `create_device`, `search_boards` | none |
| Structured edits | `add_component`, `list_automations`, `get_available_automations`, `get_automation_docs`, `upsert_automation`, `delete_automation` | none |
| Catalog | `search_components`, `get_component`, `get_config_components` | none |
| Secrets | `list_secret_names`, `set_secret` (overwrite is unrecoverable) | none |
| Reach | ingress (inside HA) or port 6052 | any MCP client, through HA ingress with `HA_TOKEN` |

What only we give: remote reach for add-on users, device logs, confirm gates (YAML and flash),
local backups, the `secrets.yaml` read block.
What only upstream gives: create device, catalog, structured component/automation edits, secrets
tools, redaction of concealed values.

## Why our `esphome_*` tools must not duplicate upstream

A second copy of upstream tools gives the agent duplicate names and no new capability.
Our ESPHome layer is worth keeping only for what upstream does not give:

- **Remote reach.** On the add-on, `/api/mcp` is reachable only from inside HA. We reach it
  through HA ingress with `HA_TOKEN`.
- **Safety gates.** Upstream writes at once: `update_config`, `set_secret` (unrecoverable
  overwrite) and `install` (a flash cannot be undone) have no confirm step and no tool annotations.
  We add a confirm step, a local backup before every write, and the `secrets.yaml` block.
- **Device logs.** Upstream has no logs tool.
- **One server for three systems.** ESPHome device, HA entity and Node-RED flow in one place, e.g.
  "the ESPHome sensor sends no data and the flow stopped — why?". No upstream server does this.

## Direction: a gated proxy over upstream `/api/mcp`

Our ESPHome layer becomes a proxy with gates, not a second implementation of the Device Builder
protocol.

| Part | Change |
|---|---|
| Transport | New `_mcp_call(tool, arguments)` in `esphome.py`: reuse `_open_ingress()`, `POST {HA_URL}/{ingress_url}/api/mcp` with cookie `ingress_session`, JSON-RPC `tools/call`. Map `isError` codes to `NRError` classes, as `_ERROR_CLASSES` does for the websocket. |
| Reads | `list_devices`, `get_config`, `validate_config`, `get_job`, `cancel_job`, catalog, automation listing go to upstream. They get the concealed-value redaction for free. |
| Writes | `update_config`, `add_component`, `create_device`, `upsert_automation`, `delete_automation`, `set_secret`, `install` go to upstream, but only through our gate: preview → `confirm=true` (per `DEPLOY_MODE`; flash and secrets always), local backup first. |
| Logs | Stay on our websocket (`devices/logs`). |
| `secrets.yaml` | `check_configuration()` still runs on every `configuration` argument before any call. |

What this removes: most of our websocket command code (`devices/*`, `firmware/*` calls), so less
code to keep in sync with Device Builder protocol changes. The websocket stays only for logs.

## Design questions for the spec

1. **Tool surface for upstream-only capabilities** (catalog, automations, create device, secrets).
   One generic `esphome_mcp_call(tool, arguments, confirm)` that exposes upstream schemas via
   `tools/list`, or one `esphome_*` tool per upstream tool (about 10 more tools, three-place count
   update). Generic is smaller and follows upstream changes; explicit tools give the agent better
   descriptions.
2. **Preview for backend-rendered writes.** `add_component` and `upsert_automation` render YAML on
   the server, so we cannot diff before the write. Candidate: `confirm=false` describes the call;
   `confirm=true` snapshots the YAML, writes, and returns the before/after diff and the `validation`
   field. Undo through our backup or upstream version history.
3. **Backup of `secrets.yaml` before `set_secret`.** Read it server-side into the local backup
   without returning the values to the model.
4. **Existing tool names.** Keep `esphome_*` names and result shapes where possible, so clients and
   prompts keep working; list any field that changes (e.g. validate output now redacted).
5. **Minimum version.** Require Device Builder ≥ 1.15 (first with `/api/mcp`) and fail with a clear
   error below it, or keep the websocket path as fallback. Fallback doubles the code; prefer the
   clear error unless real users run older add-ons.

## Rejected alternatives

- **Coexist, docs only.** Leaves duplicate tools and no added value.
- **Drop our `esphome_*` tools.** Not reachable from outside HA on the add-on; loses logs, gates
  and backups.

## Open points

- Check whether the websocket `devices/validate` stream (our `esphome_validate`) also strips
  concealed values, or whether only the MCP layer does. If only MCP, our validate output can show
  secrets (the README already warns about `!include secrets.yaml`).
- Upstream tools answer within HA's 10 s per-call budget; long work is start-then-poll, which fits
  our `REPLY_TIMEOUT`.

## Sources

- https://esphome.io/blog/2026/10/21/esphome-2026-10/
- https://github.com/esphome/device-builder/blob/main/docs/API.md (section "MCP")
- https://github.com/esphome/esphome/pull/19413
- https://github.com/jrigling/esphome-mcp-integration/issues/14
