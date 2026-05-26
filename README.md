# node-red-homeassistant-mcp

MCP server for **Node-RED** with **Home Assistant** awareness and **safe, backup-first deploy**.

It lets an AI assistant (Claude, or any MCP client) **read, author, and deploy Node-RED flows**
through Node-RED's Admin API — without breaking a working installation:

- **Backup-first** — every change snapshots the current flows to disk *before* writing.
- **Optimistic locking** — deploys use the correct `GET → POST /flows` pattern, so a concurrent
  edit is detected (never silently overwritten) and tab order is preserved.
- **Preview, then confirm** — by default mutating tools return a diff *preview* and write nothing
  until you call them again with `confirm=true`.
- **Home-Assistant-aware** — recognizes `node-red-contrib-home-assistant-websocket` nodes and can
  validate their entity/service references against a live Home Assistant.

Based on [nr-mcp](https://github.com/Texan-NXTassist/nr-mcp) (MIT). See `NOTICE`.

## Status & disclaimer

**Early alpha.** This project was **entirely "vibe-coded"** — written by an AI agent (Claude)
under human supervision, following a test-driven plan — and is provided **as-is, with no
warranties of any kind** (see `LICENSE`). It talks to your Node-RED Admin API and can modify and
deploy flows; review the code and keep a backup of your flows before pointing it at anything you
care about. Use at your own risk.

## Tools

22 MCP tools, grouped:

| Group | Tools |
|---|---|
| **Inspect** (read-only) | `nr_get_flow_summary`, `nr_get_flow` (optional `fields` filter), `nr_search_nodes`, `nr_get_function_code`, `nr_get_node_config`, `nr_get_flow_context`, `nr_get_global_context`, `nr_get_debug_output` |
| **Author & deploy** (confirm-gated, backup-first) | `nr_safe_deploy` (patch one node), `nr_safe_deploy_many` (atomic multi-node), `nr_add_wire` (idempotent), `nr_create_nodes`, `nr_delete_nodes` |
| **Backups** | `nr_backup_flow`, `nr_list_backups`, `nr_restore_backup` |
| **Modules & testing** | `nr_get_installed_modules`, `nr_install_module`, `nr_inject` |
| **Home Assistant** | `ha_list_ha_nodes`, `ha_get_server_config`, `ha_validate_calls` |

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

## Configuration

All settings come from environment variables (or a `.env` file in the working directory).
Copy `.env.example` to `.env` and edit it. Every variable is optional; defaults shown.

| Variable | Default | Purpose |
|---|---|---|
| `NR_URL` | `http://localhost:1880` | Node-RED base URL |
| `NR_TOKEN` | — | Bearer token for the Admin API |
| `NR_USER` / `NR_PASS` | — | Basic-auth (used only if no token) |
| `NR_VERIFY_SSL` | `1` | `0` to skip TLS verification (self-signed certs) |
| `DEPLOY_MODE` | `propose` | `propose` (preview, write on `confirm=true`) or `auto` |
| `BACKUP_DIR` | `./backups` | Where pre-deploy snapshots are written |
| `HA_URL` / `HA_TOKEN` | — | Optional; enables `ha_validate_calls` |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` (streamable-HTTP) |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `0.0.0.0` / `8080` | Bind address/port in `http` mode |

Auth precedence: `NR_TOKEN` → `NR_USER`/`NR_PASS` → none.

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
# 1. Start a throwaway Node-RED
docker compose -f docker-compose.dev.yml up -d

# 2. Wait ~10s for Node-RED to be ready, then run the smoke script
NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py

# 3. Tear down (removes the volume too)
docker compose -f docker-compose.dev.yml down -v
```

The script:
- Lists the registered MCP tools and asserts every expected name (22) is present.
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

MIT. Based on [nr-mcp](https://github.com/Texan-NXTassist/nr-mcp) by NXTAssist.ai (incl. its open
PR #3); see `NOTICE` for attribution.
