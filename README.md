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
