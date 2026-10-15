# node-red-homeassistant-mcp

> [!CAUTION]
> **Early alpha — largely untested. Use entirely at your own risk.**
>
> This software was **"vibe-coded"** (written by an AI agent under human supervision) and has
> **not been validated in production**. It can **modify and deploy your Node-RED flows** and
> **act on your physical Home Assistant setup** — including unlocking locks, disarming alarms,
> and controlling devices. It may misbehave, lose data, or take unintended real-world actions.
>
> It is provided **"AS IS", WITHOUT WARRANTY OF ANY KIND**. The authors accept **no liability
> for any damage** whatsoever (see [`LICENSE`](LICENSE)). Back up your flows, review the code,
> and do not point it at anything you cannot afford to break.

MCP server for **Node-RED** with **Home Assistant** awareness and **safe, backup-first deploy**.

It lets an AI assistant (Claude, or any MCP client) **read, author, and deploy Node-RED flows**
through Node-RED's Admin API — without breaking a working installation:

- **Backup-first** — every change snapshots the current flows to disk *before* writing.
- **Optimistic locking** — deploys use the correct `GET → POST /flows` pattern, so a concurrent
  edit is detected (never silently overwritten) and tab order is preserved.
- **Preview, then confirm** — by default mutating tools return a diff *preview* and write nothing
  until you call them again with `confirm=true`.
- **Home-Assistant-aware** — recognizes `node-red-contrib-home-assistant-websocket` nodes and
  lists their entity/service references.

Based on [nr-mcp](https://github.com/Texan-NXTassist/nr-mcp) (MIT). See `NOTICE`.

## Status & disclaimer

See the **[caution at the top](#node-red-homeassistant-mcp)** — this is early-alpha, vibe-coded
software provided with **no warranty and no liability** (`LICENSE`), and you run it at your own risk.

For transparency on what "untested" means here: the code was written following a test-driven plan
and ships with a `pytest` suite (mocked Admin API) plus an end-to-end smoke test against a
throwaway Node-RED. That is **not** the same as being battle-tested — it has **not** been validated
across real-world setups or in production, edge cases are largely unexplored, and breaking changes
should be expected. Keep backups of your flows and review the code before pointing it at anything
you care about.

## Tools

51 MCP tools, grouped:

| Group | Tools |
|---|---|
| **Inspect** (read-only) | `nr_get_flow_summary` (tabs + node counts), `nr_get_flow` (one tab; optional `fields` filter), `nr_export_flow` (one tab as an import-ready flat array), `nr_get_subflow` (definition, ports→internal nodes, instances), `nr_search_nodes`, `nr_get_function_code`, `nr_get_node_config` (+ up/downstream + group), `nr_get_flow_context`, `nr_get_global_context`, `nr_capture_debug` (live debug/status off the `/comms` WS), `nr_get_runtime_events` (deploy/runtime/system events), `nr_validate_flow` (structure check, no deploy), `nr_diagnostics` (read-only health snapshot) |
| **Author & deploy** (confirm-gated, backup-first) | `nr_safe_deploy` (patch one node), `nr_safe_deploy_many` (atomic multi-node), `nr_add_wire` (idempotent), `nr_create_nodes`, `nr_delete_nodes` |
| **Subflows** (confirm-gated, backup-first) | `nr_create_subflow` (assemble a definition from scratch), `nr_instantiate_subflow` (place an instance on a tab), `nr_add_subflow_port` / `nr_remove_subflow_port` (synced to the definition + every instance), `nr_delete_subflow` |
| **Backups** | `nr_backup_flow`, `nr_list_backups`, `nr_restore_backup`, `nr_diff` (backup vs current, or backup vs backup) |
| **Modules & testing** | `nr_get_installed_modules`, `nr_install_module`, `nr_inject` |
| **Home Assistant** (read) | `ha_search_entities` (live entity lookup by id/name), `ha_get_state` (current state + attributes), `ha_get_services` (discover domains/services for authoring), `ha_get_entity_history`, `ha_get_entity_statistics`, `ha_list_ha_nodes`, `ha_get_server_config`, `ha_check_nodered_integration` |
| **Home Assistant** (act) | `ha_call_service` — call any HA service (turn on/off, press buttons, nudge covers, set helpers) with a tiered safety gate (see below); `ha_ws_command` — send a raw HA WebSocket command (e.g. entity-registry edits); reads run immediately, writes need `confirm=true`; a `call_service` command obeys the shared service floor |
| **ESPHome** (Device Builder add-on via HA ingress; needs `HA_URL` + `HA_TOKEN`) | `esphome_list_devices`, `esphome_get_config`, `esphome_validate`, `esphome_logs` (read); `esphome_update_config` and `esphome_restore_backup` (diff preview → `confirm=true` per `DEPLOY_MODE`, local backup first, sha256 conflict check); `esphome_compile` (no confirm; cancels queued/running compile/install jobs of the same configuration, including an earlier install's upload), `esphome_install` (**always** `confirm=true`, also in `auto`; offline device → deferred flash), `esphome_job_status`, `esphome_cancel_job`, `esphome_list_backups`. `secrets.yaml` is blocked unless `ESPHOME_ALLOW_SECRETS=true` |

## How the safety model works

Every mutating tool runs through one gate:

1. Fetch the current flows + revision.
2. Compute the proposed flows in memory and **diff** them → a human-readable preview.
3. If `DEPLOY_MODE=propose` and you did **not** pass `confirm=true` → return the preview and stop
   (nothing is written).
4. Otherwise → **snapshot a backup**, then `POST` with optimistic locking. On a revision conflict
   it re-reads, recomputes against the fresh state, and retries once.

So in the default `propose` mode the loop is: *call a tool → read the preview → call again with
`confirm=true` to apply*. A backup is always written before any real change.

**Calling Home Assistant services (`ha_call_service`)** has its own tiered gate, independent of the
deploy gate above (HA service calls have no undo, so they cannot be backed up):

- A small **catastrophic floor** (e.g. `homeassistant.restart`, `hassio.host_reboot`,
  `recorder.purge`) is **always refused** — `confirm=true` cannot override it. Extend it, never shrink
  it, with `HA_BLOCK_SERVICES`.
- **Sensitive** services (default: `lock.unlock`/`open`, `alarm_control_panel.alarm_disarm`/
  `alarm_trigger`, `update.install`) return a dry-run **preview** unless you pass `confirm=true`.
  Tune the set with `HA_CONFIRM_SERVICES`.
- Everything else runs directly. The effective policy is reported by `nr_diagnostics`. Note: this
  gates services *at the call boundary* — a `script`/`automation` can still indirectly reach a gated
  service, so it is a guardrail against direct mistakes, not a sandbox.

## Configuration

All settings come from environment variables (or a `.env` file in the working directory).
Copy `.env.example` to `.env` and edit it. Every variable is optional; defaults shown.

| Variable | Default | Purpose |
|---|---|---|
| `NR_URL` | `http://localhost:1880` | Node-RED base URL |
| `NR_TOKEN` | — | Bearer token for the Admin API |
| `NR_USER` / `NR_PASS` | — | Node-RED login credentials (HA add-on: your HA username/password) |
| `NR_AUTH` | `basic` | How `NR_USER`/`NR_PASS` are used: `basic` — HTTP Basic on every request (correct for the **HA Node-RED add-on**, where nginx handles auth before Node-RED sees the request); `token` — exchange for a bearer via `POST /auth/token` (vanilla Node-RED with `adminAuth`) |
| `NR_VERIFY_SSL` | `1` | `0` to skip TLS verification (self-signed certs) |
| `HA_URL` | — | Home Assistant base URL (`:8123`) — **distinct from `NR_URL` (`:1880`)**. Enables `ha_search_entities`/`ha_get_state` for entity discovery during authoring |
| `HA_TOKEN` | — | Long-lived HA access token (Profile → Long-Lived Access Tokens) |
| `HA_BLOCK_SERVICES` | — | Comma-separated `domain.service` / `domain.*` entries that `ha_call_service` will always refuse, in addition to the built-in catastrophic floor (homeassistant.stop, homeassistant.restart, hassio.host_reboot, hassio.host_shutdown, hassio.supervisor_restart, hassio.restore_full, hassio.restore_partial, recorder.purge, recorder.purge_entities). Optional; default empty. |
| `HA_CONFIRM_SERVICES` | default set | Comma-separated `domain.service` / `domain.*` entries that require `confirm=true`; without it the tool returns a dry-run preview. Unset = built-in default set (lock.unlock, lock.open, alarm_control_panel.alarm_disarm, alarm_control_panel.alarm_trigger, update.install). Set to empty string to gate nothing. |
| `HA_WS_BLOCK_COMMANDS` | — | Comma-separated WebSocket command types that `ha_ws_command` will always refuse (additive to the built-in floor; `confirm=true` cannot override). |
| `HA_WS_READ_COMMANDS` | — | Comma-separated WebSocket command types that `ha_ws_command` treats as read-only (additive to the built-in defaults), so they run immediately without `confirm=true`. |
| `ESPHOME_ADDON_SLUG` | `5c53de3b_esphome` | Slug of the ESPHome Device Builder add-on used by the `esphome_*` tools (reached through HA ingress with `HA_URL` + `HA_TOKEN`; the token must belong to an HA admin user if the add-on panel is admin-only) |
| `ESPHOME_ALLOW_SECRETS` | `false` | `true` lets `esphome_get_config` / `esphome_update_config` / `esphome_restore_backup` touch `secrets.yaml` / `secrets.yml` (their values then pass through the conversation). The block on direct reads/writes of `secrets.yaml` is not a security boundary: a device YAML can `!include secrets.yaml`, and `esphome_validate` output then shows the values |
| `DEPLOY_MODE` | `propose` | `propose` (preview, write on `confirm=true`) or `auto` |
| `BACKUP_DIR` | `./backups` | Where pre-deploy snapshots are written |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` (streamable-HTTP) |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `0.0.0.0` / `8080` | Bind address/port in `http` mode |

Auth precedence: `NR_TOKEN` → `NR_USER`/`NR_PASS` (via `NR_AUTH` mode: `basic` or `token`) → none.

## Install & run

### A. Local, stdio (Claude Code on the same machine)

Until it's published to PyPI, install from source into a virtualenv:

```bash
git clone <this-repo> node-red-homeassistant-mcp && cd node-red-homeassistant-mcp
uv venv --python 3.12 .venv && uv pip install -e .
# (or without uv:  python3 -m venv .venv && .venv/bin/pip install -e .)
```

Register it with Claude Code. The launch working directory differs from the project, so pass the
config as `-e KEY=VALUE` (not via `.env`):

```bash
claude mcp add nrha -s user \
  -e NR_URL=http://localhost:1880 \
  -e DEPLOY_MODE=propose \
  -- /ABSOLUTE/PATH/TO/.venv/bin/node-red-homeassistant-mcp
```

Then open a **new** Claude Code session (MCP servers load at startup) and ask it to use the tools,
e.g. *"use nrha to summarise my Node-RED flows."*

### B. Docker / Portainer (HTTP service)

Run it as a long-lived HTTP service (streamable-HTTP at `/mcp`).

**Quickest — local, reads your `.env`:**

```bash
cp .env.example .env            # then edit if you like (e.g. MCP_HTTP_PORT, DEPLOY_MODE)
docker compose up -d --build    # starts the MCP service + a throwaway Node-RED
# ... connect (below), then:  docker compose down -v
```

This serves MCP at `http://localhost:${MCP_HTTP_PORT}/mcp` (default `8080`). The root
`docker-compose.yml` feeds your `.env` into the container via `env_file`, while **overriding the
container invariants `.env` can't get right**: `MCP_HTTP_HOST=0.0.0.0` (must bind all interfaces
inside the container — `127.0.0.1` would be unreachable), `NR_URL=http://nodered:1880` (reach
Node-RED by service name, not `localhost`), and `BACKUP_DIR=/data/backups` (persist on the
volume). In Compose, `environment:` wins over `env_file`, which is how those overrides work.

**For Portainer:**

1. **Build the image** on your Docker host:
   ```bash
   docker build -t node-red-homeassistant-mcp:local .
   ```
2. **Deploy the stack** in Portainer: *Stacks → Add stack → Web editor*, paste
   `deploy/portainer-stack.yml`, **Deploy the stack**. Set values via the stack's
   **Environment variables** UI (a host-side `.env` file isn't available there). (Edit `NR_URL`
   to point at your own Node-RED, or keep the bundled throwaway one.)
3. **Connect your MCP client** to the HTTP endpoint:
   ```bash
   claude mcp add --transport http nrha http://<DOCKER_HOST_IP>:8080/mcp
   ```

The container reaches Node-RED at `NR_URL` — use the Docker service name on a shared network
(`http://nodered:1880`) or your Home Assistant Node-RED's LAN URL + auth. Backups are written to
the `/data` volume.

## Manual verification (end-to-end smoke test)

A self-contained smoke script exercises the server over the real stdio transport against a
disposable Node-RED instance. Docker is the only extra requirement.

```bash
# 1. Start just the throwaway Node-RED (the `nodered` service of docker-compose.yml)
docker compose up -d nodered

# 2. Wait ~10s for Node-RED to be ready, then run the smoke script
NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py

# 3. Tear down (removes the volume too)
docker compose down -v
```

The script:
- Lists the registered MCP tools and asserts every expected name (23) is present.
- Calls `nr_get_flow_summary` and checks the response structure.
- Seeds a tab + inject node via the Admin API if Node-RED is empty.
- Calls `nr_safe_deploy` in propose mode (`confirm=false`) and asserts `deployed:false` plus a diff preview.
- Calls `nr_safe_deploy` with `confirm=true` and asserts `deployed:true` plus a backup file on disk.

## Development

```bash
uv pip install -e ".[dev]"
.venv/bin/pytest -q          # unit tests (mocked Admin API, no network)
```

## Credits & license

MIT (see [`LICENSE`](LICENSE)). Based on [nr-mcp](https://github.com/Texan-NXTassist/nr-mcp) by
NXTAssist.ai (incl. its open PR #3); see [`NOTICE`](NOTICE) and
[`THIRD-PARTY-NOTICES.txt`](THIRD-PARTY-NOTICES.txt) for attribution and the upstream MIT license.
