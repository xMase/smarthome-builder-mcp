# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastMCP server (`mcp` SDK) that lets an AI assistant read, author, and deploy **Node-RED** flows
through Node-RED's Admin API, with **Home Assistant awareness** and a **safe, backup-first deploy**
model. Python 3.11+, packaged with hatchling. Ported from `nr-mcp` (MIT); see `NOTICE`.

## Commands

```bash
# Dev install (project uses a .venv; uv preferred, plain venv also fine)
uv venv --python 3.12 .venv && uv pip install -e ".[dev]"

# Unit tests — mocked Admin API, no network
.venv/bin/pytest -q
.venv/bin/pytest tests/test_flows.py::test_get_flow_by_name -v   # single test

# End-to-end smoke test over the real stdio transport against a disposable Node-RED
docker compose up -d nodered          # throwaway Node-RED on :1880
NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py
docker compose down -v

# Run the server directly
.venv/bin/node-red-homeassistant-mcp                 # stdio (default)
MCP_TRANSPORT=http .venv/bin/node-red-homeassistant-mcp   # streamable-HTTP at /mcp

# Full local stack (MCP HTTP service + throwaway Node-RED); reads ./.env
docker compose up -d --build
```

There is no configured linter/formatter in `pyproject.toml`; `pytest` is the only check. Tests are
`pytest-asyncio` in `asyncio_mode = "auto"` — `async def test_*` needs no decorator beyond the
existing `@pytest.mark.asyncio` already used in the suite.

## Architecture

Three layers, each in `src/nrha_mcp/`:

1. **`server.py` — tool surface.** The *only* place MCP tools are declared (`@mcp.tool()`). Each tool
   is a thin wrapper that delegates to a domain-module function through `_wrap(...)`, which runs the
   coroutine and serializes the result to a JSON string (`_ok`/`_err`). Domain functions signal
   failure by raising `NRError` subclasses (`NRAuthError`, `NRConflictError`, `NRNotFoundError`);
   `_wrap` maps those to structured `{"error": ...}` JSON. Tools never touch HTTP directly.

2. **Domain modules** — pure-ish logic, each owning one area: `flows.py` (read/search/context),
   `edit.py` (node create/patch/wire/delete), `deploy.py` (the deploy safety gate), `backup.py`
   (`BackupStore`: snapshot/list/restore), `modules.py` (palette install), `ha.py` (HA-node
   recognition + a *separate* `HARestClient` for the HA REST API at `:8123`).

3. **`client.py` — `NRClient`.** The single path to Node-RED's Admin API (httpx, API v2). Owns auth
   (precedence: `NR_TOKEN` → `NR_USER`/`NR_PASS` per `NR_AUTH` → none) and optimistic locking
   (`GET → POST /flows` with `rev`). `config.py` holds `Settings` (pydantic-settings, env / `.env`).

### The deploy gate (the most important pattern)

Every mutating operation funnels through `deploy.commit(client, store, settings, rebuild=..., summary=..., confirm=...)`:

- The caller passes a **`rebuild(flows) -> proposed_flows` closure that MUST be pure** (deep-copy,
  never mutate its input). `commit` calls it, diffs old vs proposed, and:
  - in `propose` mode without `confirm=true` → returns the diff preview and writes nothing;
  - otherwise → snapshots a backup, then `POST`s with optimistic locking; on `NRConflictError` it
    re-fetches fresh flows, **re-runs `rebuild`**, re-snapshots, and retries once.
- The closure is re-run on the conflict path, which is *why* it must be pure. When adding a mutating
  tool, follow `edit.py`'s pattern: build a `rebuild` closure and hand it to `commit` — do not write
  flows yourself.

So default behavior is a two-call loop: call a tool → read the preview → call again with
`confirm=true`. A backup is always written before any real change.

## Conventions & gotchas

- **The registered tool set (and its exact count) is asserted in TWO places.** When you add/remove/rename a tool you
  must update all three: the `@mcp.tool()` in `server.py`, the `expected` set **and** the
  `len(names) == N` count in `tests/test_server_smoke.py`, and `EXPECTED_TOOLS` in
  `scripts/smoke_e2e.py`. Missing one breaks the smoke test.
- **Authoring HA nodes:** resolve `entity_id` via `ha_search_entities` first; reuse an existing
  source node when one already provides the data; to build a new HA node, clone an existing node of
  the same type via `nr_search_nodes`; avoid deprecated fields `state_type`/`stateType` (use
  `outputProperties`); get the HA server config id via `ha_get_server_config`. This guidance is also
  baked into the server's MCP `instructions` string.
- **`NR_AUTH` matters:** `basic` is correct for the **Home Assistant Node-RED add-on** (nginx handles
  auth before Node-RED); `token` is for vanilla Node-RED with `adminAuth` (exchanges credentials at
  `POST /auth/token`). The HA REST tools (`ha_*`) are separate and need `HA_URL` (`:8123`) +
  `HA_TOKEN`; they no-op with a clear message if those are unset.
- **Tests use `FakeNRClient`** (`tests/conftest.py`), an in-memory stand-in with the same method
  contract as `NRClient`. Unit tests call domain-module functions directly with the fake — no
  network. Add new client methods to the fake when you extend `NRClient`.

## Project workflow

This repo is developed with the **superpowers** spec→plan→implement workflow. Design specs live in
`docs/superpowers/specs/` and implementation plans in `docs/superpowers/plans/` (dated
`YYYY-MM-DD-*`). Read the latest ones for in-flight or recently completed work before making changes.
