from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".gixen-server" / "db.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comics (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    issue           TEXT NOT NULL,
    year            INTEGER NOT NULL,
    grade           REAL,
    fmv_low         REAL,
    fmv_high        REAL,
    fmv_comps       INTEGER,
    fmv_confidence  TEXT CHECK(fmv_confidence IN ('high', 'medium', 'low') OR fmv_confidence IS NULL),
    fmv_notes       TEXT,
    fmv_updated_at  TEXT,
    locg_id         INTEGER,
    locg_variant_id INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(title, issue, year, grade)
);

CREATE TABLE IF NOT EXISTS bids (
    id              INTEGER PRIMARY KEY,
    item_id         TEXT NOT NULL,
    comic_id        INTEGER REFERENCES comics(id),
    max_bid         REAL NOT NULL,
    bid_offset      INTEGER DEFAULT 6,
    snipe_group     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),
    winning_bid     REAL,
    seller          TEXT,
    auction_end_at      TEXT,
    local_snipe_at      TEXT,
    local_snipe_result  TEXT,
    notes               TEXT,
    added_at            TEXT DEFAULT (datetime('now')),
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);

CREATE TABLE IF NOT EXISTS bid_comics (
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bid_id, comic_id)
);

CREATE INDEX IF NOT EXISTS idx_bid_comics_bid ON bid_comics(bid_id);

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

CREATE TABLE IF NOT EXISTS bid_fmvs (
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    fmv_id     INTEGER NOT NULL REFERENCES fmv(id)  ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bid_id, fmv_id)
);

CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id);
"""


_COLUMN_MIGRATIONS = [
    # bids columns added since the original schema
    "ALTER TABLE bids ADD COLUMN ebay_title TEXT",
    "ALTER TABLE bids ADD COLUMN status_mirror TEXT",
    "ALTER TABLE bids ADD COLUMN cached_current_bid TEXT",
    "ALTER TABLE bids ADD COLUMN cached_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_result TEXT",
    # comics columns added since the original schema
    "ALTER TABLE comics ADD COLUMN locg_id INTEGER",
    "ALTER TABLE comics ADD COLUMN locg_variant_id INTEGER",
    # FMV split (2026-05-13): fmv_id is the single FK from bids into the
    # per-grade fmv table. ALTER is idempotent (caught by the
    # "duplicate column" handler in _apply_migrations).
    "ALTER TABLE bids ADD COLUMN fmv_id INTEGER REFERENCES fmv(id)",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
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

    # Legacy backfill from before bid_comics existed. Only runs while the
    # legacy bids.comic_id column still exists (i.e. pre-FMV-split DB).
    bid_cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    if "comic_id" in bid_cols:
        conn.execute(
            """
            INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary)
            SELECT id, comic_id, 1 FROM bids WHERE comic_id IS NOT NULL
            """
        )
        conn.commit()

    _migrate_fmv_split(conn)


def _migrate_fmv_split(conn: sqlite3.Connection) -> None:
    """Collapse comics shadow rows, manufacture fmv rows for every legacy
    (comic_id, grade) pair, and repoint bids/junction at the new fmv_id.

    Idempotent — gated on the presence of the legacy `comics.grade` column.
    Once the rebuild step runs, this column is gone and the function returns
    immediately on subsequent calls.

    Survivor priority: locg_id NOT NULL > fmv_low NOT NULL > most recent
    fmv_updated_at > lowest id."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    if "grade" not in cols:
        return  # already migrated

    import logging
    log = logging.getLogger(__name__)

    # 1. Compute survivor id per (title, issue, year) group.
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

    # 2. Build fmv rows. For every legacy comic row with grade IS NOT NULL,
    #    insert an fmv row at (survivor_id, grade) carrying the legacy
    #    valuation tuple. Conflicts on (survivor_id, grade) resolved by
    #    "row with fmv_low NOT NULL wins"; the loser's notes are prefixed.
    legacy_rows = conn.execute(
        "SELECT id, title, issue, year, grade, fmv_low, fmv_high, fmv_comps, "
        "fmv_confidence, fmv_notes, fmv_updated_at "
        "FROM comics"
    ).fetchall()
    legacy_to_survivor: dict[int, int] = {
        r["id"]: survivor_map[(r["title"], r["issue"], r["year"])]
        for r in legacy_rows
    }
    fmv_inserted = 0
    for row in legacy_rows:
        if row["grade"] is None:
            continue
        survivor_id = legacy_to_survivor[row["id"]]
        existing = conn.execute(
            "SELECT id, low FROM fmv WHERE comic_id=? AND grade=?",
            (survivor_id, row["grade"]),
        ).fetchone()
        if existing is not None:
            if existing["low"] is None and row["fmv_low"] is not None:
                conn.execute(
                    """
                    UPDATE fmv
                    SET low=?, high=?, comps=?, confidence=?,
                        notes = COALESCE(?, notes),
                        updated_at=?
                    WHERE id=?
                    """,
                    (row["fmv_low"], row["fmv_high"], row["fmv_comps"],
                     row["fmv_confidence"],
                     f"[merged from legacy comic_id={row['id']}] "
                     + (row["fmv_notes"] or ""),
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

    # 3. (comic_id, grade) → fmv_id lookup used by steps 4 and 5.
    fmv_lookup_rows = conn.execute("SELECT id, comic_id, grade FROM fmv").fetchall()
    fmv_by_pair: dict[tuple[int, float], int] = {
        (r["comic_id"], r["grade"]): r["id"] for r in fmv_lookup_rows
    }
    legacy_grade: dict[int, float | None] = {
        r["id"]: r["grade"] for r in legacy_rows
    }

    # 4. Repoint bids. For each bid with comic_id NOT NULL, resolve survivor
    #    and grade, look up fmv_id, set bids.fmv_id.
    bids_linked = 0
    bids_with_null = 0
    bid_rows = conn.execute(
        "SELECT id, comic_id FROM bids WHERE comic_id IS NOT NULL"
    ).fetchall()
    for b in bid_rows:
        legacy_cid = b["comic_id"]
        survivor_id = legacy_to_survivor[legacy_cid]
        grade = legacy_grade[legacy_cid]
        if grade is None:
            bids_with_null += 1
            continue
        fmv_id = fmv_by_pair[(survivor_id, grade)]
        conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, b["id"]))
        bids_linked += 1

    # 5. Migrate bid_comics → bid_fmvs. Resolve via the bid's primary grade
    #    (which is the legacy comics.grade of bids.comic_id). Junction rows
    #    whose bid has no resolvable grade are skipped.
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
        grade = legacy_grade[bid_row["comic_id"]]
        if grade is None:
            junction_skipped += 1
            continue
        survivor_id = legacy_to_survivor[bc["comic_id"]]
        key = (survivor_id, grade)
        if key not in fmv_by_pair:
            # Junction comic didn't carry this grade in legacy data; create
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

    # 6. Delete non-survivor comics rows. The legacy bids.comic_id and
    #    bid_comics still reference these shadow rows, but those tables are
    #    about to be rebuilt (step 7) and the new schema doesn't carry that
    #    FK. Disable FK enforcement for the delete and rebuild as a single
    #    block. PRAGMA foreign_keys is a no-op inside a transaction, so
    #    commit any pending writes first.
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")

    survivor_ids = list({s for s in survivor_map.values()})
    if survivor_ids:
        placeholders = ",".join("?" * len(survivor_ids))
        conn.execute(
            f"DELETE FROM comics WHERE id NOT IN ({placeholders})",
            survivor_ids,
        )
    collapsed = len(legacy_rows) - len(survivor_ids)

    # 7. Rebuild comics and bids to drop legacy columns. SQLite has no DROP
    #    COLUMN before 3.35 and no DROP CONSTRAINT at all, so use the standard
    #    rename-and-rebuild dance. Wrap in a savepoint so a failure leaves
    #    the DB recoverable (caller restores from backup).
    #
    #    This is the standard pattern documented at sqlite.org/lang_altertable.html
    #    (section 7, "Making other kinds of table schema changes").
    conn.execute("SAVEPOINT fmv_split_rebuild")
    try:
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

        # Bids: drop comic_id. fmv_id is already populated.
        conn.execute("ALTER TABLE bids RENAME TO bids_legacy")
        conn.execute("""
            CREATE TABLE bids (
                id                  INTEGER PRIMARY KEY,
                item_id             TEXT NOT NULL,
                fmv_id              INTEGER REFERENCES fmv(id),
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

        # PRAGMA foreign_key_check before release: catch dangling FKs in the
        # rebuilt tables. If the migration logic is sound, this returns no rows.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"fmv split migration: FK violations after rebuild: {violations}"
            )
        conn.execute("RELEASE fmv_split_rebuild")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT fmv_split_rebuild")
        conn.execute("RELEASE fmv_split_rebuild")
        conn.execute("PRAGMA foreign_keys=ON")
        raise

    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()

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
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:
        conn.close()
        raise
    _apply_migrations(conn)
    os.chmod(path, 0o600)
    return conn


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int,
    grade: float | None,
    fmv_low: float | None,
    fmv_high: float | None,
    fmv_comps: int | None,
    fmv_confidence: str | None,
    fmv_notes: str | None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, grade, fmv_low, fmv_high,
                            fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at,
                            locg_id, locg_variant_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year, grade) DO UPDATE SET
            fmv_low         = COALESCE(excluded.fmv_low,        fmv_low),
            fmv_high        = COALESCE(excluded.fmv_high,       fmv_high),
            fmv_comps       = COALESCE(excluded.fmv_comps,      fmv_comps),
            fmv_confidence  = COALESCE(excluded.fmv_confidence, fmv_confidence),
            fmv_notes       = COALESCE(excluded.fmv_notes,      fmv_notes),
            fmv_updated_at  = CASE WHEN excluded.fmv_low IS NOT NULL THEN excluded.fmv_updated_at ELSE fmv_updated_at END,
            locg_id         = COALESCE(excluded.locg_id,         locg_id),
            locg_variant_id = COALESCE(excluded.locg_variant_id, locg_variant_id)
        """,
        (title, issue, year, grade, fmv_low, fmv_high,
         fmv_comps, fmv_confidence, fmv_notes, now,
         locg_id, locg_variant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year=? AND grade IS ?",
        (title, issue, year, grade),
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
    comic_id: int | None,
    bid_offset: int,
    snipe_group: int,
    seller: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, comic_id, bid_offset, snipe_group, seller)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, comic_id, bid_offset, snipe_group, seller),
    )
    conn.commit()
    return cur.lastrowid


def get_bid_by_item_id(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def link_comic_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    comic_id: int,
    is_primary: bool = False,
) -> None:
    """Add a comic to a bid's set. If is_primary, demote any prior primary,
    promote this one, and mirror to bids.comic_id (backward-compat pointer).
    Idempotent: re-running with the same args is a no-op aside from primary
    bookkeeping."""
    if is_primary:
        conn.execute(
            "UPDATE bid_comics SET is_primary=0 WHERE bid_id=? AND comic_id != ?",
            (bid_id, comic_id),
        )
        conn.execute(
            """
            INSERT INTO bid_comics (bid_id, comic_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(bid_id, comic_id) DO UPDATE SET is_primary = 1
            """,
            (bid_id, comic_id),
        )
        conn.execute("UPDATE bids SET comic_id=? WHERE id=?", (comic_id, bid_id))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary) VALUES (?, ?, 0)",
            (bid_id, comic_id),
        )
    conn.commit()


def get_comics_for_bid(conn: sqlite3.Connection, bid_id: int) -> list[sqlite3.Row]:
    """All comics linked to a bid, primary first, then by numeric issue order."""
    return conn.execute(
        """
        SELECT c.*, bc.is_primary
        FROM bid_comics bc
        JOIN comics c ON c.id = bc.comic_id
        WHERE bc.bid_id = ?
        ORDER BY bc.is_primary DESC,
                 CAST(c.issue AS INTEGER),
                 c.issue
        """,
        (bid_id,),
    ).fetchall()


def get_primary_comic_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*
        FROM bid_comics bc
        JOIN comics c ON c.id = bc.comic_id
        WHERE bc.bid_id = ? AND bc.is_primary = 1
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
    clauses, params = [], []
    if title is not None:
        clauses.append("LOWER(title) = LOWER(?)")
        params.append(title)
    if issue is not None:
        clauses.append("issue = ?")
        params.append(issue)
    if year is not None:
        clauses.append("year = ?")
        params.append(year)
    if grade is not None:
        clauses.append("grade = ?")
        params.append(grade)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT * FROM comics {where} ORDER BY id",
        params,
    ).fetchall()


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
