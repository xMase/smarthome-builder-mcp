# ESPHome MCP Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the ESPHome Device Builder's own MCP tools (`/api/mcp`) as generated `esphome_<name>` tools, reached through HA ingress, behind our confirm gate and local backups; remove our overlapping hand-written `esphome_*` tools.

**Architecture:** A new domain module `esphome_proxy.py` owns the JSON-RPC transport over HA ingress, the tool-list rewrite and the gate. `server.py` subclasses `MCPServer` so `list_tools()` appends the upstream tools on every `tools/list` and `call_tool()` routes `esphome_*` names that are not static tools to the proxy. `esphome.py` keeps only what `esphome_logs` and `esphome_restore_backup` use.

**Tech Stack:** Python 3.11+, `mcp` SDK 2.3.0 (`MCPServer`), `httpx`, `pytest-asyncio` (auto mode).

**Spec:** `docs/superpowers/specs/2026-11-03-esphome-mcp-proxy-design.md` (background: `2026-11-03-esphome-official-mcp-analysis.md`).

## Global Constraints

- Device Builder ≥ 1.15 required; HTTP 404 on `/api/mcp` → `ESPHomeError("ESPHome Device Builder >= 1.15 is required for esphome_* tools (no /api/mcp endpoint)")`.
- Endpoint: `POST {HA_URL}/{ingress_url}/api/mcp`, cookie `ingress_session=<id>`, JSON-RPC 2.0, `Content-Type: application/json`.
- Generated tool name = `esphome_` + upstream name. A generated name equal to a static tool name is skipped (static wins).
- No-confirm upstream tools: `list_devices`, `get_config`, `validate_config`, `get_job`, `cancel_job`, `compile`, `search_components`, `get_component`, `get_config_components`, `search_boards`, `list_secret_names`, `list_automations`, `get_available_automations`, `get_automation_docs`.
- Confirm per `DEPLOY_MODE`: `update_config`, `add_component`, `create_device`, `upsert_automation`, `delete_automation`.
- Always confirm (also `auto`): `install`, `set_secret`, and every upstream tool in neither list above.
- `confirm` is never forwarded to upstream (upstream schemas are closed objects and reject unknown keys).
- `check_configuration()` runs on every `configuration` argument before any network call.
- Every proxy result and error uses the existing `_ok` / `_err` JSON envelope.
- Timeouts: ingress open bounded by `CONNECT_TIMEOUT`; each HTTP request by `REPLY_TIMEOUT`. No cache of tool list or ingress session.
- `tools/list` never fails because of ESPHome: HA unset → no upstream tools, silently; any error → static tools only + `logging.warning`.
- Static tool count 51 → 43, updated in `server.py`, `tests/test_server_smoke.py`, `scripts/smoke_e2e.py`.
- Code, comments, docs, commits in English.

## Review Focus

1. **Non-JSON upstream text** (`get_config` returns raw YAML, errors are `"code: message"`) → returned as text, never a JSON decode crash. Test in Task 2.
2. **`confirm` sent to a no-confirm tool** (the model adds it anyway) → stripped, the call still succeeds. Test in Task 2.
3. **`update_config` on a file that does not exist** → the preview surfaces upstream `not_found` as an error; no backup attempt, no crash. Test in Task 2.
4. **HA reachable but add-on stopped or slow during `tools/list`** → static tools still listed, bounded by the timeouts. Test in Task 3.
5. **Upstream adds a tool whose name collides with a static tool** (e.g. a future `logs`) → static tool kept, upstream one skipped. Test in Task 3.

---

### Task 1: JSON-RPC transport over HA ingress

**Files:**
- Create: `src/nrha_mcp/esphome_proxy.py`
- Test: `tests/test_esphome_proxy.py`

**Interfaces:**
- Consumes: `esphome._open_ingress(settings, ha_connect=None) -> (ingress_url, session_id)`, `esphome.CONNECT_TIMEOUT`, `esphome.REPLY_TIMEOUT`, `esphome.ESPHomeError`, `client.NRAuthError`.
- Produces: `async def rpc(settings, method: str, params: dict | None = None, *, ha_connect=None, transport: httpx.AsyncBaseTransport | None = None) -> dict` — returns the JSON-RPC `result` object. Raises `ESPHomeError` / `NRAuthError` (below). Later tasks inject a fake `rpc` with the same signature.

- [ ] **Step 1: Write the failing tests** in `tests/test_esphome_proxy.py`. Reuse the fake HA connection pattern of `tests/test_esphome.py` (`_ha_frames`) for `ha_connect`; use `httpx.MockTransport` for `transport`.
  - `test_rpc_posts_jsonrpc_with_ingress_cookie`: the handler sees `POST` to `http://ha:8123/api/hassio_ingress/abc/api/mcp`, header `cookie` contains `ingress_session=<session from frames>`, JSON body `{"jsonrpc": "2.0", "method": "tools/list", ...}` with an `id`; `rpc(...)` returns the reply's `result`.
  - `test_rpc_404_is_version_error`: 404 → `ESPHomeError` whose message contains `>= 1.15`.
  - `test_rpc_401_403_is_auth_error` (parametrized 401, 403) → `NRAuthError`.
  - `test_rpc_unknown_tool_is_stale_error`: reply `{"error": {"code": -32602, "message": "Unknown tool: foo"}}` → `ESPHomeError` whose message contains `Unknown tool: foo` and `/mcp`.
  - `test_rpc_other_jsonrpc_error_passes_message`: `{"error": {"code": -32603, "message": "boom"}}` → `ESPHomeError` containing `boom`.
  - `test_rpc_timeout_is_esphome_error`: handler raises `httpx.ReadTimeout` → `ESPHomeError`.

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_esphome_proxy.py -q` — expected: FAIL (module missing).

- [ ] **Step 3: Implement `rpc`** in `src/nrha_mcp/esphome_proxy.py`. Open ingress with `asyncio.wait_for(_open_ingress(...), CONNECT_TIMEOUT)` (timeout → `ESPHomeError`, as `esphome._session` does). URL = `settings.ha_url.rstrip('/') + '/' + ingress_url.strip('/') + '/api/mcp'`. `httpx.AsyncClient(timeout=REPLY_TIMEOUT, transport=transport)`, cookie `ingress_session`. Stale-session message: `f"{message}: the ESPHome tool list changed (add-on updated?); reconnect this MCP server (/mcp) or start a new session."`. `httpx.HTTPError` → `ESPHomeError(f"cannot reach the Device Builder MCP endpoint: {type(e).__name__}: {e}")`.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_esphome_proxy.py -q` — expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/esphome_proxy.py tests/test_esphome_proxy.py
git commit -m "feat(esphome): JSON-RPC transport to the Device Builder MCP endpoint via HA ingress"
```

---

### Task 2: Tool-list rewrite and gated call

**Files:**
- Modify: `src/nrha_mcp/esphome_proxy.py`
- Create: `tests/fixtures/esphome_mcp_tools_list_1.25.0.json` (copy of `.tmp/esphome-mcp-tools-list-1.25.0.json`, the full live JSON-RPC reply)
- Test: `tests/test_esphome_proxy.py`

**Interfaces:**
- Consumes: `rpc` (Task 1); `esphome.check_configuration`, `esphome.YamlBackupStore`, `esphome._diff`, `esphome._ERROR_CLASSES`, `esphome.ESPHomeError`; `settings.deploy_mode`, `settings.backup_dir`.
- Produces:
  - `PREFIX = "esphome_"`, `NO_CONFIRM: frozenset[str]`, `DEPLOY_MODE_CONFIRM: frozenset[str]` (values from Global Constraints).
  - `def gate_class(tool: str) -> str` → `"none" | "deploy_mode" | "always"`.
  - `async def list_tools(settings, *, rpc=rpc) -> list[dict]` — each item `{"name": "esphome_<n>", "description": str, "inputSchema": dict}`; for gated tools the schema gets property `confirm` (`{"type": "boolean", "default": false, "description": ...}`) and the description gets one line: `"Writes: needs confirm=true (per DEPLOY_MODE)."` or `"Needs confirm=true always, also with DEPLOY_MODE=auto."`. Must not mutate the upstream dicts.
  - `async def call(settings, tool: str, arguments: dict, *, rpc=rpc, backups: YamlBackupStore | None = None) -> dict` — `tool` is the upstream name (no prefix). Returns a dict for `_ok`; raises `NRError` subclasses.

- [ ] **Step 1: Write the failing tests.** A fake `rpc` records `(method, params)` and answers `tools/call` per tool name from a dict; `tools/list` from the fixture's `result`.
  - `test_gate_class`: `gate_class("get_config") == "none"`, `gate_class("compile") == "none"`, `gate_class("update_config") == "deploy_mode"`, `gate_class("install") == "always"`, `gate_class("set_secret") == "always"`, `gate_class("delete_device") == "always"`.
  - `test_list_tools_renames_and_adds_confirm`: 21 tools; names all start with `esphome_`; `esphome_get_config` schema has no `confirm`; `esphome_update_config` and `esphome_install` schemas have `confirm` and keep upstream `required` unchanged.
  - `test_read_tool_forwards_without_confirm`: `call(s, "list_devices", {})` → `rpc` got `tools/call` `{"name": "list_devices", "arguments": {}}`; result `{"result": [<rows>]}` (JSON text parsed).
  - `test_non_json_text_returned_as_text` (Review Focus 1): `get_config` answers YAML text → `{"result": "esphome:\n  name: x\n"}`.
  - `test_confirm_stripped_for_no_confirm_tool` (Review Focus 2): `call(s, "compile", {"configuration": "x.yaml", "confirm": True})` → forwarded arguments are `{"configuration": "x.yaml"}`.
  - `test_secrets_blocked_before_network`: `call(s, "get_config", {"configuration": "secrets.yaml"})` with `esphome_allow_secrets=False` → `ESPHomeError`; `rpc` never called.
  - `test_is_error_maps_code`: upstream `isError` text `"not_found: Device 'x.yaml' not found"` → `NRNotFoundError` with that text; `"precondition_failed: ..."` → `NRConflictError`; `"invalid_args: ..."` → `ESPHomeError`.
  - `test_update_config_preview_in_propose`: `deploy_mode="propose"`, no confirm → returns `requires_confirm: True` and a `diff` of current vs `arguments["content"]`; only `get_config` was forwarded; no backup written.
  - `test_update_config_preview_missing_file` (Review Focus 3): `get_config` answers `isError` `not_found` → `NRNotFoundError`; no backup, no `update_config` forwarded.
  - `test_update_config_auto_runs_with_backup_and_diff`: `deploy_mode="auto"`, no confirm → order of forwarded calls `get_config`, `update_config`, `get_config`; one file in `BACKUP_DIR/esphome/`; result has `result`, `backup_id`, `diff`.
  - `test_install_always_needs_confirm`: `deploy_mode="auto"`, no confirm → preview with `requires_confirm: True`, the device row of `list_devices` whose `configuration` matches, and `warning` when that row has `state == "offline"` and `port` is absent or `"OTA"`; `install` not forwarded. With `confirm=True` → `install` forwarded, no backup.
  - `test_unknown_tool_always_gated`: `call(s, "delete_device", {"name": "x"})` in `auto` without confirm → preview, nothing forwarded.
  - `test_set_secret_backs_up_secrets_without_returning_them`: confirm → `get_config` `secrets.yaml` forwarded (even with `esphome_allow_secrets=False`: the internal read bypasses the block), a backup of `secrets.yaml` written, then `set_secret`; the returned dict contains no secret file content.
  - `test_backend_rendered_write_previews_call`: `upsert_automation` in `propose` without confirm → preview `{"preview": True, "tool": "upsert_automation", "arguments": {...}, "requires_confirm": True}`; nothing forwarded.

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_esphome_proxy.py -q` — expected: the new tests FAIL.

- [ ] **Step 3: Implement** `gate_class`, `list_tools`, `call` in `esphome_proxy.py`. Flow of `call`:
  1. pop `confirm` (truthy check, default false); if `"configuration"` in arguments → `check_configuration`.
  2. class `none` → forward.
  3. needs confirm = class `always`, or class `deploy_mode` and `settings.deploy_mode == "propose"`. If needed and not given → preview (diff for `update_config`; device row + offline warning for `install`; tool + arguments otherwise), nothing written.
  4. if the call carries `configuration` and the tool is not `install` → read current YAML via upstream `get_config` (a `not_found` error propagates), snapshot it with `YamlBackupStore`. `set_secret` → same for `secrets.yaml`, read with an internal helper that skips `check_configuration`.
  5. forward; on success for the backed-up configuration case → re-read and add `backup_id` and `diff` (before vs after). For `set_secret` add only `backup_id`.
  - Result text: `json.loads` it, on `ValueError` keep the raw string; return `{"result": value, ...}`.
  - `isError` text: code = text before the first `":"`; class from `esphome._ERROR_CLASSES`, default `ESPHomeError`.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_esphome_proxy.py -q` — expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/esphome_proxy.py tests/test_esphome_proxy.py tests/fixtures/esphome_mcp_tools_list_1.25.0.json
git commit -m "feat(esphome): rewrite upstream tool list and gate upstream calls with confirm and backups"
```

---

### Task 3: Wire generated tools into the server; remove replaced tools

**Files:**
- Modify: `src/nrha_mcp/server.py` (MCPServer construction ~line 65; ESPHome section ~line 476–566)
- Modify: `src/nrha_mcp/esphome.py` (delete code only the removed tools used)
- Modify: `tests/test_server_smoke.py`, `scripts/smoke_e2e.py`, `tests/test_esphome_server.py`, `tests/test_esphome.py`
- Modify: `README.md`, `CLAUDE.md` (ESPHome lines in "Domain modules")

**Interfaces:**
- Consumes: `esphome_proxy.list_tools`, `esphome_proxy.call`, `esphome_proxy.PREFIX` (Task 2).
- SDK types: `from mcp_types import CallToolResult, TextContent, Tool as MCPTool` (as `mcp/server/mcpserver/server.py` imports them). The low-level `get_tool_input_schema` hook returns `None` for a generated name and then skips its check, so it needs no override.
- Produces: `class _Server(MCPServer)` in `server.py` overriding `async list_tools(self) -> list[MCPTool]` and `async call_tool(self, name, arguments, context=None) -> CallToolResult`; module-level `mcp = _Server(...)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_esphome_server.py` (monkeypatch `srv.esphome_proxy.list_tools` / `srv.esphome_proxy.call`):
  - `test_list_tools_merges_upstream`: with HA set and a fake list of two upstream tools → `await srv.mcp.list_tools()` contains them as `MCPTool` with the given `input_schema`, plus all static tools.
  - `test_list_tools_ha_unset_static_only`: `ha_url=""` → only static tools; fake `list_tools` never called.
  - `test_list_tools_upstream_error_static_only` (Review Focus 4): fake raises `ESPHomeError` → only static tools, and a warning logged (`caplog`).
  - `test_list_tools_skips_collision` (Review Focus 5): upstream tool named `esphome_logs` → exactly one `esphome_logs`, the static one (its schema has `seconds`).
  - `test_call_routes_generated_name`: `await srv.mcp.call_tool("esphome_get_config", {"configuration": "x.yaml"})` → fake `call` got `("get_config", {"configuration": "x.yaml"})`; result text is `_ok({...})`.
  - `test_call_generated_error_is_err_envelope`: fake raises `NRNotFoundError("nope")` → result text JSON `{"error": "nope"}`.
  - `test_call_generated_needs_ha`: `ha_url=""` → `{"error": ...}` containing `HA_URL and HA_TOKEN`.
  - `test_static_tool_still_works`: `call_tool("esphome_list_backups", {})` still runs the static tool.
  - Delete the tests of removed tools (`test_esphome_bad_name_is_structured_error` moves to the generated path: same assertion via `call_tool("esphome_get_config", {"configuration": "../x.yaml"})`; `test_esphome_install_forwards_confirm_and_port` is deleted).

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_esphome_server.py -q` — expected: FAIL.

- [ ] **Step 3: Implement** in `server.py`:
  - `_Server.list_tools`: `static = await super().list_tools()`; if HA set, fetch upstream (catch `Exception`, `logging.getLogger(__name__).warning(...)`), drop names in the static set, append `MCPTool(name=..., description=..., input_schema=...)`.
  - `_Server.call_tool`: if `name` is registered in `self._tool_manager` or does not start with `esphome_proxy.PREFIX` → `super().call_tool(...)`; else → `_esphome_unset() or await _wrap(lambda: esphome_proxy.call(settings, name[len(PREFIX):], arguments))`, returned as `CallToolResult(content=[TextContent(type="text", text=...)])`.
  - Delete the `@mcp.tool()` functions `esphome_list_devices`, `esphome_get_config`, `esphome_update_config`, `esphome_validate`, `esphome_compile`, `esphome_install`, `esphome_job_status`, `esphome_cancel_job`. Keep `esphome_logs`, `esphome_list_backups`, `esphome_restore_backup`.
  - Append to `HA_AUTHORING_GUIDANCE`: `"\n\nESPHome: esphome_* tools other than esphome_logs / esphome_list_backups / esphome_restore_backup come from the Device Builder itself. Writes need confirm=true (install and set_secret always). Before esphome_update_config, read esphome_get_config and pass its text as expected."`
- [ ] **Step 4: Delete unused code** in `esphome.py` and its tests in `tests/test_esphome.py`: `list_devices`, `get_config`, `validate`, `compile_firmware`, `install`, `job_status`, `cancel_job`, `_flat_device` and its field tuples, `_job`, `_job_list`, `_check_job_id`, `TERMINAL`, `_JOB_FIELDS`, `VALIDATE_TIMEOUT`, `JOB_MAX_WAIT`, `REPLAY_SECONDS`, `STREAM_TAIL` if unused. Keep `_session`, `_DB` (incl. `stream`), `_read_or_none`, `_diff`, `update_config` (now internal to `restore_backup`), `restore_backup`, `list_backups`, `logs`, `YamlBackupStore`, `check_configuration`, `_ERROR_CLASSES`. Verify with `tokensave_search` / `literal: true` that nothing left references a deleted name.
- [ ] **Step 5: Update the three tool-count places**: remove the 8 names from `expected` in `tests/test_server_smoke.py`, `len(names) == 43`; same 8 out of `EXPECTED_TOOLS` in `scripts/smoke_e2e.py`. `tests/test_tool_count_sync.py` reads `_tool_manager._tools`, which holds only static tools: no change.
- [ ] **Step 6: Docs.** `README.md`: replace the ESPHome row of the tool table and the ESPHome gate paragraph with: static tools, "generated from the Device Builder (≥ 1.15) `/api/mcp`, named `esphome_<tool>`", the three gate classes, `esphome_validate` → `esphome_validate_config` and `esphome_job_status` → `esphome_get_job` renames, `update_config` now needs `expected`; add one line that a standalone ESPHome install with a password can also be connected to directly (`claude mcp add --transport http device-builder http://<host>:6052/api/mcp`). `CLAUDE.md`: update the `esphome.py` description and add `esphome_proxy.py`.
- [ ] **Step 7: Run** `.venv/bin/pytest -q` — expected: all PASS.
- [ ] **Step 8: Commit**

```bash
git add -A src tests scripts README.md CLAUDE.md
git commit -m "feat(esphome)!: generate esphome_* tools from the Device Builder MCP server

BREAKING CHANGE: esphome_list_devices, _get_config, _update_config, _validate, _compile,
_install, _job_status and _cancel_job are replaced by tools generated from the Device
Builder's /api/mcp (Device Builder >= 1.15); parameters follow upstream."
```

---

### Task 4: Live check against the real add-on

**Files:** none changed (fix-ups, if any, go in a new commit).

- [ ] **Step 1: List tools over stdio** with the real `.env`: run a short script using the `mcp` client (`stdio_client` on `.venv/bin/smarthome-builder-mcp`) that calls `tools/list`. Expected: 43 static + 21 `esphome_*` generated tools (64 in total); `esphome_install` schema has `confirm`.
- [ ] **Step 2: Read-only calls**: `esphome_list_devices` → device rows; `esphome_get_config` on one device → YAML text; `esphome_get_config` on `secrets.yaml` → `{"error": ...}` blocked; `esphome_install` without confirm → preview, nothing flashed.
- [ ] **Step 3: Report** the outputs in the task report. Do not call any write tool with `confirm=true` against the real add-on.
