"""Unit tests for server/db.py — all use tmp_path, no disk side effects."""
import sqlite3
import pytest
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import (
    init_db, upsert_comic, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged,
    link_comic_to_bid, get_comics_for_bid, get_primary_comic_for_bid,
)


_COMICS_DDL = """
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
    )
"""

_BID_COMICS_DDL = """
    CREATE TABLE IF NOT EXISTS bid_comics (
        bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
        comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
        is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bid_id, comic_id)
    )
"""


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def db_with_comics(tmp_path):
    """init_db + comics and bid_comics tables for tests that call comic functions.

    After PER-27, init_db no longer creates these tables — they are plugin-owned.
    Tests that exercise upsert_comic, link_comic_to_bid, etc. use this fixture
    instead of db so they don't depend on core creating the tables.
    """
    conn = init_db(tmp_path / "test.db")
    conn.execute(_COMICS_DDL)
    conn.execute(_BID_COMICS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_comics_bid ON bid_comics(bid_id)")
    conn.commit()
    yield conn
    conn.close()


def test_init_creates_tables(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}
    assert "bids" in tables
    assert "comics" not in tables
    assert "bid_comics" not in tables


def test_bids_comic_id_has_no_foreign_key(db):
    """Fresh init_db creates bids.comic_id with no FK declaration."""
    fk_rows = db.execute("PRAGMA foreign_key_list(bids)").fetchall()
    assert len(fk_rows) == 0


def test_wal_mode_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_upsert_comic_inserts(db_with_comics):
    comic_id = upsert_comic(db_with_comics, title="Amazing Spider-Man", issue="300",
                            year=1988, grade=9.2,
                            fmv_low=800.0, fmv_high=1000.0,
                            fmv_comps=12, fmv_confidence="high",
                            fmv_notes="Key issue")
    assert isinstance(comic_id, int)
    row = db_with_comics.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["grade"] == 9.2
    assert row["fmv_confidence"] == "high"


def test_upsert_comic_updates_on_conflict(db_with_comics):
    id1 = upsert_comic(db_with_comics, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=500.0, fmv_high=700.0,
                       fmv_comps=5, fmv_confidence="medium", fmv_notes="")
    id2 = upsert_comic(db_with_comics, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=550.0, fmv_high=750.0,
                       fmv_comps=8, fmv_confidence="high", fmv_notes="Updated")
    assert id1 == id2
    row = db_with_comics.execute("SELECT fmv_low FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["fmv_low"] == 550.0


def test_insert_bid(db):
    bid_id = insert_bid(db, item_id="123456789", max_bid=800.0,
                        comic_id=None, bid_offset=6, snipe_group=0,
                        seller="seller1")
    assert isinstance(bid_id, int)
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["item_id"] == "123456789"
    assert row["status"] == "PENDING"
    assert row["max_bid"] == 800.0


def test_insert_bid_links_comic(db_with_comics):
    comic_id = upsert_comic(db_with_comics, "Hulk", "181", 1974, 9.0,
                            50.0, 70.0, 10, "high", "")
    bid_id = insert_bid(db_with_comics, "987654321", 60.0, comic_id, 6, 0, "seller2")
    row = db_with_comics.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["comic_id"] == comic_id


def test_get_bid_by_item_id(db):
    insert_bid(db, "111222333", 50.0, None, 6, 0, "s")
    row = get_bid_by_item_id(db, "111222333")
    assert row is not None
    assert row["item_id"] == "111222333"


def test_get_bid_by_item_id_missing(db):
    assert get_bid_by_item_id(db, "999999999") is None


def test_update_bid(db):
    insert_bid(db, "444555666", 50.0, None, 6, 0, "s")
    update_bid(db, "444555666", max_bid=60.0, bid_offset=10, snipe_group=1)
    row = get_bid_by_item_id(db, "444555666")
    assert row["max_bid"] == 60.0
    assert row["snipe_group"] == 1


def test_update_bid_status(db):
    insert_bid(db, "777888999", 100.0, None, 6, 0, "s")
    update_bid_status(db, "777888999", status="WON",
                      winning_bid=85.0, resolved_at="2026-04-25T12:00:00")
    row = get_bid_by_item_id(db, "777888999")
    assert row["status"] == "WON"
    assert row["winning_bid"] == 85.0
    assert row["resolved_at"] == "2026-04-25T12:00:00"


def test_delete_bid_marks_purged(db):
    insert_bid(db, "555444333", 30.0, None, 6, 0, "s")
    delete_bid(db, "555444333")
    row = get_bid_by_item_id(db, "555444333")
    assert row["status"] == "PURGED"


def test_delete_bid_marks_won_bid_purged(db):
    insert_bid(db, "666777888", 50.0, None, 6, 0, "s")
    update_bid_status(db, "666777888", status="WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    delete_bid(db, "666777888")
    row = get_bid_by_item_id(db, "666777888")
    assert row["status"] == "PURGED"


def test_get_all_bids_returns_list(db):
    insert_bid(db, "100000001", 10.0, None, 6, 0, "s")
    insert_bid(db, "100000002", 20.0, None, 6, 0, "s")
    rows = get_all_bids(db)
    item_ids = [r["item_id"] for r in rows]
    assert "100000001" in item_ids
    assert "100000002" in item_ids


def test_mark_bids_purged_sets_status(db):
    insert_bid(db, "200000001", 50.0, None, 6, 0, "s")
    insert_bid(db, "200000002", 60.0, None, 6, 0, "s")
    mark_bids_purged(db, ["200000001", "200000002"])
    row1 = get_bid_by_item_id(db, "200000001")
    row2 = get_bid_by_item_id(db, "200000002")
    assert row1["status"] == "PURGED"
    assert row2["status"] == "PURGED"
    assert row1["resolved_at"] is not None


def test_mark_bids_purged_transitions_won_bid(db):
    insert_bid(db, "200000003", 50.0, None, 6, 0, "s")
    update_bid_status(db, "200000003", "WON", winning_bid=42.0, resolved_at="2026-04-25T10:00:00")
    mark_bids_purged(db, ["200000003"])
    row = get_bid_by_item_id(db, "200000003")
    assert row["status"] == "PURGED"
    assert row["winning_bid"] == 42.0


def test_mark_bids_purged_empty_list_is_noop(db):
    insert_bid(db, "200000004", 50.0, None, 6, 0, "s")
    mark_bids_purged(db, [])
    row = get_bid_by_item_id(db, "200000004")
    assert row["status"] == "PENDING"


def test_update_bid_noop_on_non_pending(db):
    insert_bid(db, "300000001", 50.0, None, 6, 0, "s")
    update_bid_status(db, "300000001", "WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    update_bid(db, "300000001", max_bid=999.0, bid_offset=6, snipe_group=0)
    row = get_bid_by_item_id(db, "300000001")
    assert row["max_bid"] == 50.0  # unchanged — update_bid guards on status='PENDING'


def test_upsert_comic_persists_locg_ids(db_with_comics):
    """locg_id and locg_variant_id round-trip through upsert_comic."""
    comic_id = upsert_comic(
        db_with_comics, title="Amazing Spider-Man", issue="300", year=1988, grade=9.2,
        fmv_low=800.0, fmv_high=1000.0,
        fmv_comps=12, fmv_confidence="high", fmv_notes="",
        locg_id=6977652, locg_variant_id=6977652,
    )
    row = db_with_comics.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] == 6977652
    assert row["locg_variant_id"] == 6977652


def test_upsert_comic_locg_ids_default_to_null(db_with_comics):
    """Backwards compat: existing call sites without locg_id keep working."""
    comic_id = upsert_comic(
        db_with_comics, title="Hulk", issue="181", year=1974, grade=9.0,
        fmv_low=50.0, fmv_high=70.0,
        fmv_comps=10, fmv_confidence="high", fmv_notes="",
    )
    row = db_with_comics.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] is None
    assert row["locg_variant_id"] is None


def test_upsert_comic_locg_ids_preserved_on_conflict(db_with_comics):
    """A second upsert without locg_id must not clobber the existing values."""
    id1 = upsert_comic(
        db_with_comics, title="X-Men", issue="1", year=1963, grade=8.0,
        fmv_low=500.0, fmv_high=700.0,
        fmv_comps=5, fmv_confidence="medium", fmv_notes="",
        locg_id=12345, locg_variant_id=67890,
    )
    id2 = upsert_comic(
        db_with_comics, title="X-Men", issue="1", year=1963, grade=8.0,
        fmv_low=550.0, fmv_high=750.0,
        fmv_comps=8, fmv_confidence="high", fmv_notes="Updated",
        # No locg_id passed — should preserve prior values
    )
    assert id1 == id2
    row = db_with_comics.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345
    assert row["locg_variant_id"] == 67890


def test_upsert_comic_locg_ids_updated_when_provided(db_with_comics):
    """A second upsert with new locg_id values should overwrite the stored ones."""
    id1 = upsert_comic(
        db_with_comics, title="Spawn", issue="1", year=1992, grade=9.8,
        fmv_low=100.0, fmv_high=150.0,
        fmv_comps=5, fmv_confidence="high", fmv_notes="",
        locg_id=100, locg_variant_id=None,
    )
    id2 = upsert_comic(
        db_with_comics, title="Spawn", issue="1", year=1992, grade=9.8,
        fmv_low=110.0, fmv_high=160.0,
        fmv_comps=6, fmv_confidence="high", fmv_notes="",
        locg_id=200, locg_variant_id=300,
    )
    assert id1 == id2
    row = db_with_comics.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 200
    assert row["locg_variant_id"] == 300


# ---------------------------------------------------------------------------
# bid_comics junction table
# ---------------------------------------------------------------------------

def _make_lot(db_with_comics, item_id="900000001", n=3, series="Daredevil: The Man Without Fear"):
    """Helper: insert a bid + N comics, return (bid_id, [comic_id, ...])."""
    bid_id = insert_bid(db_with_comics, item_id, 100.0, None, 6, 0, "s")
    comic_ids = [
        upsert_comic(db_with_comics, series, str(i), 1993, None,
                     None, None, None, None, None)
        for i in range(1, n + 1)
    ]
    return bid_id, comic_ids


def test_link_comic_to_bid_basic(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=2)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0])
    rows = db_with_comics.execute(
        "SELECT * FROM bid_comics WHERE bid_id=?", (bid_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["comic_id"] == comic_ids[0]
    assert rows[0]["is_primary"] == 0


def test_link_comic_to_bid_idempotent(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=1)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0])
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0])
    rows = db_with_comics.execute(
        "SELECT COUNT(*) AS n FROM bid_comics WHERE bid_id=?", (bid_id,)
    ).fetchone()
    assert rows["n"] == 1


def test_link_comic_to_bid_primary_demotes_prior(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=3)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0], is_primary=True)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[1], is_primary=True)
    rows = db_with_comics.execute(
        "SELECT comic_id, is_primary FROM bid_comics WHERE bid_id=? ORDER BY comic_id",
        (bid_id,),
    ).fetchall()
    by_comic = {r["comic_id"]: r["is_primary"] for r in rows}
    assert by_comic[comic_ids[0]] == 0
    assert by_comic[comic_ids[1]] == 1


def test_link_comic_to_bid_primary_mirrors_to_bids(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=2)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0], is_primary=True)
    row = db_with_comics.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["comic_id"] == comic_ids[0]


def test_link_comic_to_bid_promotes_existing_row(db_with_comics):
    """Calling with is_primary=True on an already-linked non-primary row promotes it."""
    bid_id, comic_ids = _make_lot(db_with_comics, n=2)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0])  # non-primary
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0], is_primary=True)
    row = db_with_comics.execute(
        "SELECT is_primary FROM bid_comics WHERE bid_id=? AND comic_id=?",
        (bid_id, comic_ids[0]),
    ).fetchone()
    assert row["is_primary"] == 1


def test_get_comics_for_bid_orders_primary_first(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=3)
    # Link in reverse to verify primary-first ordering, not insertion order
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[2])
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[1])
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0], is_primary=True)
    rows = get_comics_for_bid(db_with_comics, bid_id)
    assert len(rows) == 3
    assert rows[0]["id"] == comic_ids[0]
    assert rows[0]["is_primary"] == 1
    # Remaining two ordered by issue number
    assert rows[1]["issue"] == "2"
    assert rows[2]["issue"] == "3"


def test_get_comics_for_bid_empty(db_with_comics):
    bid_id = insert_bid(db_with_comics, "900000099", 50.0, None, 6, 0, "s")
    assert get_comics_for_bid(db_with_comics, bid_id) == []


def test_get_primary_comic_for_bid_returns_primary(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=2)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0], is_primary=True)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[1])
    row = get_primary_comic_for_bid(db_with_comics, bid_id)
    assert row is not None
    assert row["id"] == comic_ids[0]


def test_get_primary_comic_for_bid_none_when_only_secondary(db_with_comics):
    bid_id, comic_ids = _make_lot(db_with_comics, n=1)
    link_comic_to_bid(db_with_comics, bid_id, comic_ids[0])  # not primary
    assert get_primary_comic_for_bid(db_with_comics, bid_id) is None


# ---------------------------------------------------------------------------
# FK-removal migration
# ---------------------------------------------------------------------------

def test_fk_removal_migration_drops_comics_reference(tmp_path):
    """On a legacy DB with bids.comic_id REFERENCES comics(id), init_db removes
    the FK. Existing bids rows survive the table rebuild intact."""
    legacy_db_path = tmp_path / "legacy.db"

    # Build a minimal legacy DB with the old bids schema (FK present) + comics.
    raw = sqlite3.connect(str(legacy_db_path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            grade REAL
        );
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER REFERENCES comics(id),
            max_bid REAL NOT NULL,
            bid_offset INTEGER DEFAULT 6,
            snipe_group INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDING',
            winning_bid REAL,
            seller TEXT,
            auction_end_at TEXT,
            local_snipe_at TEXT,
            local_snipe_result TEXT,
            notes TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
    """)
    raw.execute("INSERT INTO comics (title, issue, year) VALUES ('Hulk', '181', 1974)")
    raw.execute("INSERT INTO bids (item_id, max_bid, comic_id) VALUES ('legacy001', 50.0, 1)")
    raw.commit()
    raw.close()

    conn = init_db(legacy_db_path)
    try:
        fk_after = conn.execute("PRAGMA foreign_key_list(bids)").fetchall()
        assert not any(row["table"] == "comics" for row in fk_after)
        row = conn.execute(
            "SELECT item_id, max_bid, comic_id FROM bids WHERE item_id='legacy001'"
        ).fetchone()
        assert row is not None
        assert row["max_bid"] == 50.0
        assert row["comic_id"] == 1
    finally:
        conn.close()


def test_fk_removal_migration_is_idempotent(tmp_path):
    """Calling init_db twice on a DB that already has no FK is a no-op."""
    db_path = tmp_path / "nofk.db"
    conn = init_db(db_path)
    assert len(conn.execute("PRAGMA foreign_key_list(bids)").fetchall()) == 0
    conn.close()

    conn2 = init_db(db_path)
    assert len(conn2.execute("PRAGMA foreign_key_list(bids)").fetchall()) == 0
    conn2.close()
