# Design: ESPHome tools through the Device Builder dashboard via HA ingress (`esphome_*`)

Date: 2026-10-14

**Status:** approved 2026-10-14 (design in chat, spec reviewed by the user). Branch `feat/esphome-ingress`
(from `main`). Supersedes the parked SSH route (`2026-10-14-ha-ssh-access-design.md`).

## Problem

nrha cannot read or edit ESPHome device YAML, validate it, read device logs or flash a device. On 2026-10-14
editing `/homeassistant/esphome/onju-voice-87864c.yaml` needed a temporary Node-RED exec tab. The SSH route
(branch `feat/ha-ssh-access`) works but needs an extra add-on, a key, a host-side gate and manual setup.

## Decisions taken (not reopened)

- Access goes through the **ESPHome Device Builder** add-on (`5c53de3b_esphome`) over **HA ingress**, using only
  the existing `HA_URL` + `HA_TOKEN`. No SSH, no extra add-on.
- Scope: list, read, write (preview + confirm), validate, logs, **compile and OTA flash**, job status/cancel,
  local backups with restore.
- `secrets.yaml` / `secrets.yml` are **blocked** for read and write; `ESPHOME_ALLOW_SECRETS=true` unblocks them.
- OTA flash **always** needs `confirm=true`, also with `DEPLOY_MODE=auto`. Compile alone needs no confirm.
- Long jobs are **asynchronous**: compile/install return job ids at once; `esphome_job_status` can wait.
- Backups are **nrha-local** (not only Device Builder's version history), with list/restore tools.
- `ha_ws_command` is unchanged (treating `supervisor/api` GET as a read was proposed and left out).

## Facts this design relies on

Verified on the user's HA on 2026-10-14 (`supervisor/api GET /addons/5c53de3b_esphome/info`): add-on version
**2026.9.1** (= `version_latest`), `ingress: true`, `ingress_url` `/api/hassio_ingress/<token>/`,
`host_network: true`, dynamic `ingress_port` 62431, direct port 6052 disabled. `panel_admin` is not in that
reply (unknown).

From the source (research notes: `.superpowers/sdd/2026-10-14-esphome-ingress/research-device-builder.md`,
local, gitignored):

- ESPHome 2026.9.1 pins **`esphome-device-builder==1.14.9`** (esphome `docker/Dockerfile:25`). 1.14.9 has **no
  `/api/mcp`** and its `devices/update_config` has **no `expected`** argument (both appear by 1.17.3b1).
  Unknown args are swallowed by every handler (`**kwargs`), so sending `expected` is harmless on 1.14.9 and
  enforced on newer versions.
- WebSocket `GET /ws`. First frame from the server: `ServerInfoMessage` (`server_version`, `esphome_version`,
  `ha_ingress`, `requires_auth`; no `message_id`). Client frame: `{"command", "message_id": str, "args": {}}`.
  Replies: result `{"message_id", "result"}`, event `{"message_id", "event", "data"}`, error
  `{"message_id", "error_code", "details"}`. Commands run concurrently per socket.
- Error codes: `invalid_message`, `not_authenticated`, `unknown_command`, `invalid_args`, `not_found`,
  `precondition_failed`, `unavailable`, `rate_limited`, `already_exists`, `internal_error`.
- Streams (`devices/validate`, `devices/logs`, `firmware/follow_job`): `event:"output"` with one line per frame,
  then `event:"result"` — validate/logs `{success, code}`, follow_job `{status, exit_code, error,
  queued_update_armed}` — then a plain `result: null`. `devices/stop_stream {stream_id: <message_id>}` →
  `{cancelled}`; a stopped stream gets no terminal frame. Closing the socket kills validate/logs, not firmware jobs.
- Commands used: `devices/list` (→ `{configured: [Device], importable}`, `Device.runtime_state` nested:
  `state` online|offline|unknown, `ip_addresses`, `deployed_version`, `queued_update`; `Device.address`,
  `current_version`, `update_available`, `has_pending_changes`), `devices/get_config {configuration}` (→ YAML
  string), `devices/update_config {configuration, content, allow_wipe?}` (atomic, creates a missing file,
  refuses an empty device YAML), `devices/validate {configuration, show_secrets=false}`,
  `devices/logs {configuration, port?, no_states?}` (no timeout; streams until stopped),
  `firmware/compile {configuration}`, `firmware/install {configuration, port="OTA"}` (returns the compile job; a
  dependent upload job has `depends_on == compile.job_id`), `firmware/get_jobs {configuration?, status?}`,
  `firmware/get_job {job_id}`, `firmware/follow_job {job_id}` (replays buffered output, ≤2000 lines),
  `firmware/cancel {job_id}` (unknown → `not_found`, terminal → `invalid_args`).
- Firmware jobs run server-side, persisted, decoupled from the socket. A new compile/install of the same
  configuration supersedes earlier ones. `firmware/install` with port `OTA` on an **offline** device queues a
  **deferred install**: the device is flashed automatically when it comes back online.
- Ingress trust: the ingress site has no auth and accepts only peers 127.0.0.1/::1/172.30.32.2 (Supervisor); no
  Origin check on the trusted site, so a Python client without `Origin` passes. Path confinement
  (`resolve_under_root`) is server-side; the bare-`*.yaml` name rule exists only in the newer MCP tools.
- HA side: `supervisor/api POST /ingress/session` → `{session}`; HA Core proxies `/api/hassio_ingress/<token>/...`
  (websocket upgrades included) forwarding `Cookie`; Supervisor checks the `ingress_session` cookie, enforces
  admin when the add-on has `panel_admin`. Websocket messages up to 16 MiB; no proxy idle timeout; Device
  Builder heartbeat 30 s.

Unverified until the manual check: `panel_admin` (admin token needed?), cookie end-to-end through the websocket,
exact `devices/list` wire shape, upload job lookup after `firmware/install`, stop_stream terminal behaviour,
session TTL.

## Architecture

New domain module `src/nrha_mcp/esphome.py`. `server.py` declares the tools as thin `_wrap` wrappers, like every
other tool. No new dependency (`websockets`, `httpx` already present).

**Connection, one per tool call** (no session cache — two extra HA round-trips are cheap and keep the code
simple):

1. Open the HA websocket and authenticate with `HA_TOKEN`, reusing `ha_ws.py` (`_default_connect`, `_auth`).
   `ha_ws.py` behaviour does not change.
2. `supervisor/api GET /addons/<ESPHOME_ADDON_SLUG>/info` → check `state == "started"` and `ingress`, take
   `ingress_url`.
3. `supervisor/api POST /ingress/session` → `session`. Close the HA websocket.
4. Open `ws(s)://<HA host><ingress_url>ws` with header `Cookie: ingress_session=<session>`; read the
   `ServerInfoMessage` (keep `server_version`).
5. Run the command(s); close.

The Device Builder connection sits behind an injectable `connect` (same pattern as `ha_ws.py` / `runtime.py`)
so unit tests feed frames without a network. A small request helper sends a frame with a fresh string
`message_id` and reads frames for that id until `result` or `error_code`; a stream helper collects
`event:"output"` lines until `event:"result"`, a deadline, or a line limit, then sends `devices/stop_stream`
when it stopped early.

**Settings** (`config.py`, defaults only; nothing is added to the repo `.env`; `.env.example` documents them):

- `esphome_addon_slug: str = "5c53de3b_esphome"`
- `esphome_allow_secrets: bool = False`

**Configuration names** are checked before any connection: a bare name (no `/`, no leading `.`) ending in
`.yaml` or `.yml`. `secrets.yaml` / `secrets.yml` are refused unless `esphome_allow_secrets`. Device Builder
confines paths itself; nrha's check is the first line, not the only one.

## Tools (11 new)

| Tool | Device Builder command | Gate |
|---|---|---|
| `esphome_list_devices()` | `devices/list` | read |
| `esphome_get_config(configuration)` | `devices/get_config` | read; returns `content` + `sha256` |
| `esphome_update_config(configuration, content, base_sha256=None, confirm=False)` | `devices/update_config` | diff preview → confirm per `DEPLOY_MODE`; backup first |
| `esphome_validate(configuration)` | `devices/validate` | read |
| `esphome_logs(configuration, seconds=10, max_lines=200)` | `devices/logs` + `devices/stop_stream` | read; `seconds` ≤ 60 |
| `esphome_compile(configuration)` | `firmware/compile` | none |
| `esphome_install(configuration, port="OTA", confirm=False)` | `firmware/install` | **always** confirm |
| `esphome_job_status(job_id, wait_seconds=0, tail_lines=50)` | `firmware/get_job`, `firmware/follow_job` | read; `wait_seconds` ≤ 300 |
| `esphome_cancel_job(job_id)` | `firmware/cancel` | none |
| `esphome_list_backups(configuration=None)` | local | read |
| `esphome_restore_backup(backup_id, base_sha256=None, confirm=False)` | as `esphome_update_config` | same write path |

`esphome_list_devices` returns a flat list per configured device: `configuration`, `name`, `state`, `address`,
`ip_addresses`, `current_version`, `deployed_version`, `update_available`, `has_pending_changes`,
`queued_update`.

## Write path (`esphome_update_config`, `esphome_restore_backup`)

1. Read the current file (`devices/get_config`; `not_found` → treated as a new file) and compute its sha256.
2. `base_sha256` given and different from the current sha256 → `NRConflictError` ("re-read and preview
   again"), nothing written.
3. Content equal to the current file → `{"changed": false}`, nothing written.
4. `DEPLOY_MODE=propose` and not `confirm` → return the unified diff, `base_sha256` = current sha256, and
   `requires_confirm`; nothing written. The model passes that `base_sha256` back with `confirm=true`, so a change
   made between preview and confirm becomes a conflict instead of being overwritten.
5. Otherwise: write the local backup of the current file (skipped for a new file), re-read the file, and if its
   sha256 differs from step 1 → `NRConflictError`. Else send `devices/update_config {configuration, content,
   expected: <current content>}` (`expected` enforced only on Device Builder ≥ 1.17; on 1.14.9 the re-read is the
   only check and a small race window remains).
6. Return `{"changed": true, "backup_id", "sha256": <new>}`.

**Backups:** `BACKUP_DIR/esphome/<configuration>.<UTC timestamp>.yaml` (text, the file as it was). `backup_id` is
the file name. No pruning (same as the flow `BackupStore`). `esphome_list_backups` lists newest first, optionally
filtered by configuration. `esphome_restore_backup` reads the backup and runs the write path with the backup's
configuration and content. `backup_id` must be a bare name inside `BACKUP_DIR/esphome/`.

## Flash path (`esphome_install`)

- Without `confirm` (in every `DEPLOY_MODE`): preview from `devices/list` — device `state`, `address`,
  `ip_addresses`, `current_version` → `deployed_version`, `has_pending_changes`, `port`. If `state` is
  `offline`, the preview warns that the install becomes **deferred** (automatic flash when the device comes
  online). An unknown configuration → `NRNotFoundError`.
- With `confirm=true`: `firmware/install {configuration, port}` → compile job; find the upload job with
  `firmware/get_jobs {configuration}` by `depends_on`. Return both job ids, statuses and `is_deferred_install`.
- `esphome_compile` returns the compile job at once.

`esphome_job_status`: `firmware/get_job` for the job fields, then `firmware/follow_job` for the output (the
buffered replay comes first; a terminal job ends with `result` by itself). Follow until the terminal `result`
or a deadline of `max(wait_seconds, 2)` seconds, then `stop_stream` (the 2 s floor gives the replay time to
arrive; follow_job has no end-of-replay marker). Returns `status`, `exit_code`, `error`, `progress`, the last
`tail_lines` output lines, and `done`.

## Validate and logs

- `esphome_validate`: stream until `event:"result"`; return `success`, `code`, the last 200 lines. nrha never
  sends `show_secrets`.
- `esphome_logs`: collect lines for `seconds` (≤ 60) or `max_lines`, then `devices/stop_stream`. ESPHome's own
  connection errors come back as output lines, not as an nrha error.

## Error handling

All failures are `NRError` subclasses, mapped by `_wrap`.

| Case | Raised | Message |
|---|---|---|
| `HA_URL` / `HA_TOKEN` unset | `_err` in the tool | same wording as other `ha_*` tools |
| Add-on not found, not started, or no ingress | `NRNotFoundError` | slug and state |
| Ingress 401/403 | `NRAuthError` | the HA token must belong to an admin user |
| Device Builder `error_code` | `NRError`; `not_found` → `NRNotFoundError`, `precondition_failed` → `NRConflictError` | code + `details` |
| sha256 conflict on write | `NRConflictError` | re-read and preview again |
| Bad configuration name / secrets blocked / bad `backup_id` | `NRError`, before connecting | the rule broken |
| Connect or reply timeout | `NRError` | which step did not answer |

No new `nr_diagnostics` block: the first failing tool already says why.

## Testing

- Unit tests with a fake HA websocket and a fake Device Builder websocket (frame scripts), no network, in the
  style of the `ha_ws.py` tests. Cases: name rule; secrets blocked and the flag; write preview, no-change,
  `base_sha256` conflict, re-read conflict, backup written, `expected` sent; restore through the write path;
  install preview in `auto` mode still needs confirm; offline → deferred warning; upload job found by
  `depends_on`; job wait until result and until deadline (stop_stream sent); logs end with stop_stream;
  error-code mapping; add-on not started; ingress 403.
- Tool count: update the three places guarded by `tests/test_tool_count_sync.py`.
- Manual check on the user's HA, each step with the user's ok: list devices → get `onju-voice-87864c.yaml` →
  validate → logs. Write and flash only if the user asks.

## Out of scope

Device Builder's MCP endpoint (absent on 1.14.9), version-history tools, create/rename/delete devices, secrets
editing tools, `nr_diagnostics` block, session caching, `ha_ws_command` read classification.
