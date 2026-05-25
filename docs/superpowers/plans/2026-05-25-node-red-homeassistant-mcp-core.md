# node-red-homeassistant-mcp — Core v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the core stdio MCP server `node-red-homeassistant-mcp` — a HA-aware, safe-by-default Node-RED Admin API tool derived (MIT) from nr-mcp.

**Architecture:** A small Python package (`nrha_mcp`) layered as: HTTP `NRClient` → `BackupStore` + `deploy` gate (compute → diff → propose/confirm → backup → post) → read-only `flows`, mutating `edit`, `modules`, and HA-aware `ha` tool modules → a FastMCP `server` exposing the tools over stdio. All mutating tools route through the deploy gate; reads are stateless.

**Tech Stack:** Python 3.12 (managed by `uv`), `mcp` (FastMCP) + `httpx` + `pydantic`/`pydantic-settings`; tests with `pytest`, `pytest-asyncio`, `respx`.

**Base sources (already studied, in `$CLAUDE_JOB_DIR/nr-mcp-study` during planning):** nr-mcp `src/nr_mcp/{client,tools,server}.py` (MIT) + open PR #3 (adds `NR_VERIFY_SSL`, `get_global_context`, `safe_deploy_many`, `add_wire`, `fields` on `get_flow`). This plan ports that code into a cleaner module layout and wraps the mutating paths with a backup + propose gate (the project's differentiators).

---

## File Structure

```
node-red-homeassistant-mcp/
├── pyproject.toml                 # package metadata, deps, console script, pytest cfg
├── LICENSE                        # MIT — our copyright + nr-mcp's retained
├── NOTICE                         # attribution to nr-mcp + PR #3 author
├── README.md                      # one-liner + usage (search/AI discoverable)
├── .env.example                   # all env vars documented
├── src/nrha_mcp/
│   ├── __init__.py                # __version__
│   ├── config.py                  # Settings (pydantic-settings)            [NEW]
│   ├── client.py                  # NRClient — Admin API HTTP (ported + PR3)
│   ├── backup.py                  # BackupStore — snapshot/list/restore      [NEW]
│   ├── deploy.py                  # diff_flows + commit gate                 [NEW]
│   ├── flows.py                   # read-only tools (ported from tools.py)
│   ├── edit.py                    # mutating tools, refactored onto the gate
│   ├── modules.py                 # modules + inject (ported)
│   ├── ha.py                      # HA-awareness + optional HA REST client   [NEW]
│   └── server.py                  # FastMCP registration + main()
└── tests/
    ├── conftest.py                # FakeNRClient, sample-flows fixtures
    ├── test_config.py
    ├── test_client.py             # respx HTTP mocking
    ├── test_backup.py
    ├── test_deploy.py
    ├── test_flows.py
    ├── test_edit.py
    ├── test_modules.py
    ├── test_ha.py
    └── test_server_smoke.py
```

Each module has one responsibility. `client.py` knows HTTP only; `deploy.py` knows the
safety gate only; tool modules know flow-shapes only; `server.py` is thin wiring.

---

## Phase 0 — Project scaffold & test infrastructure

### Task 0.1: Toolchain + virtualenv (Python 3.12)

**Files:** none (environment only)

- [ ] **Step 1: Install `uv` (manages Python + venv)**

`uv` is not present on this box (only brew Python 3.14, which is bleeding-edge for `mcp`/`pydantic` wheels). Install `uv` via the already-present `pipx`:

```bash
pipx install uv
uv --version   # expect: uv 0.x.x
```

- [ ] **Step 2: Create a 3.12 virtualenv in the project**

Run from the repo root (`/var/home/xmase/Project/Personal/node-red-homeassistant-mcp`):

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
python --version   # expect: Python 3.12.x
```

Expected: uv downloads CPython 3.12 if missing and creates `.venv`.

### Task 0.2: `pyproject.toml`

**Files:**
- Create: `pyproject.toml`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "node-red-homeassistant-mcp"
version = "0.1.0"
description = "MCP server for Node-RED with Home Assistant awareness and safe, backup-first deploy"
requires-python = ">=3.11"
license = "MIT"
authors = [{ name = "xmase" }]
readme = "README.md"
keywords = ["mcp", "node-red", "home-assistant", "homeassistant", "ai", "claude", "automation", "iot", "model-context-protocol"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Home Automation",
]
dependencies = [
    "mcp>=1.0.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "respx>=0.21"]

[project.scripts]
node-red-homeassistant-mcp = "nrha_mcp.server:main"

[project.urls]
Homepage = "https://github.com/xmase/node-red-homeassistant-mcp"

[tool.hatch.build.targets.wheel]
packages = ["src/nrha_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Install the project (editable) with dev extras**

```bash
uv pip install -e ".[dev]"
python -c "import mcp, httpx, pydantic, respx; print('deps ok')"
```

Expected: `deps ok`. If a wheel fails to build on 3.12, re-run Task 0.1 Step 2 pinning `--python 3.11`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: pyproject for node-red-homeassistant-mcp (py3.12, mcp+httpx+pydantic)"
```

### Task 0.3: Package skeleton, LICENSE, NOTICE, README, .env.example

**Files:**
- Create: `src/nrha_mcp/__init__.py`, `LICENSE`, `NOTICE`, `README.md`, `.env.example`

- [ ] **Step 1: `src/nrha_mcp/__init__.py`**

```python
"""node-red-homeassistant-mcp — HA-aware, safe-by-default MCP server for Node-RED."""

__version__ = "0.1.0"
```

- [ ] **Step 2: `LICENSE`** — MIT, retaining nr-mcp's copyright line plus ours.

```text
MIT License

Copyright (c) 2026 xmase (node-red-homeassistant-mcp)
Portions Copyright (c) NXTAssist.ai (nr-mcp, https://github.com/Texan-NXTassist/nr-mcp)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: `NOTICE`**

```text
node-red-homeassistant-mcp
Copyright (c) 2026 xmase

This project is based on nr-mcp (MIT) by NXTAssist.ai:
  https://github.com/Texan-NXTassist/nr-mcp
and incorporates its open Pull Request #3 (NR_VERIFY_SSL, nr_get_global_context,
nr_safe_deploy_many, nr_add_wire, the `fields` parameter on nr_get_flow).
```

- [ ] **Step 4: `.env.example`**

```bash
# --- Node-RED Admin API ---
NR_URL=http://localhost:1880
# Auth (checked in order: token -> basic -> none)
NR_TOKEN=
NR_USER=
NR_PASS=
# Set to 0/false to skip TLS verification (self-signed Node-RED)
NR_VERIFY_SSL=1

# --- Safety ---
# propose = preview, write only on confirm=true (default). auto = write immediately.
DEPLOY_MODE=propose
BACKUP_DIR=./backups

# --- Home Assistant (optional, enables live validation) ---
HA_URL=
HA_TOKEN=

# --- Transport (Plan A uses stdio) ---
MCP_TRANSPORT=stdio
MCP_HTTP_HOST=0.0.0.0
MCP_HTTP_PORT=8080
```

- [ ] **Step 5: `README.md`** (minimal, discoverable)

```markdown
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
```

- [ ] **Step 6: Commit**

```bash
git add src/nrha_mcp/__init__.py LICENSE NOTICE README.md .env.example
git commit -m "chore: package skeleton, MIT license + nr-mcp attribution, README, env example"
```

### Task 0.4: Test fixtures — `FakeNRClient` and sample flows

**Files:**
- Create: `tests/conftest.py`

This in-memory fake stands in for `NRClient` in all tool/deploy/ha tests (no HTTP). It mimics
the real contract: `get_flows()` returns `(flows, rev)`; `post_flows(flows, rev)` raises
`NRConflictError` on a stale rev and bumps the rev otherwise.

- [ ] **Step 1: Write `tests/conftest.py`**

```python
"""Shared test fixtures: an in-memory fake Node-RED client and sample flows."""

import copy
import pytest

from nrha_mcp.client import NRConflictError, NRNotFoundError, NRError
from nrha_mcp.config import Settings


SAMPLE_FLOWS = [
    {"id": "tab1", "type": "tab", "label": "Heating"},
    {"id": "n1", "type": "inject", "z": "tab1", "name": "tick", "wires": [["n2"]]},
    {"id": "n2", "type": "function", "z": "tab1", "name": "calc", "func": "return msg;", "wires": [["n3"]]},
    {"id": "n3", "type": "api-call-service", "z": "tab1", "name": "boiler on",
     "domain": "switch", "service": "turn_on", "entityId": "switch.boiler", "wires": [[]]},
    {"id": "g1", "type": "group", "z": "tab1", "name": "grp", "nodes": ["n2", "n3"]},
    {"id": "srv1", "type": "server", "z": "", "name": "Home Assistant"},
]


class FakeNRClient:
    """In-memory stand-in for NRClient. Same method contract."""

    def __init__(self, flows=None, rev="r0"):
        self._flows = copy.deepcopy(flows if flows is not None else SAMPLE_FLOWS)
        self._rev = rev
        self.context = {}          # flow_id -> {"memory": {...}}
        self.global_context = {"memory": {}}
        self.nodes = []            # raw /nodes payload
        self.injected = []         # node_ids injected
        self.installed = []        # module names installed
        self.post_calls = []       # list of (flows, rev) posted

    async def get_flows(self):
        return copy.deepcopy(self._flows), self._rev

    async def post_flows(self, flows, rev):
        if rev != self._rev:
            raise NRConflictError("rev mismatch")
        self.post_calls.append((copy.deepcopy(flows), rev))
        self._flows = copy.deepcopy(flows)
        self._rev = f"{self._rev}+1"
        return self._rev

    async def get_context(self, flow_id):
        return self.context.get(flow_id, {"memory": {}})

    async def get_global_context(self):
        return self.global_context

    async def inject(self, node_id):
        self.injected.append(node_id)
        return True

    async def get_nodes(self):
        return self.nodes

    async def install_module(self, module_name):
        self.installed.append(module_name)
        return {"version": "1.0.0", "types": ["x"]}

    async def close(self):
        pass


@pytest.fixture
def fake_client():
    return FakeNRClient()


@pytest.fixture
def settings_propose(tmp_path):
    return Settings(deploy_mode="propose", backup_dir=str(tmp_path / "backups"))


@pytest.fixture
def settings_auto(tmp_path):
    return Settings(deploy_mode="auto", backup_dir=str(tmp_path / "backups"))
```

- [ ] **Step 2: Commit**

```bash
git add tests/conftest.py
git commit -m "test: in-memory FakeNRClient + sample flows fixtures"
```

---

## Phase 1 — Config (`config.py`)

### Task 1.1: `Settings`

**Files:**
- Create: `src/nrha_mcp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from nrha_mcp.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.nr_url == "http://localhost:1880"
    assert s.deploy_mode == "propose"
    assert s.nr_verify_ssl is True
    assert s.mcp_transport == "stdio"


def test_env_override(monkeypatch):
    monkeypatch.setenv("NR_URL", "http://ha.local:1880")
    monkeypatch.setenv("DEPLOY_MODE", "auto")
    monkeypatch.setenv("NR_VERIFY_SSL", "0")
    s = Settings(_env_file=None)
    assert s.nr_url == "http://ha.local:1880"
    assert s.deploy_mode == "auto"
    assert s.nr_verify_ssl is False


def test_invalid_mode_rejected():
    import pytest
    with pytest.raises(Exception):
        Settings(deploy_mode="banana", _env_file=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.config`.

- [ ] **Step 3: Write `src/nrha_mcp/config.py`**

```python
"""Runtime configuration via environment variables."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Node-RED Admin API
    nr_url: str = "http://localhost:1880"
    nr_token: str = ""
    nr_user: str = ""
    nr_pass: str = ""
    nr_verify_ssl: bool = True

    # Safety
    deploy_mode: Literal["propose", "auto"] = "propose"
    backup_dir: str = "./backups"

    # Home Assistant (optional)
    ha_url: str = ""
    ha_token: str = ""

    # Transport
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/config.py tests/test_config.py
git commit -m "feat(config): Settings from env (NR_*, DEPLOY_MODE, HA_*, transport)"
```

---

## Phase 2 — Admin API client (`client.py`)

Ported from nr-mcp `client.py` **with PR #3 merged** (`NR_VERIFY_SSL`, `get_global_context`) and
refactored to take a `Settings` object instead of reading `os.environ` directly (testability).

### Task 2.1: `NRClient` construction & auth

**Files:**
- Create: `src/nrha_mcp/client.py`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client.py
import httpx
import respx
import pytest

from nrha_mcp.client import NRClient, NRConflictError, NRAuthError, NRNotFoundError
from nrha_mcp.config import Settings


def make_client(**over):
    base = dict(nr_url="http://nr.test", _env_file=None)
    base.update(over)
    return NRClient(Settings(**base))


def test_token_auth_header():
    c = make_client(nr_token="abc")
    assert c._client.headers["Authorization"] == "Bearer abc"


@respx.mock
@pytest.mark.asyncio
async def test_get_flows_returns_flows_and_rev():
    respx.get("http://nr.test/flows").mock(
        return_value=httpx.Response(200, json={"rev": "r1", "flows": [{"id": "a"}]})
    )
    c = make_client()
    flows, rev = await c.get_flows()
    assert rev == "r1" and flows == [{"id": "a"}]
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_post_flows_conflict_raises():
    respx.post("http://nr.test/flows").mock(return_value=httpx.Response(409))
    c = make_client()
    with pytest.raises(NRConflictError):
        await c.post_flows([], "stale")
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_maps():
    respx.get("http://nr.test/flows").mock(return_value=httpx.Response(401))
    c = make_client()
    with pytest.raises(NRAuthError):
        await c.get_flows()
    await c.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.client`.

- [ ] **Step 3: Write `src/nrha_mcp/client.py`**

Port nr-mcp's client verbatim except the constructor (takes `Settings`) and add `get_global_context`:

```python
"""Node-RED Admin API HTTP client with auth, optimistic locking, optional TLS skip.

Ported from nr-mcp (MIT) + PR #3. Constructor takes a Settings object.
"""

from .config import Settings
import httpx


class NRError(Exception):
    """Base Node-RED error."""


class NRAuthError(NRError):
    """401/403 — bad credentials."""


class NRConflictError(NRError):
    """409 — rev mismatch, flow changed outside MCP."""


class NRNotFoundError(NRError):
    """Node, flow, or context key not found."""


class NRClient:
    def __init__(self, settings: Settings):
        self.url = settings.nr_url
        headers: dict[str, str] = {}
        auth = None
        if settings.nr_token:
            headers["Authorization"] = f"Bearer {settings.nr_token}"
        elif settings.nr_user:
            auth = httpx.BasicAuth(settings.nr_user, settings.nr_pass)

        self._client = httpx.AsyncClient(
            base_url=self.url,
            auth=auth,
            headers=headers,
            timeout=30.0,
            verify=settings.nr_verify_ssl,
        )

    async def get_flows(self) -> tuple[list[dict], str]:
        r = await self._client.get("/flows", headers={"Node-RED-API-Version": "v2"})
        self._check_response(r)
        data = r.json()
        return data.get("flows", []), data.get("rev", "")

    async def post_flows(self, flows: list[dict], rev: str) -> str:
        headers = {
            "Node-RED-API-Version": "v2",
            "Node-RED-Deployment-Type": "full",
            "Content-Type": "application/json",
        }
        r = await self._client.post("/flows", json={"rev": rev, "flows": flows}, headers=headers)
        if r.status_code == 409:
            raise NRConflictError("Flow modified by another client (rev mismatch). Retry with fresh GET.")
        self._check_response(r)
        return r.json().get("rev", "")

    async def get_context(self, flow_id: str) -> dict:
        r = await self._client.get(f"/context/flow/{flow_id}")
        self._check_response(r)
        return r.json()

    async def get_global_context(self) -> dict:
        r = await self._client.get("/context/global")
        self._check_response(r)
        return r.json()

    async def inject(self, node_id: str) -> bool:
        r = await self._client.post(f"/inject/{node_id}")
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            raise NRNotFoundError(f"Inject node '{node_id}' not found")
        raise NRError(f"Inject failed: {r.status_code} {r.text}")

    async def get_nodes(self) -> list[dict]:
        r = await self._client.get(
            "/nodes", headers={"Accept": "application/json", "Node-RED-API-Version": "v2"}
        )
        self._check_response(r)
        return r.json()

    async def install_module(self, module_name: str) -> dict:
        r = await self._client.post(
            "/nodes",
            json={"module": module_name},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=120.0,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 400:
            raise NRError(f"Module '{module_name}' not found or invalid")
        if r.status_code == 409:
            raise NRError(f"Module '{module_name}' already installed")
        raise NRError(f"Install failed: {r.status_code} {r.text}")

    def _check_response(self, r: httpx.Response):
        if r.status_code in (401, 403):
            raise NRAuthError(f"Authentication failed ({r.status_code}). Check NR_TOKEN or NR_USER/NR_PASS and NR_URL.")
        if r.status_code == 404:
            raise NRNotFoundError(f"Not found: {r.url}")
        if r.status_code >= 400:
            raise NRError(f"Node-RED API error {r.status_code}: {r.text[:200]}")

    async def close(self):
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_client.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/client.py tests/test_client.py
git commit -m "feat(client): Admin API client (ported nr-mcp + PR3 SSL/global-context, Settings-based)"
```

---

## Phase 3 — Deploy gate (`deploy.py`)

The heart of the project's safety. `diff_flows` produces a preview; `commit` runs the
compute → diff → propose/confirm → backup → post pipeline with single conflict-retry.

### Task 3.1: `diff_flows`

**Files:**
- Create: `src/nrha_mcp/deploy.py`
- Test: `tests/test_deploy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy.py
import pytest

from nrha_mcp.deploy import diff_flows, commit


def test_diff_detects_add_remove_change():
    before = [{"id": "a", "name": "x"}, {"id": "b"}]
    after = [{"id": "a", "name": "y"}, {"id": "c"}]
    d = diff_flows(before, after)
    assert d["added"] == ["c"]
    assert d["removed"] == ["b"]
    assert d["changed"] == {"a": {"name": {"before": "x", "after": "y"}}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_deploy.py::test_diff_detects_add_remove_change -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.deploy`.

- [ ] **Step 3: Write `diff_flows` in `src/nrha_mcp/deploy.py`**

```python
"""Deploy safety gate: diff preview + propose/confirm + backup-first post."""

from datetime import datetime, timezone

from .client import NRConflictError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def diff_flows(before: list[dict], after: list[dict]) -> dict:
    """Human-readable diff of two flow lists, keyed by node id."""
    bmap = {n["id"]: n for n in before if "id" in n}
    amap = {n["id"]: n for n in after if "id" in n}
    added = [i for i in amap if i not in bmap]
    removed = [i for i in bmap if i not in amap]
    changed: dict[str, dict] = {}
    for i in bmap.keys() & amap.keys():
        b, a = bmap[i], amap[i]
        fields = {
            k: {"before": b.get(k), "after": a.get(k)}
            for k in (b.keys() | a.keys())
            if b.get(k) != a.get(k)
        }
        if fields:
            changed[i] = fields
    return {"added": added, "removed": removed, "changed": changed}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_deploy.py::test_diff_detects_add_remove_change -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/deploy.py tests/test_deploy.py
git commit -m "feat(deploy): diff_flows preview"
```

### Task 3.2: `commit` gate (propose / confirm / auto + backup + conflict retry)

**Files:**
- Modify: `src/nrha_mcp/deploy.py`
- Test: `tests/test_deploy.py`

`commit` depends on a `BackupStore` (Phase 4) — but to keep Phase 3 self-contained, the test uses
a tiny stub store with the same `snapshot(flows, label) -> path` contract.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_deploy.py

class StubStore:
    def __init__(self):
        self.snapshots = []
    def snapshot(self, flows, label):
        self.snapshots.append((flows, label))
        return f"/backups/{len(self.snapshots)}.json"


@pytest.mark.asyncio
async def test_propose_mode_does_not_write(fake_client, settings_propose):
    store = StubStore()
    def rebuild(flows):
        out = [dict(f) for f in flows]
        out.append({"id": "new", "type": "debug", "z": "tab1"})
        return out
    res = await commit(fake_client, store, settings_propose,
                       rebuild=rebuild, summary="add debug", confirm=False)
    assert res["deployed"] is False
    assert res["preview"]["added"] == ["new"]
    assert store.snapshots == []          # no backup taken on preview
    assert fake_client.post_calls == []    # nothing posted


@pytest.mark.asyncio
async def test_confirm_writes_and_backs_up(fake_client, settings_propose):
    store = StubStore()
    def rebuild(flows):
        out = [dict(f) for f in flows]
        out.append({"id": "new", "type": "debug", "z": "tab1"})
        return out
    res = await commit(fake_client, store, settings_propose,
                       rebuild=rebuild, summary="add debug", confirm=True)
    assert res["deployed"] is True
    assert len(store.snapshots) == 1       # backup before write
    assert len(fake_client.post_calls) == 1
    assert "new" in res["changes"]["added"]


@pytest.mark.asyncio
async def test_auto_mode_writes_without_confirm(fake_client, settings_auto):
    store = StubStore()
    rebuild = lambda flows: [dict(f) for f in flows]
    res = await commit(fake_client, store, settings_auto,
                       rebuild=rebuild, summary="noop", confirm=False)
    assert res["deployed"] is True


@pytest.mark.asyncio
async def test_conflict_triggers_single_retry(settings_auto):
    # client whose rev changes once between read and write, then settles
    from tests.conftest import FakeNRClient
    client = FakeNRClient()
    store = StubStore()
    calls = {"n": 0}
    real_post = client.post_flows
    async def flaky_post(flows, rev):
        calls["n"] += 1
        if calls["n"] == 1:
            client._rev = "moved"          # simulate concurrent change
            raise __import__("nrha_mcp.client", fromlist=["NRConflictError"]).NRConflictError("x")
        return await real_post(flows, rev)
    client.post_flows = flaky_post
    rebuild = lambda flows: [dict(f) for f in flows]
    res = await commit(client, store, settings_auto, rebuild=rebuild, summary="retry", confirm=True)
    assert res["deployed"] is True
    assert calls["n"] == 2                  # retried once after conflict
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_deploy.py -v`
Expected: FAIL — `commit` has no implementation body yet (ImportError or AttributeError).

- [ ] **Step 3: Add `commit` to `src/nrha_mcp/deploy.py`**

```python
async def commit(client, store, settings, *, rebuild, summary, confirm=False):
    """Run the safety gate.

    rebuild(flows) -> proposed_flows : pure, must NOT mutate `flows`.
    Returns a dict describing either the preview (no write) or the deploy result.
    """
    flows, rev = await client.get_flows()
    proposed = rebuild(flows)
    preview = diff_flows(flows, proposed)

    if settings.deploy_mode == "propose" and not confirm:
        return {
            "deployed": False,
            "mode": "propose",
            "summary": summary,
            "preview": preview,
            "next": "Re-call this tool with confirm=true to apply.",
        }

    backup_path = store.snapshot(flows, summary)
    try:
        new_rev = await client.post_flows(proposed, rev)
    except NRConflictError:
        flows, rev = await client.get_flows()
        proposed = rebuild(flows)
        new_rev = await client.post_flows(proposed, rev)

    return {
        "deployed": True,
        "rev": new_rev,
        "backup": str(backup_path),
        "deployed_at": _now_iso(),
        "summary": summary,
        "changes": preview,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_deploy.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/deploy.py tests/test_deploy.py
git commit -m "feat(deploy): commit gate — propose/confirm/auto, backup-first, conflict retry"
```

---

## Phase 4 — Backup store (`backup.py`)

### Task 4.1: `BackupStore`

**Files:**
- Create: `src/nrha_mcp/backup.py`
- Test: `tests/test_backup.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backup.py
import json

from nrha_mcp.backup import BackupStore


def test_snapshot_writes_json(tmp_path):
    store = BackupStore(str(tmp_path))
    flows = [{"id": "a"}]
    path = store.snapshot(flows, label="my change")
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved["flows"] == flows
    assert saved["label"] == "my change"


def test_list_and_restore(tmp_path):
    store = BackupStore(str(tmp_path))
    p = store.snapshot([{"id": "a"}], label="x")
    listed = store.list_backups()
    assert len(listed) == 1 and listed[0]["path"] == str(p)
    restored = store.restore(str(p))
    assert restored == [{"id": "a"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backup.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.backup`.

- [ ] **Step 3: Write `src/nrha_mcp/backup.py`**

```python
"""Filesystem backup store: snapshot flows before any deploy; list & restore."""

import json
from datetime import datetime, timezone
from pathlib import Path


class BackupStore:
    def __init__(self, backup_dir: str):
        self.dir = Path(backup_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self, flows: list[dict], label: str = "") -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = self.dir / f"flows-{ts}.json"
        path.write_text(json.dumps(
            {"saved_at": ts, "label": label, "flows": flows},
            indent=2, ensure_ascii=False,
        ))
        return path

    def list_backups(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("flows-*.json")):
            meta = json.loads(p.read_text())
            out.append({"path": str(p), "saved_at": meta.get("saved_at"), "label": meta.get("label", "")})
        return out

    def restore(self, path: str) -> list[dict]:
        return json.loads(Path(path).read_text())["flows"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_backup.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/backup.py tests/test_backup.py
git commit -m "feat(backup): BackupStore snapshot/list/restore"
```

---

## Phase 5 — Read-only flow tools (`flows.py`)

Ported from nr-mcp `tools.py` (the read functions) with PR #3's `fields` param on `get_flow` and
the new `get_global_context`. These never write.

### Task 5.1: Port read-only tools

**Files:**
- Create: `src/nrha_mcp/flows.py`
- Test: `tests/test_flows.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_flows.py
import pytest

from nrha_mcp import flows
from nrha_mcp.client import NRNotFoundError


@pytest.mark.asyncio
async def test_summary_counts(fake_client):
    res = await flows.get_flow_summary(fake_client)
    assert res["total_tabs"] == 1
    assert res["tabs"][0]["label"] == "Heating"
    assert res["tabs"][0]["node_count"] == 3


@pytest.mark.asyncio
async def test_get_flow_by_name(fake_client):
    res = await flows.get_flow(fake_client, "heating")
    assert res["tab"]["id"] == "tab1"
    assert res["total_nodes"] == 3


@pytest.mark.asyncio
async def test_get_flow_fields_filter(fake_client):
    res = await flows.get_flow(fake_client, "tab1", fields=["name"])
    n = next(x for x in res["nodes"] if x["id"] == "n2")
    assert set(n.keys()) <= {"id", "type", "z", "name"}   # identity always kept


@pytest.mark.asyncio
async def test_search_by_type(fake_client):
    res = await flows.search_nodes(fake_client, query="", node_type="function")
    # empty query matches the 'name'/'type' fields containing '' -> matches; filter by type
    assert all(r["type"] == "function" for r in res["results"])


@pytest.mark.asyncio
async def test_function_code(fake_client):
    res = await flows.get_function_code(fake_client, "n2")
    assert res["code"] == "return msg;"


@pytest.mark.asyncio
async def test_global_context_lists_keys(fake_client):
    fake_client.global_context = {"memory": {"scheme": {"msg": 1}}}
    res = await flows.get_global_context(fake_client, None)
    assert res["keys"] == ["scheme"]


@pytest.mark.asyncio
async def test_get_flow_unknown_raises(fake_client):
    with pytest.raises(NRNotFoundError):
        await flows.get_flow(fake_client, "nope")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_flows.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.flows`.

- [ ] **Step 3: Write `src/nrha_mcp/flows.py`**

Port these functions verbatim from nr-mcp `tools.py` (with PR3): `get_flow_summary`, `get_flow`
(with `fields`), `search_nodes`, `get_function_code`, `get_node_config`, `get_flow_context`,
`get_global_context`, `get_debug_output`. The exact bodies (already reviewed) are:

```python
"""Read-only Node-RED inspection tools. Ported from nr-mcp (MIT) + PR #3."""

from .client import NRClient, NRError, NRNotFoundError


async def get_flow_summary(client: NRClient) -> dict:
    flows, _ = await client.get_flows()
    tabs = [f for f in flows if f.get("type") == "tab"]
    groups = [f for f in flows if f.get("type") == "group"]
    nodes = [f for f in flows if f.get("type") not in ("tab", "group", "subflow", "comment") and "z" in f]
    tab_map = {}
    for tab in tabs:
        tab_map[tab["id"]] = {"id": tab["id"], "label": tab.get("label", ""), "node_count": 0, "groups": []}
    for node in nodes:
        z = node.get("z", "")
        if z in tab_map:
            tab_map[z]["node_count"] += 1
    for group in groups:
        z = group.get("z", "")
        if z in tab_map:
            tab_map[z]["groups"].append({
                "id": group["id"], "label": group.get("name", group.get("label", "")),
                "node_count": len(group.get("nodes", [])),
            })
    return {"total_nodes": len(nodes), "total_tabs": len(tabs), "tabs": list(tab_map.values())}


async def get_flow(client: NRClient, name_or_id: str, fields: list[str] | None = None) -> dict:
    flows, _ = await client.get_flows()
    tabs = [f for f in flows if f.get("type") == "tab"]
    tab = None
    q = name_or_id.lower()
    for t in tabs:
        if t["id"] == name_or_id:
            tab = t
            break
    if not tab:
        matches = [t for t in tabs if q in t.get("label", "").lower()]
        if len(matches) == 1:
            tab = matches[0]
        elif len(matches) > 1:
            raise NRNotFoundError(f"Ambiguous tab name '{name_or_id}'. Matches: {[m.get('label') for m in matches]}")
        else:
            raise NRNotFoundError(f"Tab not found: '{name_or_id}'")
    tab_id = tab["id"]
    nodes = [f for f in flows if f.get("z") == tab_id and f.get("type") != "group"]
    groups = [f for f in flows if f.get("z") == tab_id and f.get("type") == "group"]
    if fields:
        keep = set(fields) | {"id", "type", "z"}
        nodes = [{k: v for k, v in n.items() if k in keep} for n in nodes]
    return {
        "tab": {"id": tab["id"], "label": tab.get("label", ""), "type": "tab", "info": tab.get("info", "")},
        "nodes": nodes,
        "groups": [{"id": g["id"], "label": g.get("name", ""), "nodes": g.get("nodes", [])} for g in groups],
        "total_nodes": len(nodes),
    }


async def search_nodes(client: NRClient, query: str, node_type: str | None = None,
                       flow: str | None = None, max_results: int = 20) -> dict:
    flows, _ = await client.get_flows()
    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    nodes = [f for f in flows if "z" in f and f.get("type") not in ("tab", "group")]
    q = query.lower()
    results = []
    for node in nodes:
        if node_type and node.get("type") != node_type:
            continue
        if flow:
            fl = flow.lower()
            z = node.get("z", "")
            if z != flow and fl not in tabs.get(z, "").lower():
                continue
        match_in = None
        snippet = ""
        for field in ("name", "type", "func", "template", "action", "data"):
            val = node.get(field, "")
            if isinstance(val, str) and q in val.lower():
                match_in, snippet = field, val[:200]
                break
        if match_in:
            results.append({
                "id": node["id"], "name": node.get("name", ""), "type": node.get("type", ""),
                "flow_id": node.get("z", ""), "flow_label": tabs.get(node.get("z", ""), ""),
                "match_in": match_in, "snippet": snippet,
            })
            if len(results) >= max_results:
                break
    return {"results": results, "total": len(results), "truncated": len(results) >= max_results}


async def get_function_code(client: NRClient, node_id: str) -> dict:
    flows, _ = await client.get_flows()
    node = next((f for f in flows if f.get("id") == node_id), None)
    if not node:
        raise NRNotFoundError(f"Node not found: {node_id}")
    if node.get("type") != "function":
        return {"error": f"Node {node_id} is type '{node.get('type')}', not 'function'"}
    return {
        "node_id": node_id, "name": node.get("name", ""), "code": node.get("func", ""),
        "outputs": node.get("outputs", 1), "initialize": node.get("initialize", ""),
        "finalize": node.get("finalize", ""),
    }


async def get_node_config(client: NRClient, node_id: str) -> dict:
    flows, _ = await client.get_flows()
    node = next((f for f in flows if f.get("id") == node_id), None)
    if not node:
        raise NRNotFoundError(f"Node not found: {node_id}")
    tabs = {f["id"]: f.get("label", "") for f in flows if f.get("type") == "tab"}
    groups = [f for f in flows if f.get("type") == "group"]
    downstream = []
    for wl in node.get("wires", []):
        downstream.extend(wl)
    upstream = []
    for f in flows:
        for wl in f.get("wires", []):
            if node_id in wl:
                upstream.append(f["id"])
    group_label = None
    for g in groups:
        if node_id in g.get("nodes", []):
            group_label = g.get("name", g.get("label", ""))
            break
    result = dict(node)
    result["_flow_label"] = tabs.get(node.get("z", ""), "")
    result["_group_label"] = group_label
    result["_downstream"] = downstream
    result["_upstream"] = upstream
    return result


async def get_flow_context(client: NRClient, flow_id: str, key: str | None = None) -> dict:
    data = await client.get_context(flow_id)
    memory = data.get("memory", {})
    if key:
        if key not in memory:
            raise NRNotFoundError(f"Context key not found: '{key}'")
        return {"flow_id": flow_id, "key": key, "value": memory[key].get("msg")}
    return {"flow_id": flow_id, "keys": list(memory.keys())}


async def get_global_context(client: NRClient, key: str | None = None) -> dict:
    data = await client.get_global_context()
    memory = data.get("memory", {})
    if key:
        if key not in memory:
            raise NRNotFoundError(f"Global context key not found: '{key}'")
        return {"scope": "global", "key": key, "value": memory[key].get("msg")}
    return {"scope": "global", "keys": list(memory.keys())}


async def get_debug_output(client: NRClient, flow_id: str, key: str | None = None) -> dict:
    return await get_flow_context(client, flow_id, key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_flows.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/flows.py tests/test_flows.py
git commit -m "feat(flows): read-only inspection tools (ported nr-mcp + PR3 fields/global-context)"
```

---

## Phase 6 — Mutating tools on the gate (`edit.py`)

Each function builds a pure `rebuild(flows) -> proposed_flows` closure (deep-copies, never mutates
input) and delegates to `deploy.commit`. Validation logic is ported from nr-mcp and runs inside
`rebuild` so it re-checks on conflict retry.

### Task 6.1: `safe_deploy`, `safe_deploy_many`, `add_wire`

**Files:**
- Create: `src/nrha_mcp/edit.py`
- Test: `tests/test_edit.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_edit.py
import pytest

from nrha_mcp import edit
from nrha_mcp.client import NRError, NRNotFoundError


@pytest.mark.asyncio
async def test_safe_deploy_preview_only(fake_client, settings_propose, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.safe_deploy(fake_client, store, settings_propose,
                                 node_id="n2", fields={"name": "calc2"}, confirm=False)
    assert res["deployed"] is False
    assert res["preview"]["changed"]["n2"]["name"]["after"] == "calc2"
    assert fake_client.post_calls == []


@pytest.mark.asyncio
async def test_safe_deploy_confirm_writes(fake_client, settings_propose, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.safe_deploy(fake_client, store, settings_propose,
                                 node_id="n2", fields={"name": "calc2"}, confirm=True)
    assert res["deployed"] is True
    assert len(fake_client.post_calls) == 1


@pytest.mark.asyncio
async def test_safe_deploy_rejects_immutable(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await edit.safe_deploy(fake_client, store, settings_auto,
                               node_id="n2", fields={"z": "other"}, confirm=True)


@pytest.mark.asyncio
async def test_add_wire_idempotent(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    # n2 already wires to n3; adding again is a no-op
    res = await edit.add_wire(fake_client, store, settings_auto,
                              from_id="n2", output_index=0, to_id="n3", confirm=True)
    assert res.get("already_existed") is True
    assert fake_client.post_calls == []


@pytest.mark.asyncio
async def test_safe_deploy_many(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.safe_deploy_many(fake_client, store, settings_auto, updates=[
        {"node_id": "n2", "fields": {"name": "a"}},
        {"node_id": "n3", "fields": {"name": "b"}},
    ], confirm=True)
    assert res["deployed"] is True
    assert len(fake_client.post_calls) == 1   # single atomic deploy
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_edit.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.edit`.

- [ ] **Step 3: Write `safe_deploy`, `safe_deploy_many`, `add_wire` in `src/nrha_mcp/edit.py`**

```python
"""Mutating Node-RED tools, refactored onto the deploy gate (deploy.commit).

Validation/mutation logic ported from nr-mcp (MIT) + PR #3, restructured into pure
rebuild(flows) closures so it re-runs on optimistic-lock conflict retry.
"""

import copy

from .client import NRError, NRNotFoundError
from .deploy import commit

IMMUTABLE = {"id", "type", "z"}


async def safe_deploy(client, store, settings, *, node_id, fields, confirm=False, description=None):
    bad = set(fields) & IMMUTABLE
    if bad:
        raise NRError(f"Cannot modify immutable fields: {bad}")

    def rebuild(flows):
        new = copy.deepcopy(flows)
        idx = next((i for i, f in enumerate(new) if f.get("id") == node_id), None)
        if idx is None:
            raise NRNotFoundError(f"Node not found: {node_id}")
        new[idx].update(fields)
        return new

    summary = description or f"patch {node_id}: {list(fields)}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def safe_deploy_many(client, store, settings, *, updates, confirm=False, description=None):
    for u in updates:
        bad = set(u.get("fields", {})) & IMMUTABLE
        if bad:
            raise NRError(f"Cannot modify immutable fields {bad} on '{u['node_id']}'")

    def rebuild(flows):
        new = copy.deepcopy(flows)
        index = {f["id"]: i for i, f in enumerate(new)}
        missing = [u["node_id"] for u in updates if u["node_id"] not in index]
        if missing:
            raise NRNotFoundError(f"Nodes not found: {missing}")
        for u in updates:
            new[index[u["node_id"]]].update(u["fields"])
        return new

    summary = description or f"patch {len(updates)} nodes"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def add_wire(client, store, settings, *, from_id, output_index, to_id, confirm=False):
    # Pre-check idempotency against current state (cheap, avoids a needless deploy)
    flows, _ = await client.get_flows()
    ids = {f["id"] for f in flows}
    if from_id not in ids:
        raise NRNotFoundError(f"Source node not found: '{from_id}'")
    if to_id not in ids:
        raise NRNotFoundError(f"Target node not found: '{to_id}'")
    src = next(f for f in flows if f["id"] == from_id)
    cur = src.get("wires", [])
    if len(cur) > output_index and to_id in cur[output_index]:
        return {"deployed": False, "already_existed": True,
                "message": f"Wire already exists: {from_id}[{output_index}] -> {to_id}"}

    def rebuild(fl):
        new = copy.deepcopy(fl)
        idx = next(i for i, f in enumerate(new) if f["id"] == from_id)
        wires = [list(w) for w in new[idx].get("wires", [])]
        while len(wires) <= output_index:
            wires.append([])
        if to_id not in wires[output_index]:
            wires[output_index].append(to_id)
        new[idx]["wires"] = wires
        return new

    summary = f"wire {from_id}[{output_index}] -> {to_id}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_edit.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/edit.py tests/test_edit.py
git commit -m "feat(edit): safe_deploy/safe_deploy_many/add_wire on the gate"
```

### Task 6.2: `create_nodes`, `delete_nodes`

**Files:**
- Modify: `src/nrha_mcp/edit.py`
- Test: `tests/test_edit.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_edit.py

@pytest.mark.asyncio
async def test_create_nodes_validates_and_writes(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.create_nodes(fake_client, store, settings_auto,
        nodes=[{"id": "n9", "type": "debug", "z": "tab1"}], confirm=True)
    assert res["deployed"] is True
    flows, _ = await fake_client.get_flows()
    assert any(f["id"] == "n9" for f in flows)


@pytest.mark.asyncio
async def test_create_nodes_duplicate_id_rejected(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await edit.create_nodes(fake_client, store, settings_auto,
            nodes=[{"id": "n2", "type": "debug", "z": "tab1"}], confirm=True)


@pytest.mark.asyncio
async def test_delete_nodes_cleans_refs(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    res = await edit.delete_nodes(fake_client, store, settings_auto,
        node_ids=["n3"], confirm=True)
    assert res["deployed"] is True
    flows, _ = await fake_client.get_flows()
    # n3 removed; group g1 no longer lists it; n2 wires no longer reference it
    g1 = next(f for f in flows if f["id"] == "g1")
    n2 = next(f for f in flows if f["id"] == "n2")
    assert "n3" not in g1["nodes"]
    assert all("n3" not in out for out in n2["wires"])


@pytest.mark.asyncio
async def test_delete_tab_refused(fake_client, settings_auto, tmp_path):
    from nrha_mcp.backup import BackupStore
    store = BackupStore(str(tmp_path))
    with pytest.raises(NRError):
        await edit.delete_nodes(fake_client, store, settings_auto, node_ids=["tab1"], confirm=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_edit.py -v`
Expected: FAIL — `create_nodes`/`delete_nodes` not defined.

- [ ] **Step 3: Append `create_nodes` and `delete_nodes` to `src/nrha_mcp/edit.py`**

```python
async def create_nodes(client, store, settings, *, nodes, confirm=False, description=None):
    for n in nodes:
        if not n.get("id") or not n.get("type"):
            raise NRError(f"Node missing required 'id' or 'type': {n}")
        if "z" not in n and n["type"] not in ("tab", "subflow"):
            raise NRError(f"Node '{n['id']}' missing 'z' (target tab ID)")

    def rebuild(flows):
        tab_ids = {f["id"] for f in flows if f.get("type") == "tab"}
        existing = {f["id"] for f in flows}
        for n in nodes:
            z = n.get("z", "")
            if z and z not in tab_ids and n["type"] not in ("tab", "subflow"):
                raise NRError(f"Target tab '{z}' not found for node '{n['id']}'")
            if n["id"] in existing:
                raise NRError(f"Node ID '{n['id']}' already exists")
        return copy.deepcopy(flows) + copy.deepcopy(nodes)

    summary = description or f"create {len(nodes)} node(s): {[n['id'] for n in nodes]}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)


async def delete_nodes(client, store, settings, *, node_ids, confirm=False, description=None):
    ids = set(node_ids)

    def rebuild(flows):
        existing = {f["id"] for f in flows}
        missing = ids - existing
        if missing:
            raise NRError(f"Nodes not found: {missing}")
        for f in flows:
            if f["id"] in ids and f.get("type") == "tab":
                raise NRError(f"Refusing to delete tab '{f['id']}'. Remove all nodes first.")
        new = [copy.deepcopy(f) for f in flows if f["id"] not in ids]
        for f in new:
            if f.get("type") == "group" and "nodes" in f:
                f["nodes"] = [nid for nid in f["nodes"] if nid not in ids]
            if "wires" in f:
                f["wires"] = [[w for w in out if w not in ids] for out in f["wires"]]
        return new

    summary = description or f"delete {len(node_ids)} node(s): {node_ids}"
    return await commit(client, store, settings, rebuild=rebuild, summary=summary, confirm=confirm)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_edit.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/edit.py tests/test_edit.py
git commit -m "feat(edit): create_nodes/delete_nodes on the gate (ref cleanup, validation)"
```

---

## Phase 7 — Modules & inject (`modules.py`)

`install_module` and `inject` are runtime actions (not flow deploys) and do not go through the
flow gate. Ported from nr-mcp.

### Task 7.1: `get_installed_modules`, `install_module`, `inject_node`

**Files:**
- Create: `src/nrha_mcp/modules.py`
- Test: `tests/test_modules.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_modules.py
import pytest

from nrha_mcp import modules
from nrha_mcp.client import NRError


@pytest.mark.asyncio
async def test_list_modules_filters(fake_client):
    fake_client.nodes = [
        {"name": "node-red", "version": "3", "types": ["inject", "debug"]},
        {"name": "node-red-contrib-home-assistant-websocket", "version": "0.6", "types": ["server", "api-call-service"]},
    ]
    res = await modules.get_installed_modules(fake_client, "home-assistant")
    assert res["total"] == 1
    assert res["modules"][0]["name"].endswith("websocket")


@pytest.mark.asyncio
async def test_inject_ok(fake_client):
    res = await modules.inject_node(fake_client, "n1")
    assert res["success"] is True
    assert "n1" in fake_client.injected


@pytest.mark.asyncio
async def test_inject_wrong_type(fake_client):
    with pytest.raises(NRError):
        await modules.inject_node(fake_client, "n2")   # function, not inject
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_modules.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.modules`.

- [ ] **Step 3: Write `src/nrha_mcp/modules.py`** (ported from nr-mcp)

```python
"""Node-RED module management + inject trigger. Ported from nr-mcp (MIT)."""

from .client import NRClient, NRError, NRNotFoundError


async def get_installed_modules(client: NRClient, query: str | None) -> dict:
    raw = await client.get_nodes()
    modules = []
    for mod in raw:
        name = mod.get("name", mod.get("id", "unknown"))
        version = mod.get("version", "?")
        types = mod.get("types", [])
        if not types and "nodes" in mod:
            types = []
            for n in mod["nodes"]:
                types.extend(n.get("types", []))
        if query:
            q = query.lower()
            if q not in name.lower() and not any(q in t.lower() for t in types):
                continue
        modules.append({"name": name, "version": version, "types": types, "enabled": mod.get("enabled", True)})
    return {"modules": modules, "total": len(modules), "query": query}


async def install_module(client: NRClient, module_name: str) -> dict:
    if not module_name or " " in module_name:
        raise NRError(f"Invalid module name: '{module_name}'")
    result = await client.install_module(module_name)
    return {"success": True, "module": module_name, "version": result.get("version", "unknown"),
            "types": result.get("types", []), "message": f"Module '{module_name}' installed successfully"}


async def inject_node(client: NRClient, node_id: str) -> dict:
    flows, _ = await client.get_flows()
    node = next((f for f in flows if f["id"] == node_id), None)
    if not node:
        raise NRNotFoundError(f"Node '{node_id}' not found")
    if node.get("type") != "inject":
        raise NRError(f"Node '{node_id}' is type '{node.get('type')}', not 'inject'")
    await client.inject(node_id)
    return {"success": True, "node_id": node_id, "node_name": node.get("name", ""), "message": "Inject triggered"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_modules.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/modules.py tests/test_modules.py
git commit -m "feat(modules): installed modules, install, inject (ported)"
```

---

## Phase 8 — Home Assistant awareness (`ha.py`)

The differentiator beyond nr-mcp. Static recognition always works; live validation activates only
when `HA_URL`/`HA_TOKEN` are set.

### Task 8.1: HA node recognition (`list_ha_nodes`, `get_server_config`)

**Files:**
- Create: `src/nrha_mcp/ha.py`
- Test: `tests/test_ha.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ha.py
import pytest

from nrha_mcp import ha


@pytest.mark.asyncio
async def test_list_ha_nodes(fake_client):
    res = await ha.list_ha_nodes(fake_client)
    ids = [n["id"] for n in res["ha_nodes"]]
    assert "n3" in ids                       # api-call-service node
    n3 = next(n for n in res["ha_nodes"] if n["id"] == "n3")
    assert n3["domain"] == "switch" and n3["service"] == "turn_on"
    assert n3["entity"] == "switch.boiler"


@pytest.mark.asyncio
async def test_get_server_config(fake_client):
    res = await ha.get_server_config(fake_client)
    assert res["servers"][0]["id"] == "srv1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ha.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.ha`.

- [ ] **Step 3: Write recognition part of `src/nrha_mcp/ha.py`**

```python
"""Home Assistant awareness for Node-RED flows.

Recognizes node-red-contrib-home-assistant-websocket node types and, optionally
(when HA_URL/HA_TOKEN are set), validates service/entity references against a live HA.
"""

from .client import NRClient

# Node types provided by node-red-contrib-home-assistant-websocket
HA_NODE_TYPES = {
    "server", "ha-entity-config", "ha-device-config",
    "api-call-service", "api-current-state", "api-get-history", "api-render-template",
    "server-state-changed", "poll-state", "trigger-state", "server-events",
    "ha-webhook", "ha-fire-event", "ha-get-entities", "ha-wait-until", "ha-sensor",
    "ha-button", "ha-switch", "ha-binary-sensor", "ha-number", "ha-select", "ha-text",
}


def _entity_of(node: dict) -> str | None:
    # HA nodes store the entity under different keys across versions
    for k in ("entityId", "entity_id", "entityid"):
        if node.get(k):
            return node[k]
    data = node.get("data")
    if isinstance(data, dict) and data.get("entity_id"):
        return data["entity_id"]
    return None


async def list_ha_nodes(client: NRClient) -> dict:
    flows, _ = await client.get_flows()
    out = []
    for n in flows:
        if n.get("type") in HA_NODE_TYPES and n.get("type") not in ("server", "ha-entity-config", "ha-device-config"):
            out.append({
                "id": n["id"], "type": n.get("type"), "name": n.get("name", ""),
                "flow_id": n.get("z", ""),
                "domain": n.get("domain"), "service": n.get("service"),
                "entity": _entity_of(n),
            })
    return {"ha_nodes": out, "total": len(out)}


async def get_server_config(client: NRClient) -> dict:
    flows, _ = await client.get_flows()
    servers = [{"id": f["id"], "name": f.get("name", "")} for f in flows if f.get("type") == "server"]
    return {"servers": servers, "total": len(servers)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ha.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat(ha): recognize HA nodes + list server configs"
```

### Task 8.2: Optional live validation (`validate_calls`)

**Files:**
- Modify: `src/nrha_mcp/ha.py`
- Test: `tests/test_ha.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_ha.py

class FakeHA:
    def __init__(self, entities, services):
        self._entities = entities
        self._services = services
    async def list_entities(self):
        return self._entities
    async def list_services(self):
        return self._services


@pytest.mark.asyncio
async def test_validate_flags_missing_entity(fake_client):
    hac = FakeHA(entities={"switch.other"}, services={"switch.turn_on"})
    res = await ha.validate_calls(fake_client, hac)
    problems = {p["node_id"]: p for p in res["problems"]}
    assert "n3" in problems
    assert "switch.boiler" in problems["n3"]["missing_entity"]


@pytest.mark.asyncio
async def test_validate_ok_when_present(fake_client):
    hac = FakeHA(entities={"switch.boiler"}, services={"switch.turn_on"})
    res = await ha.validate_calls(fake_client, hac)
    assert res["problems"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ha.py -v`
Expected: FAIL — `validate_calls` not defined.

- [ ] **Step 3: Append the HA REST client + `validate_calls` to `src/nrha_mcp/ha.py`**

```python
import httpx
from .config import Settings


class HARestClient:
    """Minimal Home Assistant REST client (entities + services)."""

    def __init__(self, settings: Settings):
        self._client = httpx.AsyncClient(
            base_url=settings.ha_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=15.0,
        )

    async def list_entities(self) -> set[str]:
        r = await self._client.get("/api/states")
        r.raise_for_status()
        return {s["entity_id"] for s in r.json()}

    async def list_services(self) -> set[str]:
        r = await self._client.get("/api/services")
        r.raise_for_status()
        out = set()
        for dom in r.json():
            domain = dom["domain"]
            for svc in dom.get("services", {}):
                out.add(f"{domain}.{svc}")
        return out

    async def close(self):
        await self._client.aclose()


async def validate_calls(client: NRClient, ha_client) -> dict:
    """Flag api-call-service nodes whose service or entity does not exist in live HA."""
    entities = await ha_client.list_entities()
    services = await ha_client.list_services()
    listing = await list_ha_nodes(client)
    problems = []
    for n in listing["ha_nodes"]:
        if n["type"] != "api-call-service":
            continue
        prob = {}
        if n.get("domain") and n.get("service"):
            svc = f"{n['domain']}.{n['service']}"
            if svc not in services:
                prob["missing_service"] = svc
        ent = n.get("entity")
        if ent and ent not in entities:
            prob["missing_entity"] = ent
        if prob:
            problems.append({"node_id": n["id"], "name": n.get("name", ""), **prob})
    return {"problems": problems, "checked": len(listing["ha_nodes"])}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ha.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/nrha_mcp/ha.py tests/test_ha.py
git commit -m "feat(ha): optional live validation of service/entity refs"
```

---

## Phase 9 — Server wiring (`server.py`)

FastMCP registration. Tool functions are thin wrappers around the modules, sharing one
`NRClient`, `BackupStore`, and `Settings`. Mutating tools expose `confirm`.

### Task 9.1: FastMCP server + smoke test

**Files:**
- Create: `src/nrha_mcp/server.py`
- Test: `tests/test_server_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_server_smoke.py
import nrha_mcp.server as srv


def test_tools_registered():
    # FastMCP stores registered tools; assert our names exist.
    names = set(srv.mcp._tool_manager._tools.keys()) if hasattr(srv, "mcp") else set()
    expected = {
        "nr_get_flow_summary", "nr_get_flow", "nr_search_nodes", "nr_get_function_code",
        "nr_get_node_config", "nr_get_flow_context", "nr_get_global_context",
        "nr_get_debug_output", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
        "nr_create_nodes", "nr_delete_nodes", "nr_get_installed_modules", "nr_install_module",
        "nr_inject", "ha_list_ha_nodes", "ha_get_server_config", "ha_validate_calls",
    }
    assert expected.issubset(names)
```

> Note: if the installed `mcp` version exposes registered tools differently, adjust the accessor
> in this test to match (`srv.mcp.list_tools()` is an async alternative). The assertion set stays.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: nrha_mcp.server`.

- [ ] **Step 3: Write `src/nrha_mcp/server.py`**

```python
"""FastMCP server for node-red-homeassistant-mcp."""

import json

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .client import NRClient, NRError
from .backup import BackupStore
from . import flows, edit, modules, ha

settings = Settings()
client = NRClient(settings)
store = BackupStore(settings.backup_dir)

mcp = FastMCP("node-red-homeassistant-mcp", json_response=True)


def _ok(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False)


def _err(msg: str) -> str:
    return _ok({"error": msg})


def _wrap(coro_factory):
    """Run a tool body, mapping NRError -> structured error JSON."""
    async def runner():
        try:
            return _ok(await coro_factory())
        except NRError as e:
            return _err(str(e))
        except Exception as e:  # noqa: BLE001 - surface unexpected errors to the client
            return _err(f"Unexpected error: {type(e).__name__}: {e}")
    return runner()


# ---- read-only ----
@mcp.tool()
async def nr_get_flow_summary() -> str:
    """Overview of all Node-RED tabs with node counts and groups."""
    return await _wrap(lambda: flows.get_flow_summary(client))


@mcp.tool()
async def nr_get_flow(name_or_id: str, fields: list[str] | None = None) -> str:
    """Get one tab with its nodes/groups. `fields` returns only selected node keys (compact)."""
    return await _wrap(lambda: flows.get_flow(client, name_or_id, fields=fields))


@mcp.tool()
async def nr_search_nodes(query: str, type: str | None = None, flow: str | None = None, max_results: int = 20) -> str:
    """Search nodes by name/type/function-code/template."""
    return await _wrap(lambda: flows.search_nodes(client, query, node_type=type, flow=flow, max_results=max_results))


@mcp.tool()
async def nr_get_function_code(node_id: str) -> str:
    """Get a function node's JavaScript code."""
    return await _wrap(lambda: flows.get_function_code(client, node_id))


@mcp.tool()
async def nr_get_node_config(node_id: str) -> str:
    """Full node config incl. upstream/downstream and group membership."""
    return await _wrap(lambda: flows.get_node_config(client, node_id))


@mcp.tool()
async def nr_get_flow_context(flow_id: str, key: str | None = None) -> str:
    """Read flow context (key=null lists keys)."""
    return await _wrap(lambda: flows.get_flow_context(client, flow_id, key))


@mcp.tool()
async def nr_get_global_context(key: str | None = None) -> str:
    """Read global context (key=null lists keys)."""
    return await _wrap(lambda: flows.get_global_context(client, key))


@mcp.tool()
async def nr_get_debug_output(flow_id: str, key: str | None = None) -> str:
    """Read debug data stored to flow context."""
    return await _wrap(lambda: flows.get_debug_output(client, flow_id, key))


# ---- mutating (confirm-gated) ----
@mcp.tool()
async def nr_safe_deploy(node_id: str, fields: dict, confirm: bool = False, description: str | None = None) -> str:
    """Patch one node's fields. In propose mode previews unless confirm=true. Backup-first."""
    return await _wrap(lambda: edit.safe_deploy(client, store, settings, node_id=node_id, fields=fields, confirm=confirm, description=description))


@mcp.tool()
async def nr_safe_deploy_many(updates: list[dict], confirm: bool = False, description: str | None = None) -> str:
    """Patch many nodes ([{node_id, fields}]) in one atomic deploy. Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.safe_deploy_many(client, store, settings, updates=updates, confirm=confirm, description=description))


@mcp.tool()
async def nr_add_wire(from_id: str, output_index: int, to_id: str, confirm: bool = False) -> str:
    """Idempotently add a wire from_id[output_index] -> to_id. Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.add_wire(client, store, settings, from_id=from_id, output_index=output_index, to_id=to_id, confirm=confirm))


@mcp.tool()
async def nr_create_nodes(nodes: list[dict], confirm: bool = False, description: str | None = None) -> str:
    """Create nodes/groups (each needs id,type and z for tab nodes). Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.create_nodes(client, store, settings, nodes=nodes, confirm=confirm, description=description))


@mcp.tool()
async def nr_delete_nodes(node_ids: list[str], confirm: bool = False, description: str | None = None) -> str:
    """Delete nodes by id (cleans group/wire refs). Confirm-gated, backup-first."""
    return await _wrap(lambda: edit.delete_nodes(client, store, settings, node_ids=node_ids, confirm=confirm, description=description))


# ---- modules / inject ----
@mcp.tool()
async def nr_get_installed_modules(query: str | None = None) -> str:
    """List installed Node-RED modules and their node types."""
    return await _wrap(lambda: modules.get_installed_modules(client, query))


@mcp.tool()
async def nr_install_module(module_name: str) -> str:
    """Install a Node-RED npm module (30-60s)."""
    return await _wrap(lambda: modules.install_module(client, module_name))


@mcp.tool()
async def nr_inject(node_id: str) -> str:
    """Trigger an inject node to fire."""
    return await _wrap(lambda: modules.inject_node(client, node_id))


# ---- HA awareness ----
@mcp.tool()
async def ha_list_ha_nodes() -> str:
    """List Home Assistant nodes in the flows with their domain/service/entity."""
    return await _wrap(lambda: ha.list_ha_nodes(client))


@mcp.tool()
async def ha_get_server_config() -> str:
    """List the Home Assistant server config nodes Node-RED uses."""
    return await _wrap(lambda: ha.get_server_config(client))


@mcp.tool()
async def ha_validate_calls() -> str:
    """Validate api-call-service nodes against live HA (requires HA_URL + HA_TOKEN)."""
    if not settings.ha_url or not settings.ha_token:
        return _err("HA validation needs HA_URL and HA_TOKEN to be set.")
    ha_client = ha.HARestClient(settings)
    try:
        return await _wrap(lambda: ha.validate_calls(client, ha_client))
    finally:
        await ha_client.close()


def main():
    """Console-script entry point."""
    transport = "stdio" if settings.mcp_transport == "stdio" else "sse"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_smoke.py -v`
Expected: PASS. If the `mcp` build exposes tools via a different attribute, update only the
accessor (per the note) — the expected name set is authoritative.

- [ ] **Step 5: Full suite + commit**

```bash
pytest -q          # expect: all green
git add src/nrha_mcp/server.py tests/test_server_smoke.py
git commit -m "feat(server): FastMCP wiring for all tools (confirm-gated mutations, HA tools)"
```

---

## Phase 10 — Manual stdio verification (real Node-RED, optional but recommended)

### Task 10.1: Smoke-run against a throwaway Node-RED

**Files:**
- Create: `docker-compose.dev.yml`

- [ ] **Step 1: `docker-compose.dev.yml`** (disposable Node-RED for manual testing)

```yaml
services:
  nodered:
    image: nodered/node-red:latest
    ports:
      - "1880:1880"
    volumes:
      - nr_data:/data
volumes:
  nr_data:
```

- [ ] **Step 2: Start it and create a sample flow in the UI**

```bash
docker compose -f docker-compose.dev.yml up -d
# open http://localhost:1880, drop an inject -> debug, deploy
```

- [ ] **Step 3: Run the server against it and exercise read + propose**

```bash
NR_URL=http://localhost:1880 DEPLOY_MODE=propose node-red-homeassistant-mcp
```

Connect your MCP client (or use the MCP Inspector) and verify:
- `nr_get_flow_summary` returns the tab.
- `nr_safe_deploy(node_id=..., fields={"name":"x"})` returns `deployed:false` + a preview.
- The same call with `confirm=true` returns `deployed:true`, writes a file under `./backups/`,
  and the change shows in the Node-RED UI without tab reordering.

- [ ] **Step 4: Tear down**

```bash
docker compose -f docker-compose.dev.yml down -v
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.dev.yml
git commit -m "chore: disposable Node-RED compose for manual verification"
```

---

## Self-Review (completed during planning)

**Spec coverage:** client (§5.1, Phase 2), safe-deploy gate (§5.2/§8, Phase 3), backup store
(§5.3, Phase 4), HA-awareness (§5.4, Phase 8), tool layer (§5.5/§6, Phase 9), config (§9, Phase 1),
read tools incl. `fields`+global-context and write tools incl. `safe_deploy_many`/`add_wire`
(§6 + PR #3, Phases 5–6), testing strategy (§11 — unit via FakeNRClient/respx; integration in
Phase 10). **Transports/packaging (§5.6, §10): deferred to Plan B (Docker + HA add-on)** — Phase 9
`main()` already branches on `MCP_TRANSPORT` so Plan B only adds the image/add-on wrappers.

**Placeholder scan:** none — every code/test step contains real content.

**Type/name consistency:** `commit(client, store, settings, *, rebuild, summary, confirm)` used
identically in Phases 3/6; `BackupStore.snapshot(flows, label)->Path` matches the StubStore
contract in Phase 3 and real use in Phase 6; tool names in Phase 9 match the smoke-test set and
the spec §6.

**Open item carried from spec §7:** Node-RED add-on Admin API auth — verified manually in Phase 10
/ resolved when pointing at the real instance.
