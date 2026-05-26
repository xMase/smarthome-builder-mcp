# node-red-homeassistant-mcp

MCP server for **Node-RED** with **Home Assistant** awareness and **safe, backup-first deploy**.

Lets an AI assistant read, author and deploy Node-RED flows through the Admin API without
breaking a working install: every change is backed up first, deploys use optimistic locking
(never reordering tabs), and by default changes are *previewed* and only applied on confirmation.

Based on [nr-mcp](https://github.com/Texan-NXTassist/nr-mcp) (MIT). See `NOTICE`.

## Status & disclaimer

**Early alpha.** This project was **entirely "vibe-coded"** — written by an AI agent (Claude)
under human supervision, following a test-driven plan — and is provided **as-is, with no
warranties of any kind** (see `LICENSE`). It talks to your Node-RED Admin API and can modify and
deploy flows; review the code and keep a backup of your flows before pointing it at anything you
care about. Use at your own risk.

## Quick start (stdio)

```bash
uv tool install node-red-homeassistant-mcp
NR_URL=http://YOUR_NODE_RED:1880 NR_USER=admin NR_PASS=... node-red-homeassistant-mcp
```

Configure your MCP client to launch the command with the env vars above.

## Deploy on Docker / Portainer (HTTP)

Run it as a long-lived HTTP service (streamable-HTTP at `/mcp`).

**1. Build the image on your Docker host:**

```bash
docker build -t node-red-homeassistant-mcp:local .
```

**2. Deploy the stack in Portainer:** *Stacks → Add stack → Web editor*, paste
`deploy/portainer-stack.yml`, and **Deploy the stack**. (Edit `NR_URL` to point at your own
Node-RED, or keep the bundled throwaway one.)

**3. Connect your MCP client** to the HTTP endpoint. For Claude Code:

```bash
claude mcp add --transport http home-node-red http://<DOCKER_HOST_IP>:8080/mcp
```

Notes:
- The container talks to Node-RED at `NR_URL`. On the same Docker network use the service name
  (`http://nodered:1880`); for an external/Home Assistant Node-RED use its LAN URL + auth
  (`NR_TOKEN` or `NR_USER`/`NR_PASS`).
- `DEPLOY_MODE=propose` (default) previews changes; call a mutating tool again with
  `confirm=true` to apply. Backups are written under the `/data` volume.

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
