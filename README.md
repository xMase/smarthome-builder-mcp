# node-red-homeassistant-mcp

MCP server for **Node-RED** with **Home Assistant** awareness and **safe, backup-first deploy**.

Lets an AI assistant read, author and deploy Node-RED flows through the Admin API without
breaking a working install: every change is backed up first, deploys use optimistic locking
(never reordering tabs), and by default changes are *previewed* and only applied on confirmation.

Based on [nr-mcp](https://github.com/Texan-NXTassist/nr-mcp) (MIT). See `NOTICE`.

## Quick start (stdio)

```bash
uv tool install node-red-homeassistant-mcp
NR_URL=http://YOUR_NODE_RED:1880 NR_USER=admin NR_PASS=... node-red-homeassistant-mcp
```

Configure your MCP client to launch the command with the env vars above.

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
- Lists all 19 registered MCP tools and asserts every expected name is present.
- Calls `nr_get_flow_summary` and checks the response structure.
- Seeds a tab + inject node via the Admin API if Node-RED is empty.
- Calls `nr_safe_deploy` in propose mode (`confirm=false`) and asserts `deployed:false` plus a diff preview.
- Calls `nr_safe_deploy` with `confirm=true` and asserts `deployed:true` plus a backup file on disk.
