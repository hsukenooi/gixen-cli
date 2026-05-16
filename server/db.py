from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".gixen-server" / "db.sqlite"

# Post-FMV-split schema. New databases use this shape directly; legacy DBs
# are upgraded via `_migrate_fmv_split` (gated on the presence of legacy
# columns like `comics.grade`).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS comics (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    issue           TEXT NOT NULL,
    year            INTEGER NOT NULL,
    locg_id         INTEGER,
    locg_variant_id INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(title, issue, year)
);

CREATE TABLE IF NOT EXISTS fmv (
    id          INTEGER PRIMARY KEY,
    comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
    grade       REAL NOT NULL,
    low         REAL,
    high        REAL,
    comps       INTEGER,
    confidence  TEXT CHECK(confidence IN ('high', 'medium', 'low') OR confidence IS NULL),
    notes       TEXT,
    updated_at  TEXT,
    UNIQUE(comic_id, grade)
);

CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id);

CREATE TABLE IF NOT EXISTS bids (
    id                  INTEGER PRIMARY KEY,
    item_id             TEXT NOT NULL,
    fmv_id              INTEGER REFERENCES fmv(id) ON DELETE SET NULL,
    max_bid             REAL NOT NULL,
    bid_offset          INTEGER DEFAULT 6,
    snipe_group         INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),
    winning_bid         REAL,
    seller              TEXT,
    auction_end_at      TEXT,
    local_snipe_at      TEXT,
    local_snipe_result  TEXT,
    notes               TEXT,
    ebay_title          TEXT,
    status_mirror       TEXT,
    cached_current_bid  TEXT,
    cached_at           TEXT,
    added_at            TEXT DEFAULT (datetime('now')),
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
-- idx_bids_fmv is created after _COLUMN_MIGRATIONS in _apply_migrations, so
-- legacy DBs (bids has no fmv_id yet) can still process the schema script.

CREATE TABLE IF NOT EXISTS bid_fmvs (
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    fmv_id     INTEGER NOT NULL REFERENCES fmv(id)  ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bid_id, fmv_id)
);

CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id);
"""


_COLUMN_MIGRATIONS = [
    # bids columns added since the original schema. These apply only when the
    # legacy bids shape is still present (pre-FMV-split DB) — _migrate_fmv_split
    # rebuilds bids from scratch and the post-split _SCHEMA already includes
    # everything. ADD COLUMN is idempotent (caught by the "duplicate column"
    # handler below).
    "ALTER TABLE bids ADD COLUMN ebay_title TEXT",
    "ALTER TABLE bids ADD COLUMN status_mirror TEXT",
    "ALTER TABLE bids ADD COLUMN cached_current_bid TEXT",
    "ALTER TABLE bids ADD COLUMN cached_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_result TEXT",
    # legacy comics columns. Skipped on fresh DBs (UNIQUE constraint fires on
    # already-modern shape) — the OperationalError handler below covers both
    # "duplicate column" and "no such table" cases.
    "ALTER TABLE comics ADD COLUMN locg_id INTEGER",
    "ALTER TABLE comics ADD COLUMN locg_variant_id INTEGER",
    # FMV split (2026-05-13): fmv_id is the single FK from bids into the
    # per-grade fmv table. ALTER is idempotent (caught by the
    # "duplicate column" handler in _apply_migrations).
    "ALTER TABLE bids ADD COLUMN fmv_id INTEGER REFERENCES fmv(id)",
]


def _apply_migrations(conn: sqlite3.Connection, db_path: Path | None = None) -> None:
    for stmt in _COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError as e:
            # Idempotent column adds: ignore "duplicate column name". Anything
            # else (disk full, locked DB, syntax error in a future migration)
            # should not be silently swallowed.
            if "duplicate column" not in str(e).lower():
                raise

    _migrate_fmv_split(conn, db_path)

    # Index on bids(fmv_id) — deferred to here because legacy DBs don't have
    # the column until _COLUMN_MIGRATIONS (and the migration's rebuild
    # recreates it anyway, but only when the migration fires).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_fmv ON bids(fmv_id)")
    conn.commit()


log = logging.getLogger(__name__)


def _has_legacy_columns(conn: sqlite3.Connection) -> bool:
    """Detect the pre-FMV-split schema by presence of `comics.grade`."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    return "grade" in cols


def _ensure_backup(db_path: Path) -> Path:
    """Snapshot the DB file before destructive migration steps. Refuses to
    proceed if the backup write fails. Returns the backup path.

    Suffix is `.pre-fmv-split.bak` appended to the DB filename (NOT
    `.with_suffix` — that would replace the existing `.sqlite` suffix)."""
    bak = Path(str(db_path) + ".pre-fmv-split.bak")
    shutil.copy2(str(db_path), str(bak))
    log.info("FMV split migration: backup written to %s", bak)
    return bak


def _compute_survivors(conn: sqlite3.Connection) -> tuple[
    dict[tuple[str, str, int], int],
    list[sqlite3.Row],
    dict[int, int],
]:
    """Pick one survivor id per (title, issue, year) group. Returns:
      - survivor_map: (title, issue, year) -> survivor_id
      - legacy_rows: all rows from the legacy comics table
      - legacy_to_survivor: legacy comic_id -> survivor comic_id

    Survivor priority: locg_id NOT NULL > fmv_low NOT NULL > most recent
    fmv_updated_at > lowest id."""
    survivors = conn.execute("""
        SELECT title, issue, year,
               (SELECT id FROM comics c2
                WHERE c2.title=c1.title AND c2.issue=c1.issue AND c2.year=c1.year
                ORDER BY
                  CASE WHEN c2.locg_id        IS NOT NULL THEN 0 ELSE 1 END,
                  CASE WHEN c2.fmv_low        IS NOT NULL THEN 0 ELSE 1 END,
                  CASE WHEN c2.fmv_updated_at IS NULL    THEN 1 ELSE 0 END,
                  c2.fmv_updated_at DESC,
                  c2.id ASC
                LIMIT 1) AS survivor_id
        FROM comics c1
        GROUP BY title, issue, year
    """).fetchall()
    survivor_map: dict[tuple[str, str, int], int] = {
        (r["title"], r["issue"], r["year"]): r["survivor_id"] for r in survivors
    }
    legacy_rows = conn.execute(
        "SELECT id, title, issue, year, grade, fmv_low, fmv_high, fmv_comps, "
        "fmv_confidence, fmv_notes, fmv_updated_at "
        "FROM comics"
    ).fetchall()
    legacy_to_survivor: dict[int, int] = {
        r["id"]: survivor_map[(r["title"], r["issue"], r["year"])]
        for r in legacy_rows
    }
    return survivor_map, legacy_rows, legacy_to_survivor


def _manufacture_fmv_rows(
    conn: sqlite3.Connection,
    legacy_rows: list[sqlite3.Row],
    legacy_to_survivor: dict[int, int],
) -> int:
    """For every legacy comic row with grade IS NOT NULL, insert an fmv row
    at (survivor_id, grade). Conflicts on (survivor_id, grade) resolved by:
    - if existing.low is NULL: new value wins (carry valuation up)
    - else if both have non-NULL low: more recent updated_at wins; loser's
      notes are merged in with a 'merged from legacy comic_id=X' prefix
    - else: skip (existing has valuation, new doesn't add)

    Returns the count of fmv rows inserted (not counting updates)."""
    fmv_inserted = 0
    for row in legacy_rows:
        if row["grade"] is None:
            continue
        survivor_id = legacy_to_survivor[row["id"]]
        existing = conn.execute(
            "SELECT id, low, updated_at FROM fmv WHERE comic_id=? AND grade=?",
            (survivor_id, row["grade"]),
        ).fetchone()
        if existing is not None:
            new_low = row["fmv_low"]
            existing_low = existing["low"]
            should_overwrite = False
            if existing_low is None and new_low is not None:
                should_overwrite = True
            elif existing_low is not None and new_low is not None:
                # Tied: compare updated_at, prefer the more recent. NULL
                # updated_at is treated as oldest (loses).
                existing_at = existing["updated_at"]
                new_at = row["fmv_updated_at"]
                if new_at is not None and (existing_at is None or new_at > existing_at):
                    should_overwrite = True
            if should_overwrite:
                # Merge new valuation in, prefix notes with the legacy id of
                # the loser (i.e. the previously-stored row).
                merged_notes = (
                    f"[merged from legacy comic_id={row['id']}] "
                    + (row["fmv_notes"] or "")
                )
                # COALESCE on updated_at so a merge that introduces a real
                # valuation always stamps a date — without this, a NULL
                # legacy updated_at would clobber an existing timestamp.
                conn.execute(
                    """
                    UPDATE fmv
                    SET low=?, high=?, comps=?, confidence=?,
                        notes = COALESCE(?, notes),
                        updated_at = COALESCE(?, datetime('now'))
                    WHERE id=?
                    """,
                    (new_low, row["fmv_high"], row["fmv_comps"],
                     row["fmv_confidence"], merged_notes,
                     row["fmv_updated_at"], existing["id"]),
                )
            else:
                log.info(
                    "fmv split: skip legacy comic_id=%s grade=%s "
                    "(survivor fmv row already has valuation)",
                    row["id"], row["grade"],
                )
            continue
        conn.execute(
            """
            INSERT INTO fmv (comic_id, grade, low, high, comps,
                             confidence, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (survivor_id, row["grade"], row["fmv_low"], row["fmv_high"],
             row["fmv_comps"], row["fmv_confidence"], row["fmv_notes"],
             row["fmv_updated_at"]),
        )
        fmv_inserted += 1
    return fmv_inserted


def _build_fmv_lookup(
    conn: sqlite3.Connection,
    legacy_rows: list[sqlite3.Row],
) -> tuple[dict[tuple[int, float], int], dict[int, float | None]]:
    """Returns:
      - fmv_by_pair: (survivor_id, grade) -> fmv_id
      - legacy_grade: legacy comic_id -> grade
    """
    fmv_lookup_rows = conn.execute(
        "SELECT id, comic_id, grade FROM fmv"
    ).fetchall()
    fmv_by_pair: dict[tuple[int, float], int] = {
        (r["comic_id"], r["grade"]): r["id"] for r in fmv_lookup_rows
    }
    legacy_grade: dict[int, float | None] = {
        r["id"]: r["grade"] for r in legacy_rows
    }
    return fmv_by_pair, legacy_grade


def _repoint_bids(
    conn: sqlite3.Connection,
    legacy_to_survivor: dict[int, int],
    legacy_grade: dict[int, float | None],
    fmv_by_pair: dict[tuple[int, float], int],
) -> tuple[int, int]:
    """Set bids.fmv_id by resolving the bid's primary legacy comic_id through
    survivor + grade. Returns (bids_linked, bids_with_null).

    Uses .get() with fallbacks so a dangling legacy comic_id (deleted out
    from under the FK) doesn't crash the migration."""
    bids_linked = 0
    bids_with_null = 0
    bid_rows = conn.execute(
        "SELECT id, comic_id FROM bids WHERE comic_id IS NOT NULL"
    ).fetchall()
    for b in bid_rows:
        legacy_cid = b["comic_id"]
        survivor_id = legacy_to_survivor.get(legacy_cid)
        grade = legacy_grade.get(legacy_cid)
        if survivor_id is None or grade is None:
            bids_with_null += 1
            continue
        fmv_id = fmv_by_pair.get((survivor_id, grade))
        if fmv_id is None:
            bids_with_null += 1
            continue
        conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, b["id"]))
        bids_linked += 1
    return bids_linked, bids_with_null


def _migrate_junction(
    conn: sqlite3.Connection,
    legacy_to_survivor: dict[int, int],
    legacy_grade: dict[int, float | None],
    fmv_by_pair: dict[tuple[int, float], int],
) -> tuple[int, int]:
    """Migrate bid_comics → bid_fmvs. Grade resolution per junction row:
    - use the junction comic's OWN legacy grade if non-NULL (preserves
      per-comic grade fidelity for lots with mixed grades)
    - fall back to the bid's primary legacy grade only when the junction
      comic has NULL grade
    - skip the row entirely when BOTH are NULL

    Returns (junction_inserted, junction_skipped)."""
    junction_inserted = 0
    junction_skipped = 0
    bc_rows = conn.execute(
        "SELECT bc.bid_id, bc.comic_id, bc.is_primary "
        "FROM bid_comics bc "
        "JOIN bids b ON b.id = bc.bid_id"
    ).fetchall()
    for bc in bc_rows:
        bid_row = conn.execute(
            "SELECT comic_id FROM bids WHERE id=?", (bc["bid_id"],)
        ).fetchone()
        if bid_row is None or bid_row["comic_id"] is None:
            junction_skipped += 1
            continue

        # Per-junction grade fidelity: prefer the junction comic's own legacy
        # grade. Only fall back to the primary's grade when the junction
        # comic was ungraded in legacy data.
        junction_grade = legacy_grade.get(bc["comic_id"])
        primary_grade = legacy_grade.get(bid_row["comic_id"])
        grade = junction_grade if junction_grade is not None else primary_grade
        if grade is None:
            junction_skipped += 1
            continue

        survivor_id = legacy_to_survivor.get(bc["comic_id"])
        if survivor_id is None:
            junction_skipped += 1
            continue

        key = (survivor_id, grade)
        if key not in fmv_by_pair:
            # Junction comic doesn't carry this grade in legacy data; create
            # a NULL-valuation fmv stub so the junction row can land.
            conn.execute(
                "INSERT INTO fmv (comic_id, grade, updated_at) VALUES (?, ?, NULL)",
                (survivor_id, grade),
            )
            new_id = conn.execute(
                "SELECT id FROM fmv WHERE comic_id=? AND grade=?",
                (survivor_id, grade),
            ).fetchone()["id"]
            fmv_by_pair[key] = new_id
        fmv_id = fmv_by_pair[key]
        conn.execute(
            """
            INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary)
            VALUES (?, ?, ?)
            """,
            (bc["bid_id"], fmv_id, bc["is_primary"]),
        )
        junction_inserted += 1
    return junction_inserted, junction_skipped


def _rebuild_tables(
    conn: sqlite3.Connection,
    survivor_ids: list[int],
) -> int:
    """Delete non-survivor comics rows and rebuild comics/bids tables to the
    post-split shape. Drops bid_comics entirely. Returns the count of
    collapsed (deleted) shadow rows.

    Caller is responsible for transaction wrapping and PRAGMA foreign_keys
    toggling. This function assumes both have already been handled."""
    legacy_count = conn.execute(
        "SELECT COUNT(*) AS n FROM comics"
    ).fetchone()["n"]

    if survivor_ids:
        placeholders = ",".join("?" * len(survivor_ids))
        conn.execute(
            f"DELETE FROM comics WHERE id NOT IN ({placeholders})",
            survivor_ids,
        )
    collapsed = legacy_count - len(survivor_ids)

    # SQLite has no DROP COLUMN before 3.35 and no DROP CONSTRAINT at all,
    # so use the rename-and-rebuild dance (sqlite.org/lang_altertable.html
    # section 7).

    # Comics: drop grade, fmv_*, change UNIQUE to (title, issue, year).
    conn.execute("ALTER TABLE comics RENAME TO comics_legacy")
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER NOT NULL,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(title, issue, year)
        )
    """)
    conn.execute("""
        INSERT INTO comics
        (id, title, issue, year, locg_id, locg_variant_id, created_at)
        SELECT id, title, issue, year, locg_id, locg_variant_id, created_at
        FROM comics_legacy
    """)
    conn.execute("DROP TABLE comics_legacy")

    # Bids: drop comic_id, add ON DELETE SET NULL to fmv_id FK.
    # fmv_id is already populated by _repoint_bids.
    conn.execute("ALTER TABLE bids RENAME TO bids_legacy")
    conn.execute("""
        CREATE TABLE bids (
            id                  INTEGER PRIMARY KEY,
            item_id             TEXT NOT NULL,
            fmv_id              INTEGER REFERENCES fmv(id) ON DELETE SET NULL,
            max_bid             REAL NOT NULL,
            bid_offset          INTEGER DEFAULT 6,
            snipe_group         INTEGER DEFAULT 0,
            status              TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),
            winning_bid         REAL,
            seller              TEXT,
            auction_end_at      TEXT,
            local_snipe_at      TEXT,
            local_snipe_result  TEXT,
            notes               TEXT,
            ebay_title          TEXT,
            status_mirror       TEXT,
            cached_current_bid  TEXT,
            cached_at           TEXT,
            added_at            TEXT DEFAULT (datetime('now')),
            resolved_at         TEXT
        )
    """)
    conn.execute("""
        INSERT INTO bids (
            id, item_id, fmv_id, max_bid, bid_offset, snipe_group, status,
            winning_bid, seller, auction_end_at, local_snipe_at,
            local_snipe_result, notes, ebay_title, status_mirror,
            cached_current_bid, cached_at, added_at, resolved_at
        )
        SELECT
            id, item_id, fmv_id, max_bid, bid_offset, snipe_group, status,
            winning_bid, seller, auction_end_at, local_snipe_at,
            local_snipe_result, notes, ebay_title, status_mirror,
            cached_current_bid, cached_at, added_at, resolved_at
        FROM bids_legacy
    """)
    conn.execute("DROP TABLE bids_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_fmv ON bids(fmv_id)")

    # bid_comics: drop entirely — replaced by bid_fmvs.
    conn.execute("DROP TABLE IF EXISTS bid_comics")

    # PRAGMA foreign_key_check before commit: catch dangling FKs in the
    # rebuilt tables. If the migration logic is sound, this returns no rows.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"fmv split migration: FK violations after rebuild: {violations}"
        )
    return collapsed


def _clean_dangling_refs(conn: sqlite3.Connection) -> int:
    """Pre-clean dangling legacy refs so the migration's dict lookups don't
    KeyError. Affects bids.comic_id and bid_comics.comic_id. Returns the count
    of cleaned references (informational; no impact on migration outcome)."""
    # Detect dangling bids.comic_id and NULL them out.
    dangling_bids = conn.execute(
        "SELECT id, comic_id FROM bids "
        "WHERE comic_id IS NOT NULL "
        "AND comic_id NOT IN (SELECT id FROM comics)"
    ).fetchall()
    if dangling_bids:
        log.warning(
            "FMV split migration: %d bid rows reference deleted comics; "
            "NULLing their comic_id before migration.",
            len(dangling_bids),
        )
        for row in dangling_bids:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "UPDATE bids SET comic_id=NULL WHERE id=?", (row["id"],)
            )
            conn.execute("PRAGMA foreign_keys=ON")

    # Delete dangling bid_comics rows (those whose comic_id is gone).
    dangling_bc = conn.execute(
        "SELECT bid_id, comic_id FROM bid_comics "
        "WHERE comic_id NOT IN (SELECT id FROM comics)"
    ).fetchall()
    if dangling_bc:
        log.warning(
            "FMV split migration: %d bid_comics rows reference deleted comics; "
            "deleting before migration.",
            len(dangling_bc),
        )
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "DELETE FROM bid_comics WHERE comic_id NOT IN (SELECT id FROM comics)"
        )
        conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    return len(dangling_bids) + len(dangling_bc)


def _migrate_fmv_split(
    conn: sqlite3.Connection,
    db_path: Path | None = None,
) -> None:
    """Collapse comics shadow rows, manufacture fmv rows for every legacy
    (comic_id, grade) pair, and repoint bids/junction at the new fmv_id.

    Idempotent — gated on the presence of the legacy `comics.grade` column.
    Once the rebuild step runs, this column is gone and the function returns
    immediately on subsequent calls.

    All write work (steps 2-7) runs inside a single BEGIN EXCLUSIVE transaction
    so a crash mid-migration rolls back as one atomic unit. PRAGMA foreign_keys
    is toggled OUTSIDE the transaction (PRAGMA is a no-op mid-transaction in
    SQLite) and restored via try/finally so a raise leaves the connection in
    a sane state.

    Survivor priority: locg_id NOT NULL > fmv_low NOT NULL > most recent
    fmv_updated_at > lowest id."""
    if not _has_legacy_columns(conn):
        return  # already migrated

    # Take a binary backup before any destructive work. Refuse to proceed if
    # we can't (so the operator always has a rollback path).
    if db_path is not None:
        try:
            _ensure_backup(db_path)
        except Exception as e:
            raise RuntimeError(
                f"FMV split migration: failed to create backup of {db_path}: {e}. "
                "Refusing to migrate without a recovery snapshot."
            ) from e

    # Pre-clean dangling refs so .get() fallbacks below have minimal triggers.
    # Runs outside the transaction (it's idempotent and a recoverable step).
    _clean_dangling_refs(conn)

    # Finalize any pending implicit transaction so PRAGMA foreign_keys=OFF
    # below actually takes effect (PRAGMA is a no-op inside a transaction).
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        # BEGIN EXCLUSIVE serializes the migration across any concurrent
        # writer. If another connection holds an open transaction this raises
        # OperationalError — surface it cleanly so the operator can stop the
        # other writer rather than corrupting state.
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                "FMV split migration: cannot acquire exclusive lock — another "
                f"process is using the DB. Stop all writers and retry. ({e})"
            ) from e

        try:
            survivor_map, legacy_rows, legacy_to_survivor = _compute_survivors(conn)
            fmv_inserted = _manufacture_fmv_rows(conn, legacy_rows, legacy_to_survivor)
            fmv_by_pair, legacy_grade = _build_fmv_lookup(conn, legacy_rows)
            bids_linked, bids_with_null = _repoint_bids(
                conn, legacy_to_survivor, legacy_grade, fmv_by_pair
            )
            junction_inserted, junction_skipped = _migrate_junction(
                conn, legacy_to_survivor, legacy_grade, fmv_by_pair
            )

            survivor_ids = list({s for s in survivor_map.values()})
            collapsed = _rebuild_tables(conn, survivor_ids)

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    log.warning(
        "FMV split migration complete: collapsed %d shadow comics, "
        "inserted %d fmv rows, linked %d bids to fmv_id (%d bids left with NULL "
        "fmv_id due to missing grade), migrated %d bid_fmvs junction rows (skipped %d).",
        collapsed, fmv_inserted, bids_linked, bids_with_null,
        junction_inserted, junction_skipped,
    )


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    # On a legacy DB the executescript below is a no-op for `comics` (its
    # CREATE IF NOT EXISTS won't replace the legacy shape) but adds `fmv` and
    # `bid_fmvs`. _migrate_fmv_split then upgrades the legacy tables.
    # On a fresh DB executescript creates the post-split shape directly.
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:
        conn.close()
        raise
    _apply_migrations(conn, path)
    os.chmod(path, 0o600)
    return conn


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
) -> int:
    """Upsert a comic identity row keyed by (title, issue, year). Returns id.

    Per-grade FMV lives in the `fmv` table — call `upsert_fmv(conn, id, grade, ...)`."""
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, locg_id, locg_variant_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year) DO UPDATE SET
            locg_id         = COALESCE(excluded.locg_id,         locg_id),
            locg_variant_id = COALESCE(excluded.locg_variant_id, locg_variant_id)
        """,
        (title, issue, year, locg_id, locg_variant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year=?",
        (title, issue, year),
    ).fetchone()
    return row["id"]


def upsert_fmv(
    conn: sqlite3.Connection,
    comic_id: int,
    grade: float,
    low: float | None,
    high: float | None,
    comps: int | None,
    confidence: str | None,
    notes: str | None,
) -> int:
    """Upsert per-grade FMV row. Returns the fmv.id.

    COALESCE on every value field preserves existing entries when partial
    updates arrive. `updated_at` is bumped only when at least one valuation
    field is non-NULL on this call — so a grade-only stub stays with
    updated_at=NULL until real research lands."""
    if grade is None:
        raise ValueError("upsert_fmv: grade is required")
    now = datetime.now(timezone.utc).isoformat()
    any_val = any(v is not None for v in (low, high, comps, confidence, notes))
    conn.execute(
        """
        INSERT INTO fmv (comic_id, grade, low, high, comps, confidence,
                         notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(comic_id, grade) DO UPDATE SET
            low        = COALESCE(excluded.low,        low),
            high       = COALESCE(excluded.high,       high),
            comps      = COALESCE(excluded.comps,      comps),
            confidence = COALESCE(excluded.confidence, confidence),
            notes      = COALESCE(excluded.notes,      notes),
            updated_at = CASE WHEN ? THEN excluded.updated_at ELSE updated_at END
        """,
        (comic_id, grade, low, high, comps, confidence, notes,
         now if any_val else None,
         1 if any_val else 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM fmv WHERE comic_id=? AND grade=?",
        (comic_id, grade),
    ).fetchone()
    return row["id"]


def set_bid_fmv(
    conn: sqlite3.Connection,
    bid_id: int,
    fmv_id: int | None,
) -> None:
    """Set bids.fmv_id. None clears the linkage (e.g. unclassified bid)."""
    conn.execute("UPDATE bids SET fmv_id = ? WHERE id = ?", (fmv_id, bid_id))
    conn.commit()


def get_fmv_for_bid(
    conn: sqlite3.Connection,
    bid_id: int,
) -> sqlite3.Row | None:
    """Return the fmv row this bid points at via fmv_id, or None if unlinked.
    Includes comic_id so callers can read it without a second query."""
    return conn.execute(
        """
        SELECT f.*
        FROM bids b
        JOIN fmv  f ON f.id = b.fmv_id
        WHERE b.id = ?
        """,
        (bid_id,),
    ).fetchone()


def primary_comic_id_for_bid(
    conn: sqlite3.Connection, bid_id: int
) -> int | None:
    """Resolve the comic_id of the bid's primary fmv linkage. Returns None
    when the bid has no fmv_id or the fmv row is gone (FK should prevent
    the latter, but the helper is defensive). Replaces three inline
    `SELECT comic_id FROM fmv WHERE id=?` lookups in server/main.py."""
    row = conn.execute(
        """
        SELECT f.comic_id
        FROM bids b
        JOIN fmv  f ON f.id = b.fmv_id
        WHERE b.id = ?
        """,
        (bid_id,),
    ).fetchone()
    return row["comic_id"] if row else None


def link_fmv_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    fmv_id: int,
    is_primary: bool = False,
) -> None:
    """Insert into bid_fmvs. If is_primary, demote prior primary entries for
    this bid and mirror to bids.fmv_id. Idempotent."""
    if is_primary:
        conn.execute(
            "UPDATE bid_fmvs SET is_primary=0 WHERE bid_id=? AND fmv_id != ?",
            (bid_id, fmv_id),
        )
        conn.execute(
            """
            INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(bid_id, fmv_id) DO UPDATE SET is_primary = 1
            """,
            (bid_id, fmv_id),
        )
        conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, 0)",
            (bid_id, fmv_id),
        )
    conn.commit()


def insert_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    fmv_id: int | None,
    bid_offset: int,
    snipe_group: int,
    seller: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, fmv_id, bid_offset, snipe_group, seller)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, fmv_id, bid_offset, snipe_group, seller),
    )
    conn.commit()
    return cur.lastrowid


def get_bid_by_item_id(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def get_fmvs_for_bid(conn: sqlite3.Connection, bid_id: int) -> list[sqlite3.Row]:
    """All fmv rows linked to a bid via bid_fmvs, JOINed with comic identity.
    Primary first, then by numeric issue order."""
    return conn.execute(
        """
        SELECT f.id AS fmv_id, f.comic_id, f.grade,
               f.low, f.high, f.comps, f.confidence, f.notes, f.updated_at,
               c.title, c.issue, c.year, c.locg_id, c.locg_variant_id,
               bf.is_primary
        FROM bid_fmvs bf
        JOIN fmv    f ON f.id = bf.fmv_id
        JOIN comics c ON c.id = f.comic_id
        WHERE bf.bid_id = ?
        ORDER BY bf.is_primary DESC,
                 CAST(c.issue AS INTEGER),
                 c.issue
        """,
        (bid_id,),
    ).fetchall()


def get_primary_fmv_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT f.id AS fmv_id, f.comic_id, f.grade,
               f.low, f.high, f.comps, f.confidence, f.notes,
               c.title, c.issue, c.year
        FROM bid_fmvs bf
        JOIN fmv    f ON f.id = bf.fmv_id
        JOIN comics c ON c.id = f.comic_id
        WHERE bf.bid_id = ? AND bf.is_primary = 1
        LIMIT 1
        """,
        (bid_id,),
    ).fetchone()


def update_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    bid_offset: int,
    snipe_group: int,
) -> None:
    conn.execute(
        "UPDATE bids SET max_bid=?, bid_offset=?, snipe_group=? WHERE item_id=? AND status='PENDING'",
        (max_bid, bid_offset, snipe_group, item_id),
    )
    conn.commit()


def update_bid_status(
    conn: sqlite3.Connection,
    item_id: str,
    status: str,
    winning_bid: float | None = None,
    resolved_at: str | None = None,
    status_mirror: str | None = None,
) -> None:
    # COALESCE on status_mirror so callers that don't have a fresh mirror value
    # (e.g. the eBay fallback path) don't clobber the last-known mirror status.
    # Caller must conn.commit() — this helper is hot-path inside loops where
    # the caller batches the commit at the end of the cycle.
    conn.execute(
        "UPDATE bids SET status=?, winning_bid=?, resolved_at=?, "
        "status_mirror=COALESCE(?, status_mirror) "
        "WHERE item_id=? AND status NOT IN ('PURGED')",
        (status, winning_bid, resolved_at, status_mirror, item_id),
    )


def cache_gixen_data(
    conn: sqlite3.Connection,
    item_id: str,
    title: str | None,
    seller: str | None,
    current_bid: str | None,
) -> None:
    """Cache Gixen-sourced fields. Does not touch auction_end_at — that's
    eBay's domain (Gixen only provides relative time-to-end). COALESCE keeps
    the existing value when the caller passes None.

    cached_at is only refreshed when at least one input field is non-NULL,
    so all-NULL writes (common for SCHEDULED snipes whose Gixen row hasn't
    populated current_bid yet) don't make the freshness indicator lie about
    when we last got real data.

    Caller must conn.commit() — this helper is hot-path inside the
    _sync_gixen loop where commits are batched at the end of the cycle.
    """
    has_data = any(v is not None for v in (title, seller, current_bid))
    if not has_data:
        return  # nothing to write, don't bump cached_at
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE bids SET "
        "ebay_title=COALESCE(?, ebay_title), "
        "seller=COALESCE(?, seller), "
        "cached_current_bid=COALESCE(?, cached_current_bid), "
        "cached_at=? "
        "WHERE item_id=? AND status NOT IN ('PURGED')",
        (title, seller, current_bid, now, item_id),
    )


def delete_bid(conn: sqlite3.Connection, item_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id=? AND status NOT IN ('PURGED')",
        (now, item_id),
    )
    conn.commit()


def list_comics(
    conn: sqlite3.Connection,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
) -> list[sqlite3.Row]:
    """List comic identity rows. If `grade` is supplied, JOIN fmv and return
    valuation columns inline (renamed to the legacy `fmv_*` keys for response
    compatibility). Without `grade`, returns identity rows only."""
    clauses, params = [], []
    if title is not None:
        clauses.append("LOWER(c.title) = LOWER(?)")
        params.append(title)
    if issue is not None:
        clauses.append("c.issue = ?")
        params.append(issue)
    if year is not None:
        clauses.append("c.year = ?")
        params.append(year)

    if grade is not None:
        clauses.append("f.grade = ?")
        params.append(grade)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT c.*,
                   f.grade,
                   f.low  AS fmv_low,
                   f.high AS fmv_high,
                   f.comps AS fmv_comps,
                   f.confidence AS fmv_confidence,
                   f.notes AS fmv_notes,
                   f.updated_at AS fmv_updated_at
            FROM comics c
            JOIN fmv f ON f.comic_id = c.id
            {where}
            ORDER BY c.id
        """
    else:
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT c.* FROM comics c
            {where}
            ORDER BY c.id
        """
    return conn.execute(sql, params).fetchall()


def get_all_bids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bids ORDER BY added_at DESC").fetchall()


def get_pending_bids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bids WHERE status='PENDING'").fetchall()


def mark_bids_purged(conn: sqlite3.Connection, item_ids: list[str]) -> None:
    if not item_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    # placeholders contains only '?' chars — no user data is interpolated
    placeholders = ",".join("?" * len(item_ids))
    conn.execute(
        f"UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id IN ({placeholders})",
        [now, *item_ids],
    )
    conn.commit()


def set_auction_end_time(conn: sqlite3.Connection, item_id: str, end_time_iso: str) -> None:
    conn.execute(
        "UPDATE bids SET auction_end_at=? WHERE item_id=? AND status='PENDING'",
        (end_time_iso, item_id),
    )
    conn.commit()


def get_bids_ready_to_snipe(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """Return PENDING bids whose fire time (auction_end_at - bid_offset) has arrived."""
    return conn.execute(
        """
        SELECT * FROM bids
        WHERE status = 'PENDING'
          AND local_snipe_at IS NULL
          AND auction_end_at IS NOT NULL
          AND datetime(auction_end_at, '-' || bid_offset || ' seconds') <= datetime(?)
        """,
        (now_iso,),
    ).fetchall()


def set_local_snipe_result(
    conn: sqlite3.Connection,
    item_id: str,
    fired_at: str,
    result: str,
) -> None:
    conn.execute(
        "UPDATE bids SET local_snipe_at=?, local_snipe_result=? WHERE item_id=? AND status='PENDING'",
        (fired_at, result, item_id),
    )
    conn.commit()
