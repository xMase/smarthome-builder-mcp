# ESPHome tools as a gated proxy over the Device Builder MCP server — design

Date: 2026-11-03. Background and upstream facts:
`2026-11-03-esphome-official-mcp-analysis.md` (same directory).

## Intent

The ESPHome Device Builder (add-on 2026.10.x, Device Builder ≥ 1.15) now ships its own MCP server
at `POST /api/mcp`. Our `esphome_*` tools reimplement most of it over the Device Builder websocket.
A second copy has no value and must follow every upstream protocol change.

Our server keeps value only where upstream gives none:

- **Safe remote reach.** On the add-on, `/api/mcp` answers only on the ingress port (loopback and
  Supervisor). Opening port 6052 exposes the Device Builder with **no authentication** (the HA-login
  option was removed upstream). We reach it through HA ingress, protected by the HA admin token.
- **Gates.** Upstream writes and flashes at once, with no tool annotations. We add a confirm step
  and a local backup before writes.
- **Device logs.** Upstream has no logs tool.
- **One server for Node-RED, HA and ESPHome.**

So the ESPHome layer becomes a **proxy with gates**: upstream tools are exposed as our own tools,
generated from upstream's `tools/list`; we add only transport, gates, backups and logs.

## Goals

- Every upstream tool is available to the client as a normal tool `esphome_<upstream name>`, with
  upstream's description and input schema. The client sees the schemas; it never guesses.
- No per-tool wrapper code. A new or changed upstream tool needs no change on our side.
- Mutating calls pass our confirm gate; config-changing calls snapshot a local backup first.
- The `secrets.yaml` read block (`ESPHOME_ALLOW_SECRETS`) keeps working.

## Non-goals

- No websocket fallback for Device Builder < 1.15.
- No polling of upstream and no `notifications/tools/list_changed` push (add-on updates are rare;
  a stale session gets a clear error, see below).
- No change to the Node-RED / HA tools.
- No guidance to open port 6052 on the add-on.

## Anti-goals

- A mutating upstream tool that runs without our gate, including one added by a future upstream
  release.
- Secret values sent to the model when `ESPHOME_ALLOW_SECRETS=false`.
- Server startup that fails because HA or the add-on is unreachable.

## Decisions (agreed with the user)

1. **Proxy, not reimplementation.** Reads and writes go to upstream `/api/mcp`.
2. **Generated tools, not one generic tool.** Reason: per-tool schemas for the model, lazy schema
   loading in Claude Code, per-tool permission rules in the client.
3. **The tool list is built per `tools/list` request**, not once at process start, so a long-running
   http-mode container picks up an add-on update without restart. A new client session sees the new
   list.
4. **Stale session:** a call to a tool that upstream no longer has, or with arguments upstream now
   refuses, fails with a clear error that says to reconnect (`/mcp`) or open a new session. Upstream
   checks arguments before it acts, so nothing is written.
5. **Gate by allowlist of non-mutating tools.** Any upstream tool not on the list is gated. A new
   upstream tool is unknown to us, so it always needs `confirm=true`, also in `auto`.
6. **Existing names are replaced.** The generated tools take the `esphome_` names; today's
   overlapping static tools are removed, even where upstream parameters differ.
7. **README:** users with a standalone ESPHome install (password-protected port 6052) can also
   connect the upstream server directly.

## How it works

### Transport

New async function in `esphome.py`, e.g. `_mcp_rpc(settings, method, params)`:

- `_open_ingress(settings)` (existing) → `ingress_url`, session id.
- `POST {HA_URL}/{ingress_url}/api/mcp`, cookie `ingress_session=<id>`, JSON-RPC 2.0 body,
  `Content-Type: application/json`. Use `httpx` (already a dependency).
- Verified live on 2026-11-03: `initialize` → 200, `serverInfo.version` 1.25.0; `tools/list` → 200,
  21 tools. Upstream is stateless: no session id, `tools/list` and `tools/call` work without a
  prior `initialize`. (Builder: confirm; if `initialize` turns out to be required, send it first.)
- HTTP 404 on `/api/mcp` → error "ESPHome Device Builder ≥ 1.15 is required for esphome_* tools".
- Auth / reachability errors reuse today's mapping (`NRAuthError`, `ESPHomeError`).

### Upstream contract (captured live, Device Builder 1.25.0)

Full `tools/list` reply saved in `.tmp/esphome-mcp-tools-list-1.25.0.json` (gitignored; copy it into
`tests/fixtures/` as the unit-test fixture). Facts the gate relies on:

- The file argument is always `configuration`: `get_config`, `update_config`
  (`configuration, content, expected`, all required), `add_component`, `validate_config`,
  `compile`, `install` (`configuration, port?`), `get_config_components`, `list_automations`,
  `get_available_automations`, `upsert_automation` (`configuration, location, automation,
  expected?`), `delete_automation` (`configuration, location, expected`).
- Other tools: `get_job` / `cancel_job` (`job_id`), `set_secret` (`name, value, overwrite?`),
  `create_device` (`name, friendly_name?, board_id?`), `search_*` (`query, limit?`),
  `get_component`, `get_automation_docs` (`refs`).
- No tool has `annotations`.
- Results are one text content item holding JSON (e.g. `list_devices` → a JSON array of flat device
  rows).
- Tool failure: `result.isError: true`, text `"<code>: <message>"`, e.g.
  `"not_found: Device 'x.yaml' not found"`.
- Unknown tool: JSON-RPC error `{"code": -32602, "message": "Unknown tool: <name>"}`.

### Tool listing

`MCPServer` (mcp SDK 2.3.0) routes `tools/list` to `MCPServer.list_tools()` and `tools/call` to
`MCPServer.call_tool()`. Subclass `MCPServer` (or wrap these two methods) in `server.py`:

- `list_tools()` = static tools + upstream tools fetched with `tools/list`, each renamed to
  `esphome_<name>`, description and `inputSchema` from upstream. Mutating tools get an extra
  boolean property `confirm` (default false) added to their schema and a line in the description
  that says when confirm is needed.
- If `HA_URL` / `HA_TOKEN` are unset → no upstream tools, no error.
- If HA or the add-on is unreachable, or upstream returns an error → static tools only, warning in
  the log. `tools/list` never fails because of ESPHome.
- A generated name that collides with a static tool is skipped (static wins).
- SDK detail to verify: `Server(get_tool_input_schema=...)` looks up a schema before the call; it
  must not reject generated tools. If it does, override `_tool_input_schema` too.

### Tool calls

`call_tool(name, arguments)` for a name that is not static and starts with `esphome_` → strip the
prefix, apply the gate, forward as `tools/call`. Results and errors use the same `_ok` / `_err`
JSON envelope as every other tool (my call), so gate errors, `check_configuration` errors and
upstream errors look the same to the client:

- success → upstream JSON text, parsed, inside `_ok`;
- `isError: true` → map the leading code like `_ERROR_CLASSES` (`not_found`, `precondition_failed`,
  …; others → `ESPHomeError`), keep upstream's message;
- JSON-RPC `-32602` with message `Unknown tool: …` → the stale-session error of decision 4;
  any other JSON-RPC error → pass its message through.

Time limits: every `_mcp_rpc` call is bounded by the existing `CONNECT_TIMEOUT` / `REPLY_TIMEOUT`.
No cache of the tool list or of the ingress session (my call).

### Gate

Three classes, decided by upstream tool name:

| Class | Upstream tools (1.25.0) | Gate |
|---|---|---|
| No confirm | `list_devices`, `get_config`, `validate_config`, `get_job`, `cancel_job`, `compile`, `search_components`, `get_component`, `get_config_components`, `search_boards`, `list_secret_names`, `list_automations`, `get_available_automations`, `get_automation_docs` | none |
| Confirm per `DEPLOY_MODE` | `update_config`, `add_component`, `create_device`, `upsert_automation`, `delete_automation` | `propose` → needs `confirm=true`; `auto` → runs |
| Always confirm | `install`, `set_secret`, and **every tool not in the two rows above** (a future upstream tool) | `confirm=true` always, also in `auto` |

`compile` and `cancel_job` are in the first row because they write nothing and our current
`esphome_compile` / `esphome_cancel_job` have no confirm (my call). `confirm` is removed from the
arguments before forwarding.

**Without confirm**, a gated call returns a preview and writes nothing:
- `update_config`: unified diff of current YAML vs `arguments.content` (reuse `_diff`), plus
  `requires_confirm: true`.
- Other gated tools: the tool name, the arguments, and `requires_confirm: true`. Backend-rendered
  edits (`add_component`, `upsert_automation`, `delete_automation`) cannot be diffed before they run
  (my call: no dry-run emulation).
- `install`: also the device row from `list_devices` and the offline/deferred warning, as today's
  `esphome_install` does (my call).

**With confirm**, for gated calls that carry a `configuration` argument of an existing file,
except `install` (a YAML snapshot before a flash adds nothing; my call):
1. read the current YAML (upstream `get_config`) and snapshot it with `YamlBackupStore`;
2. forward the call;
3. read the YAML again and add the before/after diff and the `backup_id` to the result.

A gated tool without a `configuration` argument gets no backup; upstream version history covers
YAML writes (my call).

`set_secret`: snapshot `secrets.yaml` server-side the same way before the call; its content never
goes into the result. `create_device` has nothing to back up.

### `secrets.yaml`

Every forwarded call runs `check_configuration()` on a `configuration` argument when present, so
`esphome_get_config` on `secrets.yaml` stays blocked unless `ESPHOME_ALLOW_SECRETS=true`. Upstream
already refuses the secrets file on all other tools.

### Tools after the change

| Static (stay in `server.py`) | Removed (replaced by generated tools) |
|---|---|
| `esphome_logs` (websocket `devices/logs`) | `esphome_list_devices`, `esphome_get_config`, `esphome_update_config`, `esphome_validate`, `esphome_compile`, `esphome_install`, `esphome_job_status`, `esphome_cancel_job` |
| `esphome_list_backups` | |
| `esphome_restore_backup` (keeps today's websocket write path and its own gate unchanged; upstream `update_config` refuses `secrets.yaml`, so a secrets backup could not be restored through it — my call) | |

Name changes visible to clients: `esphome_validate` → `esphome_validate_config`,
`esphome_job_status` → `esphome_get_job`; parameters follow upstream (e.g. `update_config` needs
`expected`, the text returned by `get_config`, instead of our `base_sha256`).

Static tool count goes from 51 to 43. Update the three places: `server.py`,
`tests/test_server_smoke.py` (`expected` set and `len(names) == 43`), `scripts/smoke_e2e.py`
`EXPECTED_TOOLS`. `tests/test_tool_count_sync.py` must keep counting only static tools.

Code that becomes unused (websocket `devices/list`, `devices/get_config` reads for the removed tools,
`firmware/*`, `validate`, job helpers) is deleted, except what `esphome_logs` and
`esphome_restore_backup` still use: `_session`, `_DB`, `_read_or_none`, `update_config` (now
internal, untouched), `YamlBackupStore`, `check_configuration`. The old `update_config` and the new
gate both protect writes, in two ways; accepted, because only restore uses the old one.

### Server instructions and README

- `instructions`: ESPHome tools come from the Device Builder; writes and flashes need
  `confirm=true`; read `esphome_get_config` before `esphome_update_config` and pass its text as
  `expected`.
- README: new ESPHome tool table (static + "generated from Device Builder ≥ 1.15"), the gate classes,
  the standalone-direct-connection note, removal of the replaced tools in the changelog.

## Tests

- Unit, no network: inject the `/api/mcp` transport the way `session=` / `ha_connect=` are injected
  today. Cover: listing merge and rename, schema gets `confirm`, HA unset / unreachable → static
  only, gate per class and `DEPLOY_MODE`, preview diff for `update_config`, backup before a
  confirmed config write, `secrets.yaml` block, `isError` mapping, `-32602` stale error, 404 →
  version error, unknown new upstream tool is gated.
- Live check against the real add-on (read-only: `tools/list`, `esphome_list_devices`,
  `esphome_get_config` on a device) before release.

## Left to the builder

- Subclass vs. method wrapping of `MCPServer`.
- How upstream text results are wrapped (raw text vs parsed JSON in our `_ok` envelope).
- Caching the ingress session inside one `tools/call` (backup read + call + re-read) — one ingress
  session per tool call is fine.
- Exact error message wording.
