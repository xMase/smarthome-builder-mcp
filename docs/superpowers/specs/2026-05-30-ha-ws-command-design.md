# Design: `ha_ws_command` — generic Home Assistant WebSocket command tool

Date: 2026-05-30

## Problem

To mutate the Home Assistant registry (e.g. `config/entity_registry/remove`,
`config/entity_registry/update`) the agent currently has **no MCP tool**. Its only
recourse is a workaround: author a *temporary* Node-RED node that injects the raw
WebSocket command into the HA server connection, then deploy it through Node-RED's
deploy cycle (backup, rev, etc.) for an operation that has nothing to do with the
flows. This is awkward, pollutes flows, and — crucially — runs **arbitrary WS
commands with zero guardrails**.

The danger of arbitrary WS commands therefore *already exists* today, in its worst
form. Centralizing that capability in a single, gated MCP tool is strictly **safer**
than the status quo.

## Goal

A single generic MCP tool that sends an arbitrary HA WebSocket command and returns
its result, protected by the same tiered-safety pattern the project already uses for
`ha_call_service` (preview → `confirm=true`, plus an env-extensible block-list).

Non-goals: typed per-command wrappers (entity/device/area CRUD), subscriptions/streaming.

## Architecture

One new function in `ha_ws.py`, `run_command(...)`, reusing the existing transport
(`_default_connect`, `_HAWsConn`, `_auth`, injectable `connect` for tests). One thin
MCP tool in `server.py`, `ha_ws_command`, that guards on `ha_url`/`ha_token` and
delegates via `_wrap`. No HTTP/WS logic in the server — consistent with the rest.

```
ha_ws_command(command, params={}, confirm=False)
   └─ ha_ws.run_command(settings, command, params, confirm, connect=None)
        ├─ _classify(command) -> "read" | "write"
        ├─ read               -> execute immediately
        ├─ write & blocklist  -> HAWsError (confirm cannot override)
        ├─ write & !confirm    -> return preview {would_send, requires_confirm}
        └─ write & confirm     -> _auth -> send {id, type, **params} -> result
```

### Tool surface

```python
@mcp.tool()
async def ha_ws_command(command: str, params: dict | None = None,
                        confirm: bool = False) -> str
```

- `command`: the WS message `type` (e.g. `"config/entity_registry/remove"`).
- `params`: extra fields merged into the WS message (e.g. `{"entity_id": "..."}`).
- `confirm`: required to execute a *write* command.
- No-ops with a clear message when `ha_url`/`ha_token` are unset (like the other `ha_*`).

## Read/write classification (the core)

**Safe by default**: a command is treated as a *write* (requires `confirm`) **unless**
recognized as a read. An unknown command falls into the prudent path.

- **read** (execute immediately): `type` ending in `/list` or `/get`, or in the known
  allowlist: `get_states`, `get_config`, `get_services`, `render_template`,
  `recorder/statistics_during_period`. Extensible via env `HA_WS_READ_COMMANDS`
  (comma-separated, additive).
- **write** (preview → confirm): everything else.

## Safety

Mirrors `ha_call_service`:

- **block-list** `HA_WS_BLOCK_COMMANDS` (env, additive) → `HAWsError`; `confirm=true`
  does **not** override it. **Default floor is empty** (no invented "catastrophic" WS
  commands; the mechanism exists and is documented).
- preview without confirm:
  `{"preview": true, "would_send": {"type": command, **params}, "requires_confirm": true,
    "hint": "re-call with confirm=true to execute."}`.
- write success returns `{"ok": true, "command": ..., "result": <ha result>}`.
- HA error frame (`success: false`) → `HAWsError` with the HA error payload.
- Exposed in `nr_diagnostics` via the WS policy (read allowlist + block-list, with
  source = default|configured).

### Settings (`config.py`)

Two new optional env-backed fields on `Settings`:

- `ha_ws_block_commands: str = ""`
- `ha_ws_read_commands: str | None = None`

## Testing

`pytest` with an injectable `connect` (a fake conn returning canned frames), as the
other `ha_ws` tests already do. Cases:

1. read command (e.g. `config/entity_registry/list`) executes without confirm.
2. write without confirm returns a preview and sends **nothing** past auth.
3. write with confirm sends `{id, type, **params}` and returns the result.
4. command in block-list raises `HAWsError` even with `confirm=true`.
5. HA `success: false` frame → `HAWsError`.
6. unknown command defaults to write (preview without confirm).

## Tool-count sync

Adding a tool requires updating the **three** mandated places (guarded by
`tests/test_tool_count_sync.py`):

1. the `@mcp.tool()` in `server.py`,
2. the `expected` set **and** `len(names) == N` count in `tests/test_server_smoke.py`,
3. `EXPECTED_TOOLS` in `scripts/smoke_e2e.py`.

## Docs

- README Tools table: add `ha_ws_command` and bump the tool count.
- Mention the two new env vars where the other `HA_*` vars are documented.
