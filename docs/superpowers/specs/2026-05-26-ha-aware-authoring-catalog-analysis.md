# HA-aware authoring & node-schema catalog — design analysis (milestone "C")

- **Date:** 2026-05-26
- **Status:** Analysis / deferred milestone (NOT yet implemented)
- **Context:** follow-up to v1. Captures the design reasoning so it can be picked up later.

---

## 1. Background — the two problems observed in real use

When asked to author a flow ("bathroom humidity > 80% → debug message"), two issues surfaced:

1. **Slow discovery.** The MCP only knows what is *inside* Node-RED, so Claude had to reverse-engineer
   the entity (`sensor.bagno_humidity`) by reading several nodes. → **Fixed in v1.x** by adding
   `ha_search_entities` / `ha_get_state` (HA REST entity discovery) — commit `5604faa`.
2. **Deprecated node schema.** Claude built a `server-state-changed` node using the deprecated
   `state_type`/`stateType` field instead of `outputProperties`, because there was **no local node of
   that type to clone** and its training data carried the old schema (`State type is deprecated and
   will be removed in version 1.0`). → **Mitigated (best-effort)** in v1.x by authoring *guidance*
   (FastMCP `instructions=` + enriched `nr_create_nodes` docstring): "reuse existing source nodes;
   clone the schema of an existing same-type node; avoid `state_type`, use `outputProperties`."

**Milestone C is the durable fix for problem #2**: reliably produce the *current, correct* schema for
an HA node type **even when no local example exists** — turning the best-effort guidance into a
near-guarantee.

## 2. Goal of C

Given an HA node type (e.g. `server-state-changed`), provide its **current, non-deprecated** field
schema/template, and flag node fields that are not part of that current schema (catches `state_type`).

## 3. The crux — where does the authoritative schema come from?

Three candidate sources, ranked:

| # | Source | Pros | Cons |
|---|--------|------|------|
| 1 | **Clone an existing node** of the same type from the user's flows | version-accurate to their install; zero cost | **fails for absent types** (the exact gap) |
| 2 | **Introspect the installed package at runtime** — `GET /nodes/<module>/<set>` (Accept: text/html) returns the editor JS with `RED.nodes.registerType('<type>', { defaults })` | authoritative, version-accurate, self-updating | **fragile runtime parsing** of a JS object literal (functions/validators, minification, version drift) |
| 3 | **Hand-curated catalog** baked into nrha | simple, no parsing | drifts; manual maintenance; partial coverage |

## 4. Chosen approach — build-time generated catalog (best of 2 & 3)

Move the fragile parsing **out of the runtime hot path** into an **offline, build-time generator**:

```
BUILD TIME (maintainer-run, not shipped to users):
  scripts/generate_ha_catalog.*  →  src/nrha_mcp/ha_catalog.json   (committed)
    1. fetch node-red-contrib-home-assistant-websocket@<pinned version> (npm tarball / GitHub tag)
       — the npm package ships the built editor resources containing the registerType(defaults).
    2. extract each node type's `defaults`.
    3. emit ha_catalog.json:  { type -> { fields, defaults, source_version } }

RUNTIME:
  nr_get_node_template(type):              # tool surface
    1. clone:      existing node of that type in the flows  -> sanitized template   (preferred; version-accurate)
    2. catalog:    ha_catalog.json lookup                    -> template for absent types  (no runtime parsing)
    3. fallback:   explanatory error / raw block
  + schema-lint in the deploy gate: for HA node types, warn in the `propose` preview about
    fields NOT present in the current schema (e.g. `state_type`) -> the anti-deprecation guarantee.
```

Why better than the alternatives: runtime is a trivial JSON lookup (fast, deterministic, testable);
not hand-maintained (regenerated from source); covers all node types.

## 5. Extracting the `defaults` in the generator (ranked by robustness)

- **G-exec** *(most accurate)*: in a Node script, stub `RED.nodes.registerType` and **load** the
  editor script → capture the real `defaults` object with no parsing (even computed defaults).
  Needs some browser-env stubbing; degrade per-node on failure.
- **G-ast**: parse the editor JS with `acorn`/`esprima`, extract the `defaults` AST. Robust, no execution.
- **G-regex**: brace-aware extraction. Simplest, least robust — acceptable **because it's offline + tested**.

Plan: prototype **G-exec**, fall back to **G-ast**. (Node/npx already available in the dev env.)
Build the generator **TDD against the real editor artifact** captured from an actual Node-RED, not an
idealized sample. Never let a parse failure block — fall back gracefully.

## 6. Caveat — version skew (catalog vs the user's installed package)

The catalog reflects the version it was generated from; the user's Node-RED may differ. Mitigations:
1. **Prefer clone-from-existing** at runtime (accurate to their install); the catalog is only the
   *fallback* for absent types (which the user likely hasn't customized anyway).
2. At runtime read the installed package version (present in `get_nodes()`) and **warn** if it
   diverges materially from the catalog's `source_version`. Cheap and honest.

Licensing: the contrib package is Apache-2.0 — record `source_version` in the catalog and add
attribution in `NOTICE`.

## 7. Keeping the catalog fresh — CI auto-refresh (later, with publication)

Idea: when upstream publishes a new **release**, regenerate the catalog automatically.

- Track **releases** (npm `latest` / GitHub tags), **not** raw pushes. No cross-repo webhooks →
  **scheduled polling** (e.g. weekly cron) is fine; schema changes aren't urgent.
- **Auto-PR, NOT auto-publish.** Open a PR with the regenerated catalog; a human merges. Rationale:
  upstream changes can be semantically meaningful (renames/deprecations) that may need the lint
  messaging/templates adjusted; the generator could break on an upstream restructure (fail loudly,
  don't ship a corrupt catalog). Standard Dependabot/Renovate pattern.
- **Bonus:** the catalog **diff in the PR** shows exactly which fields were added/removed per node
  type — i.e. you *see* upstream deprecations the moment they happen (e.g. `state_type` disappearing).

Sketch:
```
.github/workflows/refresh-ha-catalog.yml   (on: schedule + workflow_dispatch)
  - query npm latest of the contrib package
  - compare to ha_catalog.json source_version
  - if newer: run scripts/generate_ha_catalog.* ; if catalog changed -> pytest -> open PR
  release.yml (on tag/merge) -> publish (PyPI / GHCR)
```
Elegant variant: model the contrib package as a pinned **devDependency** and let **Renovate/Dependabot**
open the bump PR; a CI hook regenerates the catalog in the same PR.

## 8. Sequencing & prerequisites

This automation sits **on top of** things that don't exist yet:
1. the **generator + `ha_catalog.json`** (C2′) — nothing to refresh without it;
2. the repo on **GitHub** with CI + publish secrets — i.e. **publication (Plan C)**, currently deferred.

So the CI auto-refresh is step **N+2**.

## 9. Recommended order

1. **C1** — `nr_get_node_template` with clone-from-existing (low risk, immediate value).
2. **C2′** — build-time generator + `ha_catalog.json` + runtime lookup (the real work, but offline/testable).
3. **C3** — schema-lint in the deploy gate using the catalog (the anti-deprecation guarantee).
4. **Publication (Plan C)** — GitHub repo + PyPI/image release.
5. **CI auto-refresh** — auto-PR on upstream release (natural once 2 + 4 exist).

## 10. When to actually do this

Only if real use shows the shipped v1.x mitigation (`ha_search_entities` + authoring guidance) is
**not enough** — i.e. Claude still emits deprecated schemas for node types absent from the user's
flows. If "1+2" proves sufficient in practice, C may never be needed. Re-evaluate after the next
real authoring test against a node type with no local example.
