---
title: "PER-26: Refactor FastAPI Routes for Plugin Registration"
type: refactor
status: active
date: 2026-05-18
deepened: 2026-05-18
origin: docs/refactor-split-handoff.md
---

# PER-26: Refactor FastAPI Routes for Plugin Registration

## Overview

Separate comic-dedicated routes from the core `server/main.py` so the eventual extraction in PER-30 has a clear seam. PER-25 already shipped the `register_routes(app)` plugin hook; PER-26 reorganizes the existing routes so the comic-dedicated ones live in their own module and the lifespan-plumbing for plugins moves into `gixen/plugins.py` as underscore-prefixed internal helpers.

This is a structural refactor with **no behavior changes**. All 17 existing routes return identical responses; all existing tests pass unchanged.

## Problem Frame

`server/main.py` is a 1424-line monolith that mixes three concerns:

1. **Generic snipe routes** that must stay in the core forever (`/health`, `/api/sync`, `/api/purge`, `DELETE /api/bids/{item_id}`, dashboard statics).
2. **Comic-dedicated routes** that will move whole to the comic-pipeline plugin in PER-30 (`/v2/comics`, `/api/comics` GET+POST, `/api/bids/{item_id}/comics/locg`, `/api/extract-comics`).
3. **Comic-contaminated routes** that stay in core but currently inline-call `upsert_comic` or `LEFT JOIN comics` (`POST /api/bids`, `GET /api/snipes`, `GET /api/history`, `GET /api/bids`, `PATCH /api/bids/{item_id}`). PER-30 will eventually decontaminate these via a response-enrichment hook — out of scope here.

Today every route is decorated directly on the `FastAPI()` instance with no `APIRouter` in sight, so the comic-dedicated routes are visually indistinguishable from generic ones. PER-26's job is to make that distinction structural: comic-dedicated routes live in their own `APIRouter` in a dedicated module file, included on `app` via `app.include_router(...)`. PER-30 then moves that module to the comic-pipeline plugin and removes the one `include_router` line from core.

PER-25's review surfaced an M-01 maintainability finding: the per-plugin SAVEPOINT loop and bulk-hook plumbing in `server/main.py` lifespan is application-layer code that should live in `gixen/plugins.py`. PER-26 is the natural moment to fix this — the lifespan is already being touched to set up `app.state` for the extracted routes. The M-01 cleanup ships in the same PR but as a separate commit so bisect can isolate route changes from lifespan-plumbing changes.

(see origin: `docs/refactor-split-handoff.md` — sections "Current State of This Repo", "Target End State", "The Work Plan / Phase 1 / PER-26")

## Trajectory Note

PER-26 deliberately does **not** progress the broader refactor's end-state goal of "a non-comic Gixen user never sees the word comic in core." After PER-26:

- `AddBidRequest` still has 11 comic-optional fields.
- `GET /api/snipes`, `GET /api/history`, `GET /api/bids` still `LEFT JOIN comics` and return comic columns in every row.
- `server/static/index.html` still renders comic-specific UI (gradeLabel, fmtFmv, fmv columns).
- The word "comic" still appears throughout core Pydantic models, core SQL, and the core dashboard.

This is by design. The 5 comic-dedicated routes have a clean seam today (zero callers in core, no shared models, no JOINs); PER-26 extracts them. The 5 contaminated routes do not have a clean seam — they require a response-enrichment hook whose design depends on what the comic plugin's request/response surface actually needs, which is only knowable when PER-30 builds it. PER-26 explicitly does not invent that hook speculatively. The cost is that PER-30 retains its "highest-risk Issue" status from the handoff.

If this trade-off needs to be revisited (e.g., merge PER-26 + PER-30 into one larger Issue), that decision should be made before PER-26 lands, not after.

## Requirements Trace

- **R1.** All 17 existing routes return identical responses before and after the refactor (handoff invariant — confirmed by `tests/test_server_api.py`).
- **R2.** Comic-dedicated routes (5) live in `server/comic_routes.py` and are mounted via `app.include_router(...)`. After PER-30, removing one `include_router` call removes those routes from the core.
- **R3.** Comic-dedicated Pydantic models (`UpsertComicRequest`, `LocgLinkRequest`) move with the routes that consume them.
- **R4.** The `register_routes(app)` hook from PER-25 keeps working — plugins can still mount routes on the same `app`. Existing plugin integration tests (`tests/test_plugin_integration.py`) pass unchanged, including all PER-25 regression tests (ADV-001/ADV-002/REL-01/COR-01) whose `caplog.set_level(..., logger="server.main")` assertions must still capture the cleanup error logs.
- **R5.** Shared state needed by extracted routes (the SQLite connection) is exposed via `app.state.db`. Lifespan ordering is pinned: `app.state.db` is set **before** `load_plugins()` runs and is **cleared on teardown** alongside `_db.close()`.
- **R6.** Lifespan-plumbing for plugin invocation (SAVEPOINT loop, bulk hook wrappers, tab flattening) moves into `gixen/plugins.py` as underscore-prefixed internal helpers. The lifespan in `server/main.py` reads as a sequence of named calls, not inline pluggy machinery.
- **R7.** A regression test catches duplicate `(method, path)` route pairs in `app.routes` — the kind of silent shadowing FastAPI never warns about. No pinned route-count manifest (too brittle during the mid-refactor period of PER-27..PER-37).

## Scope Boundaries

**In scope:**
- Extract 5 comic-dedicated routes into `server/comic_routes.py` (single file, matching the existing flat `server/` layout).
- Move `UpsertComicRequest` and `LocgLinkRequest` into the same module.
- Expose the SQLite connection via `app.state.db` in the lifespan, with explicit lifecycle ordering pinned.
- Extract plugin lifespan plumbing into `gixen/plugins.py` as underscore-prefixed helpers (PER-25 M-01 finding).
- Add `tests/test_route_organization.py` with a single duplicate-`(method, path)` regression test.

**Out of scope (explicit non-goals):**
- Decontaminating `/api/snipes`, `/api/history`, `/api/bids`, `POST /api/bids`, `PATCH /api/bids/{item_id}`. These routes currently `LEFT JOIN comics` or inline-call `upsert_comic` and stay that way in PER-26. **No** `enrich_snipes(rows)` hook, **no** Pydantic-model-extension mechanism. Both are explicitly deferred to PER-30 by the PER-25 plan and the handoff.
- Splitting `AddBidRequest` / `EditBidRequest` to remove their comic fields. Deferred to PER-30 alongside the model-extension hook design.
- Adding a `register_cli_commands(group: click.Group)` hook. Out of scope — PER-26 only touches server routes.
- Refactoring `server/main.py` helpers (`_sync_gixen`, `_ensure_fresh_sync`, `_run_ebay_fallback`, etc.) into a separate `server/runtime.py` module. Generic helpers stay on the module while only the extracted comic routes need shared state — and they only need the DB connection, available via `app.state.db`. Not worth the churn for PER-26.
- Touching `server/static/index.html` (comic-contaminated UI). That's PER-28's territory.
- Adding TODO comments at JOIN sites referencing PER-30. The handoff and the PER-30 plan are the durable record; in-code TODOs would rot.
- Adding router-level dependencies, middleware, or exception handlers to the comic router. PER-26 introduces an `APIRouter` to organize routes; it must not introduce divergent middleware behavior between routes-on-router and routes-on-app. Any future middleware/handler additions must be applied uniformly via `app`, not via the comic router.
- A pinned 17-route manifest test. The repo is mid-refactor; such a manifest would fire constantly on legitimate route additions across PER-27..PER-37. The duplicate-pair check from R7 catches the real bug (silent shadowing); a path manifest catches nothing during active refactor work.

### Deferred to Separate Tasks

- **Response-enrichment hook** (`enrich_snipes`, `enrich_history`, etc.): PER-30. Surfaces only when the comic plugin tries to re-inject comic columns into `/api/snipes` responses.
- **Pydantic model extension** for `AddBidRequest` / `EditBidRequest`: PER-30. The 11 comic-optional fields stay where they are until PER-30 designs the extension mechanism.
- **CLI command registration hook**: PER-30 / `comic-pipeline` design. Separate concern from server routes.
- **Index.html decontamination**: PER-28. The dashboard refactor is its own scope.

## Context & Research

### Relevant Code and Patterns

**Repository facts (verified via repo-research-analyst):**

- Single `FastAPI()` instance at `server/main.py:699`; no `APIRouter` anywhere in the codebase. PER-26 introduces the first one.
- Lifespan is at `server/main.py:567-699`; the PER-25 plugin loader runs there at lines 587 (`load_plugins()`), 593-621 (per-plugin SAVEPOINT loop for `register_db_tables`), 624-629 (bulk `pm.hook.register_routes(app=app)`), 635-654 (bulk `register_dashboard_tabs` with flattening), 658 (`app.openapi_schema = None`).
- Lifespan logger emission uses `logger = logging.getLogger(__name__)` in `server/main.py:49` — logger name is `"server.main"`. PER-25 regression tests in `tests/test_plugin_integration.py` (lines 186, 262, 310, 345) use `caplog.set_level(logging.ERROR, logger="server.main")` to capture these. R4 requires this behavior be preserved.
- Static files are served via per-file `FileResponse` handlers (no `StaticFiles` mount). `GET /`, `GET /v2/bids`, `GET /v2/comics`, `GET /static/v2.css` each have a dedicated handler.
- The `_db` SQLite connection and `_api_client` Gixen wrapper are module-level globals in `server/main.py`, initialized in the lifespan and closed on shutdown (`server/main.py:696`). PER-25 already set `app.state.plugin_manager` and `app.state.dashboard_tabs` — the same pattern applies for `app.state.db`.
- The handoff doc claims the edit route is `PUT /api/bids/{item_id}`. **Reality**: it's `PATCH`. Verified at `server/main.py:1126` (`@app.patch`). PER-26 should not normalize this; existing CLI thin-client mode and any downstream consumers depend on the verb. The handoff doc itself will be corrected as part of PER-26's documentation updates.

**Route inventory (17 routes, ordered by source position in `server/main.py`):**

| # | Method | Path | Handler | Line | Classification | Pydantic | Needs DB? |
|---|---|---|---|---|---|---|---|
| 1 | GET | `/` | `root` | 810 | Generic | — | No |
| 2 | GET | `/v2/comics` | `variant_v2_comics` | 818 | **Comic-dedicated** | — | **No (static file)** |
| 3 | GET | `/v2/bids` | `variant_v2_bids` | 826 | Generic | — | No |
| 4 | GET | `/static/v2.css` | `static_v2_css` | 834 | Generic | — | No |
| 5 | GET | `/health` | `health` | 843 | Generic | — | No |
| 6 | GET | `/api/comics` | `api_list_comics` | 848 | **Comic-dedicated** | — | **Yes** |
| 7 | POST | `/api/comics` | `api_upsert_comic` | 860 | **Comic-dedicated** | `UpsertComicRequest` | **Yes** |
| 8 | POST | `/api/bids` | `api_add_bid` | 881 | Contaminated (stays) | `AddBidRequest` | Yes |
| 9 | GET | `/api/snipes` | `api_get_snipes` | 929 | Contaminated (stays) | — | Yes |
| 10 | GET | `/api/history` | `api_get_history` | 1024 | Contaminated (stays) | — | Yes |
| 11 | GET | `/api/bids` | `api_get_all_bids` | 1087 | Contaminated (stays) | — | Yes |
| 12 | PATCH | `/api/bids/{item_id}` | `api_edit_bid` | 1126 | Contaminated (stays) | `EditBidRequest` | Yes |
| 13 | POST | `/api/bids/{item_id}/comics/locg` | `api_link_locg` | 1185 | **Comic-dedicated** | `LocgLinkRequest` | **Yes** |
| 14 | DELETE | `/api/bids/{item_id}` | `api_remove_bid` | 1276 | Generic | — | Yes |
| 15 | POST | `/api/sync` | `api_sync` | 1293 | Generic | — | Yes |
| 16 | POST | `/api/purge` | `api_purge` | 1302 | Generic | `PurgeRequest` | Yes |
| 17 | POST | `/api/extract-comics` | `api_extract_comics` | 1345 | **Comic-dedicated** | — | **Yes** |

The 5 **bold** rows move to `server/comic_routes.py`. Of those, only `GET /v2/comics` does not access the DB. Everything else stays where it is.

**Existing test coverage** (`tests/test_server_api.py`, `tests/test_plugin_integration.py`): the comic routes are exercised by route-specific tests and by integration tests that hit the lifespan. PER-26 must not break either.

**Patterns to mirror:**

- `tests/test_plugin_integration.py:101-122` — the `register_routes` integration test already demonstrates the `APIRouter` + `app.include_router(router)` pattern. PER-26's comic router follows the same shape.
- `gixen/plugins.py:113-161` (`load_plugins`) — already uses per-name iteration with a top-level try/except. The M-01 helpers PER-26 adds will follow the same logging conventions, but will accept an injected logger so the lifespan can pass `logging.getLogger("server.main")` and preserve existing test expectations.
- PER-25 plan (`docs/plans/2026-05-18-001-feat-plugin-entry-point-system-plan.md`) — same author, same conventions. Match its structure, docstring style, and test-file naming.

### Institutional Learnings

- **No `docs/solutions/` directory exists in this repo yet.** PER-25 also flagged this gap. Worth capturing PER-26's APIRouter-extraction pattern in a solution doc after PER-30 lands, when we know whether the chosen seam survived contact with the actual extraction.
- **PER-25 review M-01**: "subset_hook_caller loop belongs in gixen/plugins.py, not the lifespan." Captured by Unit 3 of this plan.
- **PER-25 review (kieran-python RR-2)**: bulk `pm.hook.register_routes(app=app)` halts the chain on the first failing plugin — deliberate loud-failure design. PER-26 preserves this contract; the M-01 cleanup must not change semantics.
- **PER-25 review (api-contract residual)**: FastAPI silently accepts duplicate `(method, path)` declarations. Last-write-wins. Captured by Unit 4 of this plan via a duplicate-pair test.
- **PER-25 review (adversarial ADV-001 / reliability REL-01 / correctness COR-01)**: `conn.executescript()` implicitly commits and destroys SAVEPOINTs. Fixed in PER-25 — the savepoint-cleanup try/except is in `server/main.py:608-619`. PER-26's M-01 helper must preserve this defensive wrapping AND must continue to emit cleanup-failure logs under the `"server.main"` logger so the existing regression tests catch them.

### External References

External research skipped: FastAPI `APIRouter` and `app.include_router(...)` are first-party patterns we already used in PER-25 integration tests. The codebase has 3 direct usages in tests; no need to consult external docs.

## Key Technical Decisions

- **Comic router lives in a single file `server/comic_routes.py`**: matches the existing flat layout (`server/main.py`, `server/db.py`, `server/title_parser.py`). Avoids creating a `server/routes/` package with one module; sidesteps the Hatchling nested-package discovery question; eliminates the `__init__.py` re-export design choice. If a second internal route group ever materializes (no current candidate), it can be promoted to a package then.
- **Comic-dedicated Pydantic models move with their routes**: `UpsertComicRequest` and `LocgLinkRequest` go into `server/comic_routes.py` directly. They have no consumers outside their route. PER-30 picks them up in the same `git mv`.
- **Shared state via `app.state.db`, with pinned lifecycle**: matches the PER-25 pattern (`app.state.plugin_manager`, `app.state.dashboard_tabs`). The lifespan sets `app.state.db = _db` **immediately after `init_db(...)` returns and before `load_plugins()` runs**, so plugin `register_db_tables` hooks can rely on it. On teardown, the lifespan calls `_db.close()` and clears `app.state.db = None` to prevent any post-teardown code from holding a closed connection.
- **Generic routes stay on `app` in `main.py`**: not worth the churn to extract them. The handoff's target end state ("main.py: FastAPI app, generic routes only") is satisfied as soon as the comic-dedicated routes leave.
- **Contaminated routes stay whole-cloth in `main.py`**: PER-30 owns the response-enrichment hook design. PER-26 must not invent it speculatively.
- **No in-code TODOs at JOIN sites**: the handoff and PER-30 plan are the durable record. In-code TODOs rot; the project's `CLAUDE.md` says default to no comments.
- **No router-level middleware, dependencies, or exception handlers**: PER-26 introduces `APIRouter` as an organizational seam, not a middleware seam. Any future middleware/handler work must apply to `app` directly so all 17 routes get consistent treatment.
- **`include_router(comic_router)` is called at module level in `main.py`, not inside the lifespan**: matches FastAPI's blessed pattern. Lifespan-time `include_router` (which plugins do via the hook) is reserved for plugins only. `server/comic_routes.py` must have zero import-time side effects — no DB connection, no env reads — so `from server.comic_routes import router as comic_router` is safe regardless of whether env vars are set.
- **M-01 helper names and visibility**: underscore-prefixed module-level functions in `gixen/plugins.py`: `_invoke_db_tables_isolated(pm, conn, *, logger)`, `_invoke_register_routes(pm, app, *, logger)`, `_collect_dashboard_tabs(pm, *, logger)`. The leading underscore signals "host-side internal, not part of the plugin contract" even though Python's `__all__` does not enforce that. Each helper requires an injected `logger: logging.Logger` (keyword-only) so the lifespan can pass `logging.getLogger("server.main")` and preserve existing `caplog` test expectations.

## Open Questions

### Resolved During Planning

- **Q: Should generic routes also move out of `main.py`?**
  A: No. Only comic-dedicated routes need to be portable. Generic routes are at home in `main.py`. (Rationale: minimum churn, maximum clarity of intent.)
- **Q: Should `_db`, `_api_client`, helpers move to `server/runtime.py`?**
  A: No for PER-26. Comic-dedicated routes only need the DB connection. `app.state.db` covers them. If PER-30 needs more shared-state for the plugin (Gixen client, helpers), it can either expand `app.state.*` or introduce `server/runtime.py` at that point.
- **Q: Should `PATCH /api/bids/{item_id}` be normalized to `PUT`?**
  A: No. The handoff doc has a documentation error claiming PUT. The actual route is PATCH (`server/main.py:1126`). Existing CLI thin-client and any other consumers expect PATCH. PER-26 does not change verbs.
- **Q: Does `app.include_router(comic_router)` need to fire before or after the plugin loader's `pm.hook.register_routes(app=app)`?**
  A: Before. Core routes — including the comic router included in `main.py` at module level — register at import time. The lifespan's `register_routes(app=app)` call fires later, registering plugin routes. Last-write-wins means a future plugin could shadow `/api/comics` if it wanted, but that's the same as today; PER-26 does not change this contract.
- **Q: Should the M-01 cleanup change error handling semantics?**
  A: No. PER-25's behavior is preserved exactly: per-plugin SAVEPOINT with inner try/except for cleanup; bulk routes hook halts on first failure; bulk tabs hook flattens with `isinstance(x, list)` guard. The M-01 helpers' logger emission also keeps the `"server.main"` logger name (via injected logger) so existing caplog tests pass unchanged.
- **Q: Should `server/routes/` be a package?**
  A: No. Single file `server/comic_routes.py` matches the existing flat layout. Avoids Hatchling nested-package concerns and the `__init__.py` re-export ambiguity.
- **Q: When is `app.state.db` valid?**
  A: From the moment `init_db(...)` returns inside the lifespan, up until the lifespan teardown clears it. Plugins' `register_db_tables` hooks can rely on it being set. After teardown, it is explicitly `None`.

### Deferred to Implementation

- **Exact import path for the comic router in `main.py`**: `from server.comic_routes import router as comic_router` (single file makes this unambiguous).
- **Whether to use `Depends(get_db)` or `request.app.state.db`** for accessing the DB connection inside comic route handlers: both work. Pick the one with the cleanest signature. Recommendation: `request.app.state.db` to minimize new abstractions, since we don't have any other `Depends`-style providers in the codebase yet. (Note for PER-30: the comic plugin may want to introduce a `Depends` provider when these routes leave this repo. That's PER-30's call.)

## Implementation Units

- [ ] **Unit 1: Expose DB connection on `app.state.db` with pinned lifecycle**

**Goal:** Make the SQLite connection accessible to route handlers in any module via `request.app.state.db`. Pin the lifecycle: set immediately after `init_db(...)` returns, cleared on teardown.

**Requirements:** R5

**Dependencies:** None

**Files:**
- Modify: `server/main.py`
- Modify: `gixen/plugins.py` (docstring update for `register_db_tables` hookspec — document that `app.state.db` is available during this hook)
- Test: `tests/test_server_api.py` (extend with one assertion), `tests/test_plugin_integration.py` (verify `app.state.db` is reachable from a test plugin's route AND from a test plugin's `register_db_tables` hook; verify it is `None` after lifespan teardown)

**Approach:**
- In the lifespan, after `_db = init_db(...)`, immediately set `app.state.db = _db`. This must happen **before** `pm = load_plugins()` and before the per-plugin `register_db_tables` hooks fire, so plugin DDL implementations can access `app.state.db` if they want.
- Keep the module-level `_db` global for existing handlers — they keep working unchanged.
- On lifespan teardown (in the `finally` or after `yield`), after `_db.close()`, also set `app.state.db = None` so any code that mistakenly holds a reference past teardown sees a clear failure rather than acting on a closed connection.
- Only `app.state.db` is exposed. `_api_client`, `_api_lock`, and the other module-level globals stay where they are — none of the routes being extracted in Unit 2 access them.
- Update the `register_db_tables` hookspec docstring in `gixen/plugins.py` to document that `app.state.db` is set and points at the same connection passed as the `conn` argument.

**Patterns to follow:**
- `server/main.py` already sets `app.state.plugin_manager` (PER-25, line 588) and `app.state.dashboard_tabs` (PER-25, around line 654). Match that style.

**Test scenarios:**
- Integration: `client.app.state.db` is a `sqlite3.Connection` after lifespan startup. Same connection as `server.main._db`.
- Integration: A test plugin's route can do `request.app.state.db.execute("SELECT 1").fetchone()` inside its handler.
- Integration: A test plugin's `register_db_tables` hookimpl can read `app.state.db` (e.g., via `pm._app.state.db` or by stashing `app` via an earlier hook) and confirm it points at the same connection as the `conn` parameter.
- Edge case: After exiting the TestClient context manager, `client.app.state.db` is `None`. Asserts the teardown cleanup ran.

**Verification:**
- All existing tests pass unchanged.
- New assertions confirm `app.state.db` is reachable from inside a route handler context, available during plugin DB hooks, and cleared on teardown.

---

- [ ] **Unit 2: Extract comic-dedicated router to `server/comic_routes.py`**

**Goal:** Move all 5 comic-dedicated routes plus their Pydantic models out of `server/main.py` and into a new `server/comic_routes.py`. `main.py` mounts them via `app.include_router(...)`.

**Requirements:** R1, R2, R3

**Dependencies:** Unit 1

**Files:**
- Create: `server/comic_routes.py` (single file, no `__init__.py`, no subpackage)
- Modify: `server/main.py` (remove the 5 route decorators, remove `UpsertComicRequest` + `LocgLinkRequest`, add `from server.comic_routes import router as comic_router` and `app.include_router(comic_router)`)
- Test: `tests/test_server_api.py` continues to pass; add `tests/test_comic_routes.py` with focused assertions that the 5 routes are mounted via the router (e.g., `len(comic_router.routes) == 5`).

**Approach:**
- `server/comic_routes.py` defines `router = APIRouter()` and the 5 route handlers. Pydantic models (`UpsertComicRequest`, `LocgLinkRequest`) live in the same file.
- **Which routes need `request: Request` (per the route inventory):**
  - `GET /v2/comics`: serves a static file via `FileResponse`. Keep its zero-arg signature; do NOT add `request: Request`.
  - `GET /api/comics`: DB access — gets `request: Request`, uses `request.app.state.db`.
  - `POST /api/comics`: DB access — gets `request: Request`, uses `request.app.state.db`.
  - `POST /api/bids/{item_id}/comics/locg`: DB access — gets `request: Request`, uses `request.app.state.db`.
  - `POST /api/extract-comics`: DB access — gets `request: Request`, uses `request.app.state.db`.
- Routes import what they need from `server.db` (CRUD helpers) and `server.title_parser` (for `/api/extract-comics`).
- `main.py` does `from server.comic_routes import router as comic_router` and `app.include_router(comic_router)` at module level — before the lifespan starts, alongside the other `app = FastAPI(...)` setup.
- `server/comic_routes.py` must have **zero import-time side effects**: no DB connection, no env reads. Defining the `APIRouter` and Pydantic models at import is fine; anything that touches `_db`, env vars, or the filesystem must happen at request time.
- Verify the 5 routes still appear in `/openapi.json` and continue to respond identically.

**Patterns to follow:**
- `tests/test_plugin_integration.py:101-122` — same `APIRouter` + `app.include_router(router)` shape the integration test already exercises.
- Existing handler bodies copy across verbatim. Where they previously called `_get_db()` or referenced the `_db` module global, they now use `request.app.state.db`.

**Test scenarios:**
- Happy path: `GET /api/comics` returns the same response shape after the move. Compare a fixture-loaded DB response before and after by snapshot.
- Happy path: `POST /api/comics` with a minimal payload (`title`, `issue`) creates a row identical to pre-refactor behavior.
- Happy path: `POST /api/bids/{item_id}/comics/locg` with a known item links a LOCG ID to the correct comic.
- Happy path: `POST /api/extract-comics` parses cached titles and creates expected comic rows.
- Happy path: `GET /v2/comics` returns the static HTML file with `200` and `Content-Type: text/html`. Confirms the zero-arg `FileResponse` signature still works.
- Integration: After the move, `comic_router.routes` contains exactly 5 routes. `app.routes` still contains all 17 paths (now 5 via include_router, 12 directly).
- Integration: `/openapi.json` lists all 5 comic paths with their request bodies.
- Edge case: Importing `server.comic_routes` without setting `DB_PATH`, `GIXEN_USERNAME`, or `GIXEN_PASSWORD` env vars succeeds (zero import-time side effects).

**Verification:**
- `pytest tests/test_server_api.py tests/test_plugin_integration.py tests/test_comic_routes.py` passes.
- Smoke step (verifies single-file packaging works in editable mode): `pip install -e . && python -c "from server.comic_routes import router as comic_router; assert len(comic_router.routes) == 5"`.
- `git diff` shows the 5 comic route handler bodies are identical (modulo `request.app.state.db` and signature additions for the 4 DB-touching routes). Pydantic model definitions are byte-identical.
- `server/main.py` line count drops by ~150 lines (5 handlers + 2 models).

---

- [ ] **Unit 3: Extract plugin-lifespan plumbing into `gixen/plugins.py` (PER-25 M-01)**

**Goal:** Move the per-plugin SAVEPOINT loop, bulk hook wrappers, and tab flattening from `server/main.py` lifespan into underscore-prefixed internal helpers in `gixen/plugins.py`. The lifespan becomes a short sequence of named calls. Logger emission stays under the `"server.main"` logger so existing tests pass unchanged.

**Requirements:** R6, R4

**Dependencies:** None (independent of Units 1 and 2 — different file)

**Files:**
- Modify: `gixen/plugins.py` (add `_invoke_db_tables_isolated`, `_invoke_register_routes`, `_collect_dashboard_tabs` helpers)
- Modify: `server/main.py` (lifespan calls the new helpers; ~80 lines of inline plumbing collapse to ~5 lines of calls)
- Test: `tests/test_plugins.py` (unit tests for the new helpers), `tests/test_plugin_integration.py` (unchanged — must still pass, including all `caplog.set_level(..., logger="server.main")` regression tests)

**Approach:**
- Three new functions in `gixen/plugins.py`, all underscore-prefixed to signal host-side-internal:
  - `_invoke_db_tables_isolated(pm, conn, *, logger) -> list[str]`: runs the per-plugin `register_db_tables` hook with a SAVEPOINT around each plugin's DDL. Sanitizes the plugin name for SQL via the existing regex. Wraps `ROLLBACK TO`/`RELEASE` cleanup in an inner try/except (the PER-25 ADV-001 fix). Returns the list of plugin names whose DDL succeeded. Logs errors via the injected `logger`.
  - `_invoke_register_routes(pm, app, *, logger) -> None`: calls `pm.hook.register_routes(app=app)` inside a try/except that logs at ERROR via the injected `logger`. Sets `app.openapi_schema = None` afterwards (unconditionally — both success and error paths, via try/finally — so the schema is consistent regardless of whether plugins registered routes). Halts on first failure (preserves PER-25 contract).
  - `_collect_dashboard_tabs(pm, *, logger) -> list[dict]`: calls `pm.hook.register_dashboard_tabs()`, defensively guards each return value with `isinstance(x, list)`, and flattens. Logs a clear ERROR when a plugin returns a non-list (the PER-25 ADV-002 finding).
- **Logger injection is load-bearing for R4.** Each helper takes a keyword-only `logger: logging.Logger` argument. The lifespan in `server/main.py` calls them as `_invoke_db_tables_isolated(pm, _db, logger=logger)` where `logger = logging.getLogger(__name__)` is already `"server.main"`. This preserves the logger name on every emission, so `tests/test_plugin_integration.py`'s `caplog.set_level(logging.ERROR, logger="server.main")` continues to capture the cleanup-failure messages from PER-25's ADV-001/ADV-002/REL-01/COR-01 regression tests.
- The lifespan in `server/main.py` reads as:
  ```
  _db = init_db(...)
  app.state.db = _db
  pm = load_plugins()
  app.state.plugin_manager = pm
  _invoke_db_tables_isolated(pm, _db, logger=logger)
  _invoke_register_routes(pm, app, logger=logger)
  app.state.dashboard_tabs = _collect_dashboard_tabs(pm, logger=logger)
  yield
  app.state.db = None
  _db.close()
  ```
  (directional sketch — not literal source)
- Public API of `gixen/plugins.py` (`__all__`) stays as `["hookimpl", "load_plugins"]` — the new helpers are host-side, not for plugin authors. The underscore prefix is the primary signal; `__all__` is the secondary signal.

**Patterns to follow:**
- `gixen/plugins.py:113-161` (`load_plugins`) for the logging conventions and try/except shape.
- PER-25 plan unit 4 (the lifespan integration unit) — preserve every behavior it documented (per-plugin isolation, bulk-route halt-on-first-failure, defensive list check for tabs, openapi regen).

**Execution note:** Land Unit 3 as a separate commit on the same branch, after Units 1 and 2. Keeps `git bisect` clean — route-extraction regressions and lifespan-plumbing regressions land in separate commits.

**Test scenarios:**
- Unit: `_invoke_db_tables_isolated` with no plugins → returns `[]`, no DDL executed.
- Unit: With one plugin whose DDL succeeds → returns `["plugin-name"]`, table exists in the connection.
- Unit: With one plugin whose DDL raises mid-statement → returns `[]`, SAVEPOINT rolled back, no orphan table.
- Unit: With one plugin that uses `conn.executescript(...)` (violating the hookspec) → does not crash; logs `"Savepoint cleanup failed"` via the injected logger and continues. (Regression for ADV-001/REL-01/COR-01.)
- Unit: With two plugins where the first fails and the second succeeds → returns `["second-plugin"]`, only the second's table exists.
- Unit: `_invoke_register_routes` with one plugin that raises → logs at ERROR via the injected logger, does not propagate. `app.openapi_schema` is set to `None` regardless (via try/finally).
- Unit: `_collect_dashboard_tabs` with one plugin returning `[{"slug": "x"}]` → returns `[{"slug": "x"}]`.
- Unit: `_collect_dashboard_tabs` with one plugin returning a bare dict (not a list) → returns `[]`, logs `"register_dashboard_tabs returned ... expected list"` via the injected logger. (Regression for ADV-002.)
- Unit: All three helpers, when passed `logger=logging.getLogger("server.main")`, emit their log records on the `"server.main"` logger (assertable via `caplog.records[i].name == "server.main"`).
- Integration: `tests/test_plugin_integration.py` passes unchanged — the lifespan behaves identically after extraction, and all `caplog.set_level(..., logger="server.main")` assertions still capture records.

**Verification:**
- `pytest tests/test_plugins.py tests/test_plugin_integration.py` passes, including all PER-25 regression tests (ADV-001 via `test_plugin_executescript_does_not_crash_lifespan`, ADV-002 via `test_plugin_register_dashboard_tabs_bare_dict_is_rejected`, etc.).
- `server/main.py` lifespan is significantly shorter — visual confirmation that the plumbing moved.
- `gixen/plugins.py` exports the three new helpers as module-level underscore-prefixed functions (not in `__all__`) and they each have a docstring explaining their contract, the PER-25 finding they capture, and the requirement to pass a logger.

---

- [ ] **Unit 4: Route-shadow regression test**

**Goal:** Catch silent route-shadowing during future plugin registration or refactors via a duplicate-`(method, path)` check.

**Requirements:** R7

**Dependencies:** Units 2 and 3 (the refactor needs to be in place to test against)

**Files:**
- Create: `tests/test_route_organization.py`

**Approach:**
- One test asserts `app.routes` (excluding the openapi/docs/redoc auto-routes) has no duplicate `(method, path)` pairs after lifespan startup with no plugins installed.
- No pinned route-count manifest. The repo is mid-refactor with PER-27..PER-37 queued; a pinned set would fire reflexively on legitimate route additions and degrade into a rubber-stamp test. The duplicate-pair check catches the actual silent-shadowing bug class.

**Patterns to follow:**
- `tests/test_plugin_integration.py:42-67` for the `make_app` / TestClient context-manager fixture.

**Test scenarios:**
- Happy path: With no plugins installed, after lifespan startup, no `(method, path)` pair appears twice in `app.routes`. Compute as `pairs = [(method, route.path) for route in app.routes if hasattr(route, "methods") for method in route.methods if route.path not in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect")]`; assert `len(pairs) == len(set(pairs))`.

**Verification:**
- `pytest tests/test_route_organization.py` passes.

## System-Wide Impact

- **Interaction graph:** `server/main.py`'s lifespan is the single integration point for plugins and core state. Unit 1 adds `app.state.db` with pinned lifecycle; Unit 2 mounts the comic router via `include_router`; Unit 3 collapses ~80 lines of inline plumbing into ~5 named calls. No new entrypoints or callbacks.
- **Error propagation:** Unit 3 preserves PER-25's error semantics exactly — per-plugin SAVEPOINT rollback for DDL, bulk halt-on-first-failure for routes, defensive list check for tabs. Logger emission stays under `"server.main"` via injected logger. Any change to these semantics would be a contract change, not an M-01 cleanup, and is out of scope.
- **State lifecycle risks:** `app.state.db` is set immediately after `init_db(...)` returns and cleared to `None` on teardown alongside `_db.close()`. Routes never see an unset state because routes only fire after lifespan startup completes. Plugin `register_db_tables` hooks see it set because Unit 1 pins the ordering before `load_plugins()`. Post-teardown code that mistakenly holds a reference gets `None` rather than a closed connection.
- **API surface parity:** Every external HTTP path and response shape is unchanged. The CLI thin-client (`cli.py` with `GIXEN_SERVER_URL` set) calls these endpoints directly; PER-26 must not break that mode. Verified by existing `tests/test_server_api.py`.
- **Integration coverage:** Existing `tests/test_plugin_integration.py` already verifies that `register_routes` is invoked, that plugin routes appear in `/openapi.json`, and that lifespan-time DDL failures roll back. PER-26 inherits this coverage by ensuring the refactored helpers preserve identical observable behavior, including the logger name `"server.main"` for ADV-001/ADV-002/REL-01/COR-01 cleanup logs.
- **Unchanged invariants:**
  - All 17 existing route paths, methods, and response shapes are identical before and after PER-26.
  - The `gixen.plugins` entry-point group, hookspec signatures, and `hookimpl` marker are unchanged.
  - The `"server.main"` logger continues to emit cleanup-failure messages from `_invoke_*` helpers (preserved via logger injection).
  - Module-level globals in `server/main.py` (`_db`, `_api_client`, etc.) remain in place for backward compatibility with any code that imports them directly.
  - Lifespan ordering is now explicit: init_db → set `app.state.db` → load_plugins → `_invoke_db_tables_isolated` → `_invoke_register_routes` → `_collect_dashboard_tabs` → openapi reset → yield → clear `app.state.db` → `_db.close()`.
  - No middleware, exception handlers, or router-level dependencies are added.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Comic route signatures need slight changes (adding `request: Request` for `app.state.db` access on the 4 DB-touching routes). Could miss one, causing a runtime `NameError` for the old `_db` global. | The route inventory table in this plan explicitly lists which routes need DB access. Run full test suite after Unit 2; existing route tests will catch any miss immediately. |
| `include_router(comic_router)` order matters relative to lifespan-time plugin route registration. If the comic router were ever moved into a plugin (PER-30) without removing the core `include_router` call, both core and plugin would register `/api/comics`. | Unit 4's duplicate-pair test catches core-only mistakes (runs without plugins). PER-30 will need its own removal-verification test that asserts the plugin's routes don't shadow core. Document in the PER-26 PR description. |
| The M-01 cleanup (Unit 3) is "behavior-preserving" but pluggy semantics are subtle. A regression in `_invoke_db_tables_isolated` behavior could silently re-introduce ADV-001. | Unit 3's test scenarios explicitly include regression tests for ADV-001 (executescript), ADV-002 (bare-dict), REL-01/COR-01 (savepoint cleanup failure). All PER-25 regression tests must pass unchanged, and now also assert that log records appear on the `"server.main"` logger (caplog target). Landing Unit 3 as a separate commit on the same branch makes bisect surgical. |
| `server/comic_routes.py` could accidentally introduce import-time side effects (env reads, DB connections) that break the late-import pattern in `tests/test_plugin_integration.py:62`. | Unit 2 has an explicit edge-case test that imports `server.comic_routes` without any env vars set and succeeds. The module must define only `APIRouter()` and Pydantic models at import time; all runtime work happens in handler bodies. |
| `app.state.db` lifecycle could subtly diverge for tests that re-import `server.main` (the module singleton accumulates plugin routes across tests). | Unit 4's duplicate-pair test runs without plugins, so plugin accumulation doesn't affect it. The existing `make_app` fixture in `tests/test_plugin_integration.py` already handles per-test env-var monkeypatching; Unit 1's `app.state.db = None` teardown clears state cleanly. If a future test orchestration issue surfaces, a `create_app()` factory may be needed in PER-30 — but for PER-26 the existing pattern holds. |

## Documentation / Operational Notes

- **Update `CLAUDE.md`** — the "Architecture" section currently lists `server/` as a single bullet describing the FastAPI app. After PER-26, add a short note that comic-dedicated routes live in `server/comic_routes.py` and will move to the comic-pipeline plugin in PER-30. Keep the entry brief; the handoff doc has the full context.
- **Correct `docs/refactor-split-handoff.md`** — the handoff text says `PUT /api/bids/{item_id}` but the actual route is `PATCH`. One-line fix; should land in the same PR as PER-26 so the document stays accurate for PER-27..PER-37 contributors.
- **Commit hygiene** — Units 1, 2 land as commits 1 and 2. Unit 3 (M-01 cleanup) lands as commit 3 on the same branch. Unit 4 (test) lands as commit 4. This keeps `git bisect` precise: route-extraction regressions and lifespan-plumbing regressions are isolatable.
- **No `requirements.txt` change.** No new dependencies.
- **No production migration.** Existing DB schema, existing endpoints, existing CLI thin-client mode all keep working.
- **After merging PER-26 into `feat/plugin-refactor`, smoke-test locally**: start `uvicorn server.main:app --reload`, hit the dashboard, verify the snipes table renders and the `/v2/comics` tab loads. Then verify a comic API call works (`curl http://localhost:8000/api/comics`).

## Sources & References

- **Origin document:** [docs/refactor-split-handoff.md](docs/refactor-split-handoff.md) — handoff brief for the 13-issue refactor. PER-26 is the second issue in Phase 1.
- **Prior plan (predecessor):** [docs/plans/2026-05-18-001-feat-plugin-entry-point-system-plan.md](docs/plans/2026-05-18-001-feat-plugin-entry-point-system-plan.md) — PER-25 plan. Established the plugin entry-point system PER-26 builds on. Surfaced the M-01 finding PER-26 resolves in Unit 3.
- **PER-25 review artifacts:** `.context/compound-engineering/ce-review/20260518-180724-1470c731/` — the 11-reviewer pass that flagged M-01 (maintainability), the route-shadowing residual risk (api-contract), and the ADV-001/ADV-002/REL-01/COR-01 logger-name regression requirements.
- **PER-26 review artifacts** (this plan's deepening pass): 5-reviewer document-review pass run 2026-05-18. Findings integrated: logger-name preservation (P1), single-file router layout (P2), drop pinned manifest (P2), underscore-prefix M-01 helpers (P2), pin `app.state.db` lifecycle (P1), explicit per-route DB-need list (P3), drop optional Unit 4 third test (P3), drop Unit 1 optional api_client/lock framing (P3), fix handoff doc PATCH-vs-PUT (P3), no middleware additions to scope (P3), add Trajectory Note (judgment).
- **Linear:** [PER-26](https://linear.app/hk-iterative/issue/PER-26) in Project [PER / Separate gixen-cli from Comics Use Case](https://linear.app/hk-iterative/project/separate-gixen-cli-from-comics-use-case-f54d62e4b982).
- **Related code:**
  - `server/main.py` — all 17 routes today; will host 12 routes after PER-26 (5 move to `server/comic_routes.py`)
  - `gixen/plugins.py` — host-side helpers added in Unit 3
  - `server/db.py` — CRUD helpers comic routes call into (unchanged)
  - `server/title_parser.py` — used by `POST /api/extract-comics` (unchanged)
  - `tests/test_plugin_integration.py` — pattern reference for `APIRouter` + `include_router` testing; the load-bearing regression suite for PER-25 ADV-001/ADV-002/REL-01/COR-01
- **Branch:** `feat/per-26-routes-for-plugin-registration` off `feat/plugin-refactor`. PR will target `feat/plugin-refactor` (integration branch).
