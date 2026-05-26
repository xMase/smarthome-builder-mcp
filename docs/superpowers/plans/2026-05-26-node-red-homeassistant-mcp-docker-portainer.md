# node-red-homeassistant-mcp — Plan B: Docker / HTTP transport / Portainer

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Make the server deployable as a long-running container (for Portainer) by finishing the **streamable-HTTP** transport, adding a **Dockerfile**, and providing a **Portainer stack** + connection instructions.

**Architecture:** The existing `nrha_mcp` package is transport-agnostic; `server.main()` already branches on `MCP_TRANSPORT`. We (1) construct the FastMCP app with host/port from `Settings` and run `transport="streamable-http"` when `MCP_TRANSPORT=http`, (2) package it in a slim image, (3) ship a compose stack (MCP service + optional throwaway Node-RED on a shared network). Claude Code connects over `--transport http` to `http://HOST:8080/mcp`.

**Tech Stack:** mcp 1.27.1 FastMCP (streamable-http at `/mcp`), Docker (python:3.12-slim), Portainer stacks.

**Verified API (mcp 1.27.1):** `FastMCP(name, *, host="127.0.0.1", port=8000, streamable_http_path="/mcp", json_response=False, stateless_http=False, ...)`; `mcp.run(transport="stdio"|"sse"|"streamable-http")`. `mcp.settings.host/.port` reflect the constructor values.

---

## File Structure

```
Dockerfile                         [NEW]
.dockerignore                      [NEW]
deploy/portainer-stack.yml         [NEW]   # Portainer "web editor" stack
src/nrha_mcp/server.py             [MODIFY] # host/port + streamable-http
tests/test_server_transport.py     [NEW]
README.md                          [MODIFY] # "Deploy on Docker / Portainer" section
```

---

## Task B1: Finish the HTTP transport in `server.py`

**Files:**
- Modify: `src/nrha_mcp/server.py`
- Test: `tests/test_server_transport.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_transport.py
import importlib


def _fresh_server(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import nrha_mcp.server as srv
    return importlib.reload(srv)


def test_http_mode_binds_and_selects_streamable(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="http",
                        MCP_HTTP_HOST="0.0.0.0", MCP_HTTP_PORT="8080")
    # FastMCP picked up host/port from Settings
    assert srv.mcp.settings.host == "0.0.0.0"
    assert srv.mcp.settings.port == 8080
    # main() runs the streamable-http transport
    captured = {}
    monkeypatch.setattr(srv.mcp, "run", lambda transport: captured.setdefault("t", transport))
    srv.main()
    assert captured["t"] == "streamable-http"


def test_stdio_is_default(monkeypatch):
    srv = _fresh_server(monkeypatch, MCP_TRANSPORT="stdio")
    captured = {}
    monkeypatch.setattr(srv.mcp, "run", lambda transport: captured.setdefault("t", transport))
    srv.main()
    assert captured["t"] == "stdio"
```

> Note: these reload `nrha_mcp.server` so the module-level `Settings()`/`FastMCP(...)` pick up the
> monkeypatched env. Reloading re-instantiates `client`/`store` (no network at construction) and
> re-creates `./backups` — harmless. If `importlib.reload` causes duplicate-tool warnings, that is
> cosmetic; the assertions are what matter.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_server_transport.py -v`
Expected: FAIL — currently `mcp` is built without host/port (defaults 127.0.0.1:8000) and `main()` maps http→"sse".

- [ ] **Step 3: Edit `src/nrha_mcp/server.py`**

Change the FastMCP construction to pass host/port and enable stateless HTTP:

```python
mcp = FastMCP(
    "node-red-homeassistant-mcp",
    json_response=True,
    host=settings.mcp_http_host,
    port=settings.mcp_http_port,
    stateless_http=True,
)
```

Change `main()` to use streamable-http for the `http` mode:

```python
def main():
    """Console-script entry point."""
    transport = "streamable-http" if settings.mcp_transport == "http" else "stdio"
    mcp.run(transport=transport)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_server_transport.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite + commit**

```bash
.venv/bin/pytest -q            # all green (existing + 2 new)
git add src/nrha_mcp/server.py tests/test_server_transport.py
git commit -m "feat(server): streamable-http transport bound to MCP_HTTP_HOST/PORT"
```

---

## Task B2: Dockerfile + .dockerignore

**Files:**
- Create: `Dockerfile`, `.dockerignore`

- [ ] **Step 1: `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Build deps for the package (PEP 517 via hatchling)
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .

# HTTP service defaults (override at runtime / in the stack)
ENV MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8080 \
    DEPLOY_MODE=propose \
    BACKUP_DIR=/data/backups

EXPOSE 8080
VOLUME ["/data"]

CMD ["node-red-homeassistant-mcp"]
```

- [ ] **Step 2: `.dockerignore`**

```
.venv/
__pycache__/
*.pyc
.git/
tests/
docs/
backups/
*.egg-info/
dist/
build/
docker-compose.dev.yml
```

- [ ] **Step 3: Build the image (verify it builds & starts)**

```bash
docker build -t node-red-homeassistant-mcp:local .
# Start it (no Node-RED yet -> it still boots the HTTP server; tools will error on calls, that's fine)
docker run --rm -d --name nrha-test -p 8080:8080 -e NR_URL=http://localhost:1880 node-red-homeassistant-mcp:local
sleep 3
# The streamable-http endpoint should respond (405/406/400 for a bare GET is fine — it means the server is up)
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/mcp || true
docker logs nrha-test | tail -20
docker rm -f nrha-test
```

Expected: the container stays up and `/mcp` returns an HTTP status (not connection-refused).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: Dockerfile (streamable-http service) + .dockerignore"
```

---

## Task B3: Portainer stack + README deploy section

**Files:**
- Create: `deploy/portainer-stack.yml`
- Modify: `README.md`

- [ ] **Step 1: `deploy/portainer-stack.yml`**

A self-contained test stack: the MCP service + an optional throwaway Node-RED on a shared network.
Remove the `nodered` service and point `NR_URL` at your real Node-RED to use your own.

```yaml
# Portainer: Stacks -> Add stack -> Web editor -> paste this -> Deploy.
# The image `node-red-homeassistant-mcp:local` must exist on the Docker host
# (build it there first:  docker build -t node-red-homeassistant-mcp:local .).
services:
  nrha-mcp:
    image: node-red-homeassistant-mcp:local
    environment:
      NR_URL: "http://nodered:1880"   # point at your own Node-RED to skip the test one
      NR_TOKEN: ""
      NR_USER: ""
      NR_PASS: ""
      DEPLOY_MODE: "propose"           # preview-first; set "auto" to deploy without confirm
      MCP_TRANSPORT: "http"
      MCP_HTTP_HOST: "0.0.0.0"
      MCP_HTTP_PORT: "8080"
      BACKUP_DIR: "/data/backups"
    ports:
      - "8080:8080"
    volumes:
      - nrha_backups:/data
    restart: unless-stopped
    networks: [nrha]

  # Optional throwaway Node-RED for testing. Delete this service to use your own.
  nodered:
    image: nodered/node-red:latest
    ports:
      - "1880:1880"
    volumes:
      - nodered_data:/data
    restart: unless-stopped
    networks: [nrha]

networks:
  nrha:

volumes:
  nrha_backups:
  nodered_data:
```

- [ ] **Step 2: Add a "Deploy on Docker / Portainer" section to `README.md`** (after Quick start)

```markdown
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
```

- [ ] **Step 3: Commit**

```bash
git add deploy/portainer-stack.yml README.md
git commit -m "docs: Portainer stack + Docker/HTTP deploy instructions"
```

---

## Task B4 (best-effort): containerized HTTP end-to-end

**Files:** none (verification only)

- [ ] **Step 1: Stand up MCP + Node-RED via the stack locally**

```bash
docker build -t node-red-homeassistant-mcp:local .
docker compose -f deploy/portainer-stack.yml up -d
# wait for node-red
for i in $(seq 1 30); do curl -sf http://localhost:1880/flows >/dev/null && break; sleep 2; done
```

- [ ] **Step 2: Talk to it over streamable-HTTP with the mcp client**

Write a tiny throwaway check (or reuse `scripts/smoke_e2e.py`'s assertions adapted to HTTP) using
`mcp.client.streamable_http.streamablehttp_client("http://localhost:8080/mcp")` →
`ClientSession` → `list_tools()` (assert the 22 names) and `nr_get_flow_summary`.

- [ ] **Step 3: Tear down**

```bash
docker compose -f deploy/portainer-stack.yml down -v
```

- [ ] **Step 4 (if it ran):** record the result. If Docker can't build/run in this environment,
  that's acceptable — the deliverables (Dockerfile, stack, instructions) let the human run it on
  their Portainer host. Report B4 as NOT RUN with the reason rather than fabricating output.

---

## Self-Review (during planning)

- **Coverage:** HTTP transport wiring (B1 + test), image (B2), Portainer deploy + client wiring (B3),
  e2e (B4). Maps to spec §5.6/§10 (Docker target). The HA add-on packaging (§10 target 3) remains a
  later plan.
- **No placeholders:** all code/commands are concrete; the verified FastMCP API backs B1.
- **Consistency:** `MCP_TRANSPORT=http` → `"streamable-http"`; endpoint `/mcp`; port `8080` consistent
  across Dockerfile/stack/README/client command.
- **Risk:** `importlib.reload` in the transport test (cosmetic duplicate-tool warnings only). The
  Node-RED Admin API auth open item is unchanged and surfaces only on real tool calls, not on boot.
