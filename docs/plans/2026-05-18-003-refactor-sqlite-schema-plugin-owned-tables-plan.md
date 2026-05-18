---
title: "refactor: Refactor SQLite Schema for Plugin-Owned Tables (PER-27)"
type: refactor
status: active
date: 2026-05-18
---

# refactor: Refactor SQLite Schema for Plugin-Owned Tables (PER-27)

## Overview

Core's `server/db.py` currently defines and creates three tables: `bids`, `comics`, and `bid_comics`. After this refactor, core owns only `bids`; the `comics` and `bid_comics` tables become plugin-owned — created via the `register_db_tables` hook that PER-25/26 established. The FK declaration `bids.comic_id INTEGER REFERENCES comics(id)` is removed from the `bids` schema so `bids.comic_id` becomes an untyped integer link that the plugin interprets. Existing databases are not migrated for table ownership (the plugin adopts existing `comics`/`bid_comics` via `CREATE TABLE IF NOT EXISTS`), but the FK on `bids.comic_id` in existing databases IS cleaned up via `_apply_migrations`.

## Problem Frame

`gixen-cli` is being transformed into a plugin-extensible tool. The `comics` and `bid_comics` tables are comic-specific and must be plugin-owned so a non-comic user never has those tables created by core. The current `bids.comic_id INTEGER REFERENCES comics(id)` FK is a load-bearing coupling — SQLite enforces it when `PRAGMA foreign_keys = ON` (which `init_db` enables), so it must be removed from the schema. See `docs/refactor-split-handoff.md` for the full refactor context.

## Requirements Trace

- R1. Core's `init_db` creates only `bids` (plus its index); `comics` and `bid_comics` are not created by core.
- R2. `bids.comic_id` is typed `INTEGER` with no `REFERENCES` clause in the schema used for new databases.
- R3. Existing databases that already have the FK declaration are migrated via `_apply_migrations` — after migration, `PRAGMA foreign_key_list(bids)` returns no row referencing `comics`.
- R4. The `bid_comics` backfill (`INSERT OR IGNORE INTO bid_comics SELECT … FROM bids WHERE comic_id IS NOT NULL`) is removed from core. It is a comic-specific migration that belongs to the plugin.
- R5. The `ALTER TABLE comics ADD COLUMN locg_id` and `ALTER TABLE comics ADD COLUMN locg_variant_id` entries are removed from `_COLUMN_MIGRATIONS` in core. These are comic-specific; the plugin's `register_db_tables` will create the full `comics` schema from scratch for new installs, and the live DB already has these columns.
- R6. All tests in `tests/test_server_db.py` and `tests/test_server_api.py` remain green after the change.
- R7. `test_server_api.py` tests that exercise comic routes or comic-column queries work by creating `comics`/`bid_comics` through a stub plugin fixture, proving the `register_db_tables` hook pattern before the real plugin exists.

## Scope Boundaries

- Comic-specific `db.py` functions (`upsert_comic`, `list_comics`, `link_comic_to_bid`, `get_comics_for_bid`, `get_primary_comic_for_bid`) stay in `server/db.py` — `comic_routes.py` still imports them; they move to the plugin in PER-30.
- `server/comic_routes.py` is unchanged — it moves to the plugin in PER-30.
- The `LEFT JOIN comics` inline SQL in `/api/snipes`, `/api/history`, `/api/bids` inside `server/main.py` is unchanged — decontaminating those routes is PER-30 scope.

### Deferred to Separate Tasks

- Move comic-specific `db.py` functions to the plugin: PER-30.
- Move `comic_routes.py` to the plugin: PER-30.
- Decontaminate the `LEFT JOIN comics` queries in core routes: PER-30.
- Create the actual `gixen-overlay` plugin with its `register_db_tables` implementation: PER-29/30.

## Context & Research

### Relevant Code and Patterns

- `server/db.py` — `_SCHEMA` (three CREATE TABLE statements), `_COLUMN_MIGRATIONS` (list of ALTER TABLE statements run on every `init_db`), `_apply_migrations(conn)` function (runs column additions and the bid_comics backfill)
- `gixen/plugins.py` — `_invoke_db_tables_isolated(pm, conn, *, logger)` implements per-plugin SAVEPOINT loop; hookspec docstring bans `conn.executescript()`
- `tests/test_plugin_integration.py` — `_install_plugins` helper, `make_app` factory fixture, `test_plugin_creates_table` — the canonical pattern for inserting synthetic plugins in tests
- `tests/test_server_api.py` — `api` fixture (sets env vars + `TestClient(app)` with `GixenClient` mocked); does not currently install any plugins

### Institutional Learnings

- `_apply_migrations` runs on every `init_db`; all entries must be idempotent (column additions use duplicate-column-name suppression; new FK-removal migration must detect-then-act)
- `conn.executescript()` is banned in `register_db_tables` hookimpls — it implicitly commits and destroys the SAVEPOINT (ADV-001 regression)
- Logger injection (`*, logger: logging.Logger`) is required for `_invoke_db_tables_isolated` so `caplog` assertions in `test_plugin_integration.py` on `"server.main"` logger continue to work
- The migration posture for existing DBs: leave `comics`/`bid_comics` tables alone; plugin adopts them via `CREATE TABLE IF NOT EXISTS` (from `docs/refactor-split-handoff.md`)

## Key Technical Decisions

- **FK removal on existing DBs via `_apply_migrations`**: SQLite has no `ALTER TABLE DROP CONSTRAINT`. The standard approach is: detect the FK with `PRAGMA foreign_key_list(bids)`; if a row referencing `comics` exists, run the table-rebuild dance (rename → recreate without FK → INSERT … SELECT → drop old → rebuild indexes). The required sequence is: `PRAGMA foreign_keys=OFF` via `conn.execute()` **before** any BEGIN/SAVEPOINT (SQLite silently ignores PRAGMA foreign_keys changes inside an active transaction), then a SAVEPOINT for atomicity, then the rebuild steps, then RELEASE, then `PRAGMA foreign_keys=ON`. This migration runs on every `init_db` call but is a no-op after the first run (no FK row detected on subsequent calls).

- **Remove bid_comics backfill from core**: The `INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary) SELECT id, comic_id, 1 FROM bids WHERE comic_id IS NOT NULL` backfill is comic-specific data migration. The live DB has already had this run. PER-30's plugin `register_db_tables` will include it for completeness, wrapped in `INSERT OR IGNORE` for idempotency.

- **Remove `ALTER TABLE comics ADD COLUMN` entries from `_COLUMN_MIGRATIONS`**: Removing them is safe — the live DB already has these columns, and for new installs the plugin's `CREATE TABLE IF NOT EXISTS` will include them from the start. Keeping them in core after the comics table is no longer created by core would cause `OperationalError: no such table: comics` on fresh databases without the plugin.

- **Stub plugin in `test_server_api.py`**: The `api` fixture will install a synthetic plugin (using the same `_install_plugins` + `monkeypatch` pattern from `test_plugin_integration.py`) that creates `comics` and `bid_comics` tables in `register_db_tables`. This keeps all existing comic-route tests green and validates the plugin hook mechanism before the real plugin exists.

- **`upsert_comic` and other comic db functions stay in core**: They are still imported and used by `server/comic_routes.py`, which is still in core. Moving them prematurely would break the import chain. PER-30 moves them.

## Open Questions

### Resolved During Planning

- **Do we need to migrate the FK on existing DBs or just leave it?** Decision: yes, run the table-rebuild migration. `PRAGMA foreign_keys = ON` is enabled by `init_db`, so the FK IS enforced. Leaving it would cause constraint violations if a plugin-less install writes `bids.comic_id = NULL` or a user attempts to drop the `comics` table manually.
- **Should `ALTER TABLE comics ADD COLUMN` entries be guarded (table-existence check) vs. removed?** Decision: remove them. They are comic-specific; a table-existence check would just delay the inevitable move to the plugin, and the live DB doesn't need them.
- **Stub plugin in `test_server_api.py`: per-test fixture or modify the `api` fixture?** Decision: modify the `api` fixture so all existing tests automatically get the comic tables. No test currently asserts the absence of `comics`, so making it always present is safe and keeps test changes minimal.

### Deferred to Implementation

- Exact column list for the `bids` table rebuild — implementer reads the current `_SCHEMA` **and** the full `_COLUMN_MIGRATIONS` list to enumerate every column (the live DB has columns added by migrations that are not in `_SCHEMA`); never derive the column list from `_SCHEMA` alone.
- Whether to use a bare transaction or a named SAVEPOINT for the FK-removal rebuild in `_apply_migrations` — either works; SAVEPOINT is slightly cleaner for nested contexts.
- Exact conftest pattern for `test_server_api.py` — whether to add `_install_plugins` inline or refactor to a shared `tests/conftest.py`; PER-27 keeps it inline for minimal scope.

## Implementation Units

- [x] **Unit 1: Strip comics/bid_comics from `_SCHEMA` and `_COLUMN_MIGRATIONS`**

**Goal:** `server/db.py` no longer creates or migrates the `comics` or `bid_comics` tables, and `bids.comic_id` has no FK declaration in the schema used for new databases.

**Requirements:** R1, R2, R5

**Dependencies:** None

**Files:**
- Modify: `server/db.py`
- Test: `tests/test_server_db.py` (updated in Unit 3)

**Approach:**
- In `_SCHEMA`, remove the entire `CREATE TABLE IF NOT EXISTS comics (...)` block
- In `_SCHEMA`, remove the entire `CREATE TABLE IF NOT EXISTS bid_comics (...)` block and its `CREATE INDEX IF NOT EXISTS idx_bid_comics_bid` line
- In `_SCHEMA`, change `bids.comic_id INTEGER REFERENCES comics(id)` to `comic_id INTEGER`
- In `_COLUMN_MIGRATIONS`, remove the two `ALTER TABLE comics ADD COLUMN locg_id ...` and `ALTER TABLE comics ADD COLUMN locg_variant_id ...` entries (the only two comics-specific entries)
- All other `_SCHEMA` content (bids table definition, bids index) is unchanged

**Patterns to follow:**
- Existing `_SCHEMA` string structure in `server/db.py`
- Existing `_COLUMN_MIGRATIONS` list structure

**Test scenarios:**
- Test expectation: none at this unit — tested in Unit 3 against the full `init_db` call.

**Verification:**
- `grep "REFERENCES comics" server/db.py` returns no output
- `grep "CREATE TABLE.*comics" server/db.py` returns no output
- `grep "ALTER TABLE comics" server/db.py` returns no output

---

- [x] **Unit 2: Remove bid_comics backfill and add FK-removal migration to `_apply_migrations`**

**Goal:** `_apply_migrations` no longer contains comic-specific logic; fresh installs without the plugin never encounter a reference to the `bid_comics` table. Existing databases with the FK on `bids.comic_id` have it removed on the first `init_db` call after the upgrade.

**Requirements:** R3, R4

**Dependencies:** Unit 1 (the `_SCHEMA` change defines what the rebuilt `bids` table looks like)

**Files:**
- Modify: `server/db.py`
- Test: `tests/test_server_db.py` (updated in Unit 3)

**Approach:**
- Remove the `INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary) SELECT id, comic_id, 1 FROM bids WHERE comic_id IS NOT NULL` block from `_apply_migrations`
- Add a FK-removal migration block after the existing `_COLUMN_MIGRATIONS` loop:
  - Query `PRAGMA foreign_key_list(bids)` and check if any row has `table == "comics"`
  - If not found: no-op (idempotent); skip the rest
  - If found, execute the table-rebuild dance in this exact sequence:
    1. `conn.execute("DROP TABLE IF EXISTS bids_old")` — clear any orphan from a previous failed run
    2. `conn.execute("PRAGMA foreign_keys=OFF")` — must be issued **before** any BEGIN/SAVEPOINT; SQLite silently ignores this PRAGMA inside an active transaction
    3. Open a named SAVEPOINT for atomicity
    4. `ALTER TABLE bids RENAME TO bids_old`
    5. `CREATE TABLE bids (...)` — exact same columns as the updated `_SCHEMA` (from Unit 1, minus the `REFERENCES` clause); **must also include all columns added by `_COLUMN_MIGRATIONS`** that exist in the live schema (`ebay_title`, `status_mirror`, `cached_current_bid`, `cached_at`, `local_snipe_at`, `local_snipe_result`), since `_SCHEMA` alone does not enumerate them
    6. `INSERT INTO bids (col1, col2, ...) SELECT col1, col2, ... FROM bids_old` — use an **explicit named column list** (never `SELECT *`); the list must include every column present in both old and new schema
    7. `DROP TABLE bids_old`
    8. `CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id)`
    9. RELEASE the SAVEPOINT
    10. `conn.execute("PRAGMA foreign_keys=ON")`

**Patterns to follow:**
- `_COLUMN_MIGRATIONS` duplicate-column-name suppression pattern (try/except OperationalError with specific message check)
- Standard SQLite table-rebuild approach from SQLite docs: rename → create → insert → drop

**Test scenarios:**
- Test expectation: none at this unit — covered by Unit 3 integration tests against `init_db`.

**Verification:**
- `grep "bid_comics" server/db.py` returns output only from function signatures and SQL in `link_comic_to_bid` / `get_comics_for_bid` / `get_primary_comic_for_bid` — not from `_apply_migrations`
- Reopening an old test DB (with the FK in schema) and calling `init_db` produces `PRAGMA foreign_key_list(bids)` with no row referencing `comics`

---

- [x] **Unit 3: Update `test_server_db.py` for new schema boundary**

**Goal:** `tests/test_server_db.py` accurately reflects what core creates and migrates; no test asserts that `comics` or `bid_comics` are core-owned; new tests cover the FK-removal migration; comic-function tests are updated to use a `db_with_comics` fixture that pre-creates those tables.

**Requirements:** R6

**Dependencies:** Units 1 and 2

**Files:**
- Modify: `tests/test_server_db.py`

**Approach:**
- Update `test_init_creates_tables`: replace assertions `"comics" in tables` and `"bid_comics" in tables` with assertions that these tables are NOT present (`"comics" not in tables`, `"bid_comics" not in tables`). Keep the assertion that `"bids" in tables`.
- Remove `test_migration_backfills_bid_comics_from_legacy_bids` — this test exercises the bid_comics backfill, which is being removed from core.
- Remove `test_migration_backfill_is_idempotent` — same reason.
- Add `test_bids_comic_id_has_no_foreign_key`: fresh `init_db`; call `PRAGMA foreign_key_list(bids)`; assert the result is empty (no FK declared).
- Add `test_fk_removal_migration_drops_comics_reference`: manually create a SQLite DB at `tmp_path / "legacy.db"` with the old `bids` schema (including `comic_id INTEGER REFERENCES comics(id)`) and the `comics` table; call `init_db(legacy_db_path)` (a `Path`, not `str` — `init_db`'s signature is `path: Path` and calls `path.parent.mkdir()`); verify `PRAGMA foreign_key_list(bids)` returns no rows referencing `comics`; verify that any existing `bids` rows survive the migration intact (insert one row before, check it exists after).
- **Do NOT leave `upsert_comic`/`link_comic_to_bid`/`get_comics_for_bid`/`get_primary_comic_for_bid` tests unchanged** — after Unit 1, `init_db` no longer creates `comics` or `bid_comics`, so the `db` fixture (which calls `init_db`) produces a DB without those tables. Every test that calls `upsert_comic`, `link_comic_to_bid`, `get_comics_for_bid`, or `get_primary_comic_for_bid` will immediately fail with `OperationalError: no such table: comics`.
- Add a `db_with_comics` fixture alongside the existing `db` fixture: call `init_db(tmp_path / "test.db")`, then run raw `CREATE TABLE IF NOT EXISTS comics (...)` and `CREATE TABLE IF NOT EXISTS bid_comics (...)` DDL directly on the connection (mirroring the exact schema from the old `_SCHEMA`), then yield that connection. This avoids touching the plugin hook machinery in `test_server_db.py`.
- Update all tests that currently use `db` and call comic functions to use `db_with_comics` instead. Keep the `db` fixture for non-comic tests (e.g., `test_init_creates_tables`, `test_bids_comic_id_has_no_foreign_key`, `test_fk_removal_migration_drops_comics_reference`).
- The original `db` fixture remains unchanged for bids-only tests.

**Patterns to follow:**
- Existing `test_server_db.py` fixture pattern (uses `tmp_path`, calls `init_db`, uses raw `sqlite3` connection for assertions)

**Test scenarios:**
- Happy path: fresh `init_db` on a new path → `sqlite_master` contains only `bids` and `idx_bids_item_id`; `PRAGMA foreign_key_list(bids)` is empty; `comics` and `bid_comics` tables are NOT present
- Happy path: fresh `init_db` → `bids.comic_id` accepts `NULL` and any integer value without FK constraint errors
- Migration — FK removal: existing DB with `bids` table including `REFERENCES comics(id)` and the `comics` table → after `init_db`, `PRAGMA foreign_key_list(bids)` is empty; data rows in `bids` are intact
- Migration — idempotency: call `init_db` twice on a DB that started without the FK; second call does not error and `PRAGMA foreign_key_list(bids)` is still empty
- Comic functions (via `db_with_comics` fixture): `upsert_comic`, `link_comic_to_bid`, `get_comics_for_bid`, `get_primary_comic_for_bid` all work correctly when the fixture pre-creates the tables

**Verification:**
- `pytest tests/test_server_db.py` passes with 0 failures
- No test in `test_server_db.py` asserts `"comics" in tables` or `"bid_comics" in tables` (grep check)
- All `upsert_comic`/`link_comic_to_bid`/`get_comics_for_bid`/`get_primary_comic_for_bid` tests use `db_with_comics` fixture, not `db`

---

- [x] **Unit 4: Add stub comic-schema plugin to `test_server_api.py`**

**Goal:** The `api` fixture in `test_server_api.py` installs a synthetic plugin that creates `comics` and `bid_comics` tables via `register_db_tables`, so all existing comic-route tests pass without relying on core to create those tables.

**Requirements:** R6, R7

**Dependencies:** Units 1–3

**Files:**
- Modify: `tests/test_server_api.py`

**Approach:**
- Copy the `_install_plugins` helper from `tests/test_plugin_integration.py` into `tests/test_server_api.py` (or import from `conftest.py` if one is created)
- Add necessary imports: `types`, `sys`, `EntryPoint` from `importlib.metadata`, `hookimpl` from `gixen.plugins`
- Create a `_make_comic_schema_plugin()` factory function that returns a `types.ModuleType` with a `register_db_tables` hookimpl that:
  - Creates `comics` with `CREATE TABLE IF NOT EXISTS`
  - Creates `bid_comics` with `CREATE TABLE IF NOT EXISTS`
  - Creates `idx_bid_comics_bid` index with `CREATE INDEX IF NOT EXISTS`
  - Includes `INSERT OR IGNORE INTO bid_comics` backfill (migrating legacy `bids.comic_id` values into the junction table)
  - Uses individual `conn.execute(...)` calls (never `conn.executescript`)
- Update the `api` fixture to call `_install_plugins(monkeypatch, {"comic-stub": _make_comic_schema_plugin()})` before the `TestClient(app)` context

**Execution note:** Implement the stub's `register_db_tables` with the full `comics`/`bid_comics` CREATE TABLE statements matching the current `server/db.py` `_SCHEMA`. Verify the schema matches exactly to avoid test drift.

**Patterns to follow:**
- `_install_plugins` helper in `tests/test_plugin_integration.py`
- `test_plugin_creates_table` pattern for `register_db_tables` hookimpl

**Test scenarios:**
- Integration: `api` fixture starts the app → lifespan runs → `load_plugins()` discovers `comic-stub` → `_invoke_db_tables_isolated` calls `register_db_tables` → `comics` and `bid_comics` tables exist in the test DB
- Happy path: existing `test_get_snipes_merges_fmv` continues to pass (LEFT JOIN comics finds data)
- Happy path: existing `test_add_bid_with_comic_links_fmv` continues to pass (upsert_comic succeeds)
- Happy path: all `test_locg_link_*` tests continue to pass
- Error path: if stub plugin's `register_db_tables` raises, the server still starts (covered by existing `test_plugin_db_tables_failure_rolls_back_savepoint` in `test_plugin_integration.py`)

**Verification:**
- `pytest tests/test_server_api.py` passes with 0 failures
- `pytest tests/` passes with 0 failures (full suite green)

## System-Wide Impact

- **Interaction graph:** `init_db` is called once per lifespan in `server/main.py`. The `_apply_migrations` FK-removal logic runs every time `init_db` is called; it is idempotent (no-op after first run). No other code path calls `init_db`.
- **Error propagation:** If the FK-removal table-rebuild fails mid-way (e.g., disk full), the SAVEPOINT/transaction rolls back and `bids_old` may be left orphaned. The `_apply_migrations` error handling should log and re-raise so `init_db` surfaces the failure at startup rather than silently continuing with a half-migrated schema.
- **State lifecycle risks:** The table rebuild temporarily renames `bids` to `bids_old`. Any concurrent connection that tries to query `bids` during the ~millisecond rebuild would fail. This is only a risk in test environments with multiple threads; production uses a single uvicorn startup sequence.
- **API surface parity:** The comic-specific `db.py` functions (`upsert_comic` etc.) are unchanged. Routes that use them continue to work. The `LEFT JOIN comics` queries in core routes (`/api/snipes`, `/api/history`, `/api/bids`) continue to work as long as a plugin that creates `comics` is installed — this is a known limitation noted in Scope Boundaries.
- **Integration coverage:** The stub plugin in `test_server_api.py` provides end-to-end coverage of the plugin hook creating tables and the core routes consuming them through the full lifespan.
- **Unchanged invariants:** All 5 comic routes in `comic_routes.py` are unchanged. All three contaminated core routes (`/api/snipes`, `/api/history`, `/api/bids`) are unchanged. The `register_db_tables` hook contract (savepoint isolation, `conn.execute` only, `CREATE TABLE IF NOT EXISTS`) is unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| FK-removal table rebuild corrupts data on existing DB | Wrap in transaction; verify with `PRAGMA integrity_check` after rebuild; add test with actual data rows that must survive migration |
| `_apply_migrations` FK-removal logic errors on a DB where `bids_old` already exists (previous failed run) | Check for `bids_old` existence before renaming; drop it if present, or use a unique temp name |
| `test_server_api.py` stub plugin schema drifts from the plugin's eventual real schema in PER-30 | Document the stub schema clearly; PER-30 will replace it with the real plugin and delete the stub |
| Removing the bid_comics backfill from core loses data for a user who has `bids.comic_id` set but empty `bid_comics` | Confirmed: the live DB has already had this backfill run; removing from core is safe for the single real user |

## Documentation / Operational Notes

- `CLAUDE.md` does not need updating — the architecture description of `server/db.py` is already accurate after PER-26's edits.
- The `docs/refactor-split-handoff.md` "Current State" section mentions `bids.comic_id INTEGER REFERENCES comics(id)` — after PER-27 merges, update that line to note the FK was removed.
- PER-30 will need to implement the full `register_db_tables` for the comic plugin; the stub plugin in `test_server_api.py` provides the exact DDL as a reference.

## Sources & References

- **Handoff doc:** `docs/refactor-split-handoff.md` — prescribes FK removal and "leave existing comic tables alone" migration posture
- **Prior plans:** `docs/plans/2026-05-18-001-feat-plugin-entry-point-system-plan.md` — hookspec design, savepoint isolation, logger injection
- **Prior plans:** `docs/plans/2026-05-18-002-refactor-fastapi-routes-for-plugin-registration-plan.md` — `_invoke_db_tables_isolated` extraction, ADV-001 regression test
- Related code: `server/db.py` (`_SCHEMA`, `_COLUMN_MIGRATIONS`, `_apply_migrations`)
- Related code: `gixen/plugins.py` (`_invoke_db_tables_isolated`)
- Related tests: `tests/test_server_db.py`, `tests/test_server_api.py`, `tests/test_plugin_integration.py`
- Related PRs: #6 (PER-25), #7 (PER-26)
