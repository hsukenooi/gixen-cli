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
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_init_creates_tables(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}
    assert "comics" in tables
    assert "bids" in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_wal_mode_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_upsert_comic_inserts(db):
    comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="300", year=1988)
    assert isinstance(comic_id, int)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["issue"] == "300"
    assert row["year"] == 1988


def test_upsert_comic_idempotent_on_identity(db):
    id1 = upsert_comic(db, title="X-Men", issue="1", year=1963)
    id2 = upsert_comic(db, title="X-Men", issue="1", year=1963)
    assert id1 == id2


def test_upsert_comic_different_year_is_distinct_row(db):
    a = upsert_comic(db, "Hulk", "181", 1974)
    b = upsert_comic(db, "Hulk", "181", 1975)
    assert a != b


def test_insert_bid(db):
    bid_id = insert_bid(db, item_id="123456789", max_bid=800.0,
                        fmv_id=None, bid_offset=6, snipe_group=0,
                        seller="seller1")
    assert isinstance(bid_id, int)
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["item_id"] == "123456789"
    assert row["status"] == "PENDING"
    assert row["max_bid"] == 800.0


def test_insert_bid_links_via_fmv_id(db):
    comic_id = upsert_comic(db, "Hulk", "181", 1974)
    fmv_id = upsert_fmv(db, comic_id, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "987654321", 60.0, fmv_id, 6, 0, "seller2")
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fmv_id


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


def test_upsert_comic_persists_locg_ids(db):
    comic_id = upsert_comic(
        db, title="Amazing Spider-Man", issue="300", year=1988,
        locg_id=6977652, locg_variant_id=6977652,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] == 6977652
    assert row["locg_variant_id"] == 6977652


def test_upsert_comic_locg_ids_default_to_null(db):
    comic_id = upsert_comic(db, title="Hulk", issue="181", year=1974)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] is None
    assert row["locg_variant_id"] is None


def test_upsert_comic_locg_ids_preserved_on_conflict(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, locg_id=12345, locg_variant_id=67890)
    id2 = upsert_comic(db, "X-Men", "1", 1963)
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345
    assert row["locg_variant_id"] == 67890


def test_upsert_comic_locg_ids_updated_when_provided(db):
    id1 = upsert_comic(db, "Spawn", "1", 1992, locg_id=100)
    id2 = upsert_comic(db, "Spawn", "1", 1992, locg_id=200, locg_variant_id=300)
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 200
    assert row["locg_variant_id"] == 300


# ---------------------------------------------------------------------------
# bid_comics junction table
# ---------------------------------------------------------------------------

def _make_lot(db, item_id="900000001", n=3, series="Daredevil: The Man Without Fear"):
    """Insert a bid + N comics (each with one fmv row at grade 9.0).
    Returns (bid_id, [fmv_id, ...])."""
    fmv_ids = []
    for i in range(1, n + 1):
        cid = upsert_comic(db, series, str(i), 1993)
        fmv_ids.append(upsert_fmv(db, cid, 9.0, None, None, None, None, None))
    bid_id = insert_bid(db, item_id, 100.0, fmv_ids[0], 6, 0, "s")
    return bid_id, fmv_ids


def test_link_fmv_to_bid_creates_junction(db):
    bid_id, fmv_ids = _make_lot(db, n=2)
    link_fmv_to_bid(db, bid_id, fmv_ids[1])
    rows = db.execute(
        "SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchall()
    fmv_ids_in_junction = {r["fmv_id"] for r in rows}
    assert fmv_ids[1] in fmv_ids_in_junction


def test_link_fmv_to_bid_idempotent_secondary(db):
    bid_id, fmv_ids = _make_lot(db, n=1)
    link_fmv_to_bid(db, bid_id, fmv_ids[0])
    link_fmv_to_bid(db, bid_id, fmv_ids[0])
    n = db.execute(
        "SELECT COUNT(*) AS n FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchone()["n"]
    assert n == 1


def test_post_migration_bid_fmvs_mirrors_primary_linkage(tmp_path):
    """After the FMV-split migration, every bid with fmv_id NOT NULL has a
    matching primary row in bid_fmvs. Mirrors the pre-FMV-split test that
    bid_comics carried the primary pointer."""
    path = tmp_path / "post.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.execute(
        "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        bid = new.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
        assert bid["fmv_id"] is not None
        junc = new.execute(
            "SELECT is_primary FROM bid_fmvs WHERE bid_id=1 AND fmv_id=?",
            (bid["fmv_id"],),
        ).fetchone()
        assert junc is not None
        assert junc["is_primary"] == 1
    finally:
        new.close()


def test_fmv_table_exists(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_fmv_has_expected_columns(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(fmv)")}
    assert cols == {
        "id", "comic_id", "grade", "low", "high", "comps",
        "confidence", "notes", "updated_at",
    }


def test_fmv_unique_on_comic_and_grade(db):
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='fmv'"
    ).fetchone()["sql"]
    normalized = sql.replace(" ", "")
    assert "UNIQUE(comic_id,grade)" in normalized


def test_bid_fmvs_has_expected_columns(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(bid_fmvs)")}
    assert cols == {"bid_id", "fmv_id", "is_primary"}


def test_bids_fmv_id_column_exists(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(bids)")}
    assert "fmv_id" in cols


from server.db import (
    upsert_fmv, set_bid_fmv, get_fmv_for_bid, link_fmv_to_bid,
)


def test_upsert_fmv_inserts_with_values(db):
    cid = upsert_comic(db, title="Hulk", issue="181", year=1974)
    fid = upsert_fmv(db, comic_id=cid, grade=9.2,
                     low=4000.0, high=5500.0, comps=12,
                     confidence="high", notes="GPA Jan 2026")
    assert isinstance(fid, int)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 4000.0
    assert row["high"] == 5500.0
    assert row["confidence"] == "high"
    assert row["updated_at"] is not None


def test_upsert_fmv_inserts_null_valuation(db):
    """Grade-only row, no FMV researched yet. updated_at stays NULL because
    no actual valuation was supplied."""
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, low=None, high=None, comps=None,
                     confidence=None, notes=None)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["grade"] == 9.2
    assert row["low"] is None
    assert row["high"] is None
    assert row["updated_at"] is None


def test_upsert_fmv_idempotent_on_conflict(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    f1 = upsert_fmv(db, cid, 8.0, low=500.0, high=700.0, comps=5,
                    confidence="medium", notes="")
    f2 = upsert_fmv(db, cid, 8.0, low=550.0, high=750.0, comps=8,
                    confidence="high", notes="Updated")
    assert f1 == f2
    row = db.execute("SELECT low, confidence FROM fmv WHERE id=?", (f1,)).fetchone()
    assert row["low"] == 550.0
    assert row["confidence"] == "high"


def test_upsert_fmv_preserves_on_partial_update(db):
    cid = upsert_comic(db, "Spawn", "1", 1992)
    fid = upsert_fmv(db, cid, 9.8, 100.0, 150.0, 5, "high", "first pass")
    upsert_fmv(db, cid, 9.8, low=120.0, high=None, comps=None,
               confidence=None, notes=None)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 120.0
    assert row["high"] == 150.0
    assert row["comps"] == 5
    assert row["confidence"] == "high"
    assert row["notes"] == "first pass"


def test_upsert_fmv_different_grades_coexist(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    f1 = upsert_fmv(db, cid, 9.2, 800.0, 1000.0, 12, "high", "")
    f2 = upsert_fmv(db, cid, 7.0, 200.0, 300.0, 8, "high", "")
    assert f1 != f2
    rows = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
        (cid,),
    ).fetchall()
    assert [(r["grade"], r["low"]) for r in rows] == [(7.0, 200.0), (9.2, 800.0)]


def test_set_bid_fmv_sets_value(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    fid = upsert_fmv(db, cid, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "111111", 50.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_set_bid_fmv_accepts_none(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    fid = upsert_fmv(db, cid, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "111112", 50.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    set_bid_fmv(db, bid_id, None)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] is None


def test_get_fmv_for_bid_returns_joined_row(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, 800.0, 1000.0, 12, "high", "")
    bid_id = insert_bid(db, "111113", 600.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    fmv = get_fmv_for_bid(db, bid_id)
    assert fmv is not None
    assert fmv["low"] == 800.0
    assert fmv["grade"] == 9.2
    assert fmv["comic_id"] == cid


def test_get_fmv_for_bid_returns_none_when_unlinked(db):
    bid_id = insert_bid(db, "111114", 600.0, None, 6, 0, "s")
    assert get_fmv_for_bid(db, bid_id) is None


def test_link_fmv_to_bid_basic(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, None, None, None, None, None)
    bid_id = insert_bid(db, "111115", 600.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, fid)
    rows = db.execute("SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == fid
    assert rows[0]["is_primary"] == 0


def test_link_fmv_to_bid_primary_mirrors_to_bids_fmv_id(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, None, None, None, None, None)
    bid_id = insert_bid(db, "111116", 600.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_link_fmv_to_bid_primary_demotes_prior(db):
    cid = upsert_comic(db, "Daredevil", "1", 1993)
    f1 = upsert_fmv(db, cid, 9.0, None, None, None, None, None)
    f2 = upsert_fmv(db, cid, 7.0, None, None, None, None, None)
    bid_id = insert_bid(db, "111117", 100.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, f1, is_primary=True)
    link_fmv_to_bid(db, bid_id, f2, is_primary=True)
    by_fmv = {
        r["fmv_id"]: r["is_primary"] for r in db.execute(
            "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,)
        )
    }
    assert by_fmv[f1] == 0
    assert by_fmv[f2] == 1


def test_fk_invariant_fmv_id_must_exist(db):
    """Trying to set bids.fmv_id to a non-existent fmv.id fails the FK."""
    bid_id = insert_bid(db, "111118", 100.0, None, 6, 0, "s")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE bids SET fmv_id=999999 WHERE id=?", (bid_id,))
        db.commit()


def test_fk_invariant_bid_fmvs_fmv_id_must_exist(db):
    """Junction can't point at a non-existent fmv either."""
    bid_id = insert_bid(db, "111119", 100.0, None, 6, 0, "s")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, 999999, 0)",
            (bid_id,),
        )
        db.commit()


import sqlite3 as _sqlite3  # alias to avoid clash with sqlite3 used in fixtures


def _build_legacy_db(path):
    """Construct a DB at the pre-split schema, bypassing init_db's new code.
    Mirrors the schema in production as of commit 941201f."""
    conn = _sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = _sqlite3.Row
    conn.executescript("""
    CREATE TABLE comics (
        id              INTEGER PRIMARY KEY,
        title           TEXT NOT NULL,
        issue           TEXT NOT NULL,
        year            INTEGER NOT NULL,
        grade           REAL,
        fmv_low         REAL,
        fmv_high        REAL,
        fmv_comps       INTEGER,
        fmv_confidence  TEXT,
        fmv_notes       TEXT,
        fmv_updated_at  TEXT,
        locg_id         INTEGER,
        locg_variant_id INTEGER,
        created_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(title, issue, year, grade)
    );
    CREATE TABLE bids (
        id              INTEGER PRIMARY KEY,
        item_id         TEXT NOT NULL,
        comic_id        INTEGER REFERENCES comics(id),
        max_bid         REAL NOT NULL,
        bid_offset      INTEGER DEFAULT 6,
        snipe_group     INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'PENDING',
        winning_bid     REAL,
        seller          TEXT,
        auction_end_at      TEXT,
        local_snipe_at      TEXT,
        local_snipe_result  TEXT,
        notes               TEXT,
        added_at            TEXT DEFAULT (datetime('now')),
        resolved_at         TEXT,
        ebay_title          TEXT,
        status_mirror       TEXT,
        cached_current_bid  TEXT,
        cached_at           TEXT
    );
    CREATE TABLE bid_comics (
        bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
        comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
        is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bid_id, comic_id)
    );
    """)
    conn.commit()
    return conn


def test_migration_collapses_shadow_rows_into_single_comic(tmp_path):
    """Same (title, issue, year), three grades, only the first has FMV.
    After migration: one comics row, three fmv rows (one per grade), bids
    repointed to the matching fmv.id."""
    path = tmp_path / "legacy.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, "
        "fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at, locg_id) "
        "VALUES (42, 'Spider-Man', '300', 1988, 9.0, 800, 1000, 12, 'high', "
        "'orig', '2026-05-01T00:00:00', 99999)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (58, 'Spider-Man', '300', 1988, 9.2)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (60, 'Spider-Man', '300', 1988, 8.0)"
    )
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 42, 700)")
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (2, '222', 58, 900)")
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (3, '333', 60, 400)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 42, 1)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (2, 58, 1)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (3, 60, 1)")
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        rows = new.execute(
            "SELECT id, locg_id FROM comics WHERE title='Spider-Man' AND issue='300' AND year=1988"
        ).fetchall()
        assert len(rows) == 1
        survivor_id = rows[0]["id"]
        assert rows[0]["locg_id"] == 99999

        fmv_rows = new.execute(
            "SELECT id, grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
            (survivor_id,),
        ).fetchall()
        assert [r["grade"] for r in fmv_rows] == [8.0, 9.0, 9.2]
        by_grade = {r["grade"]: r["low"] for r in fmv_rows}
        assert by_grade[9.0] == 800.0
        assert by_grade[9.2] is None
        assert by_grade[8.0] is None

        fmv_by_grade = {r["grade"]: r["id"] for r in fmv_rows}
        bid_rows = new.execute(
            "SELECT item_id, fmv_id FROM bids ORDER BY item_id"
        ).fetchall()
        bid_fmv_by_item = {r["item_id"]: r["fmv_id"] for r in bid_rows}
        assert bid_fmv_by_item["111"] == fmv_by_grade[9.0]
        assert bid_fmv_by_item["222"] == fmv_by_grade[9.2]
        assert bid_fmv_by_item["333"] == fmv_by_grade[8.0]

        junc_rows = new.execute(
            "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs ORDER BY bid_id"
        ).fetchall()
        assert {(r["bid_id"], r["fmv_id"]) for r in junc_rows} == {
            (1, fmv_by_grade[9.0]),
            (2, fmv_by_grade[9.2]),
            (3, fmv_by_grade[8.0]),
        }
        assert all(r["is_primary"] == 1 for r in junc_rows)
    finally:
        new.close()


def test_migration_is_idempotent(tmp_path):
    """Running init_db on an already-migrated DB is a no-op."""
    path = tmp_path / "idem.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.commit()
    conn.close()

    conn1 = init_db(path)
    cc1 = conn1.execute("SELECT COUNT(*) AS n FROM comics").fetchone()["n"]
    fc1 = conn1.execute("SELECT COUNT(*) AS n FROM fmv").fetchone()["n"]
    bf1 = conn1.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()["fmv_id"]
    conn1.close()

    conn2 = init_db(path)
    cc2 = conn2.execute("SELECT COUNT(*) AS n FROM comics").fetchone()["n"]
    fc2 = conn2.execute("SELECT COUNT(*) AS n FROM fmv").fetchone()["n"]
    bf2 = conn2.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()["fmv_id"]
    conn2.close()

    assert cc1 == cc2 == 1
    assert fc1 == fc2 == 1
    assert bf1 == bf2 is not None


def test_migration_handles_null_grade_bid(tmp_path):
    """Bid with comic_id but no grade on the linked comic → fmv_id stays NULL,
    bid stays in the table (it's still a real auction we're tracking)."""
    path = tmp_path / "null.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (1, 'Hulk', '181', 1974, NULL)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.execute(
        "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        bid = new.execute(
            "SELECT id, fmv_id FROM bids WHERE id=1"
        ).fetchone()
        assert bid is not None
        assert bid["fmv_id"] is None
        fmv_count = new.execute("SELECT COUNT(*) AS n FROM fmv").fetchone()["n"]
        assert fmv_count == 0
        junc_count = new.execute("SELECT COUNT(*) AS n FROM bid_fmvs").fetchone()["n"]
        assert junc_count == 0
    finally:
        new.close()


def test_migration_preserves_orphan_fmv(tmp_path):
    """A legacy comics row with FMV but no bids still gets an fmv row so the
    valuation isn't lost."""
    path = tmp_path / "orphan.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'X-Men', '1', 1963, 8.0, 5000)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        fmv = new.execute("SELECT low FROM fmv WHERE grade=8.0").fetchone()
        assert fmv is not None
        assert fmv["low"] == 5000.0
    finally:
        new.close()


def test_migration_survivor_prefers_locg_then_fmv(tmp_path):
    path = tmp_path / "survivor.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, locg_id) "
        "VALUES (10, 'ASM', '300', 1988, 9.4, 11111)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (20, 'ASM', '300', 1988, 9.2, 800)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        survivor = new.execute(
            "SELECT id, locg_id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
        ).fetchone()
        assert survivor["locg_id"] == 11111
        grades = {r["grade"] for r in new.execute(
            "SELECT grade FROM fmv WHERE comic_id=?", (survivor["id"],)
        )}
        assert grades == {9.2, 9.4}
        fmv92 = new.execute(
            "SELECT low FROM fmv WHERE comic_id=? AND grade=9.2",
            (survivor["id"],),
        ).fetchone()
        assert fmv92["low"] == 800
    finally:
        new.close()


def test_migration_lot_with_grade_creates_one_bid_fmvs_per_comic(tmp_path):
    """bid_comics row over a 3-issue lot, bid grade=6.0 → 3 bid_fmvs rows,
    each pointing to an fmv at grade 6.0 for the respective comic."""
    path = tmp_path / "lot.db"
    conn = _build_legacy_db(path)
    for i, cid in enumerate((101, 102, 103), start=1):
        conn.execute(
            "INSERT INTO comics (id, title, issue, year, grade) "
            "VALUES (?, 'Daredevil', ?, 1993, 6.0)",
            (cid, str(i)),
        )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 101, 100)"
    )
    for cid in (101, 102, 103):
        conn.execute(
            "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, ?, ?)",
            (cid, 1 if cid == 101 else 0),
        )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        comic_ids = {
            r["id"] for r in new.execute(
                "SELECT id FROM comics WHERE title='Daredevil'"
            )
        }
        assert len(comic_ids) == 3
        fmv_rows = new.execute("SELECT id, grade FROM fmv").fetchall()
        assert {r["grade"] for r in fmv_rows} == {6.0}
        assert len(fmv_rows) == 3
        junc = new.execute(
            "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs WHERE bid_id=1"
        ).fetchall()
        assert len(junc) == 3
        primary = [r for r in junc if r["is_primary"] == 1]
        assert len(primary) == 1
        primary_fmv_id = primary[0]["fmv_id"]
        bid_fmv = new.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()["fmv_id"]
        assert bid_fmv == primary_fmv_id
    finally:
        new.close()


def test_migration_lot_with_mixed_per_comic_grades_preserves_each(tmp_path):
    """A lot where the primary comic was graded 7.0 and the junction comics
    have legacy grades 7.0, 8.5, and NULL. Post-migration each bid_fmvs row
    points at the fmv row matching its OWN legacy grade; NULL falls back to
    the primary's 7.0."""
    path = tmp_path / "mixed_lot.db"
    conn = _build_legacy_db(path)
    # Primary comic (grade 7.0).
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (1, 'X-Men', '1', 1991, 7.0)"
    )
    # Junction comic with its own legacy grade 8.5.
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (2, 'X-Men', '2', 1991, 8.5)"
    )
    # Junction comic with NULL legacy grade.
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (3, 'X-Men', '3', 1991, NULL)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 100)"
    )
    for cid, prim in ((1, 1), (2, 0), (3, 0)):
        conn.execute(
            "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, ?, ?)",
            (cid, prim),
        )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        # Three bid_fmvs rows: one per junction entry.
        junc = new.execute(
            """
            SELECT bf.is_primary, f.grade, c.issue
            FROM bid_fmvs bf
            JOIN fmv f ON f.id = bf.fmv_id
            JOIN comics c ON c.id = f.comic_id
            WHERE bf.bid_id = 1
            ORDER BY c.issue
            """
        ).fetchall()
        by_issue = {r["issue"]: r for r in junc}
        # Issue 1 (primary): grade=7.0
        assert by_issue["1"]["grade"] == 7.0
        # Issue 2 (junction grade=8.5): grade=8.5 (NOT primary's 7.0)
        assert by_issue["2"]["grade"] == 8.5
        # Issue 3 (junction grade=NULL): falls back to primary's 7.0
        assert by_issue["3"]["grade"] == 7.0
    finally:
        new.close()


def test_migration_recovers_2026_05_13_incident(tmp_path):
    """14 bids spread across 4 shadow comics rows after grade revisions.
    Post-migration: one comics identity, 4 fmv rows, all 14 bids point at the
    fmv row matching their original grade, and the original FMV at 9.0 is intact."""
    path = tmp_path / "incident.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, "
        "fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at, locg_id) "
        "VALUES (1, 'Spider-Man', '300', 1988, 9.0, 800, 1000, 12, 'high', "
        "'GPA Jan 2026', '2026-05-01T00:00:00', 99999)"
    )
    for shadow_id, grade in [(2, 9.2), (3, 8.0), (4, 9.4)]:
        conn.execute(
            "INSERT INTO comics (id, title, issue, year, grade) VALUES (?, 'Spider-Man', '300', 1988, ?)",
            (shadow_id, grade),
        )
    for i, comic_id in enumerate([1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 4]):
        conn.execute(
            "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (?, ?, ?, 600)",
            (100 + i, f"99{i:07d}", comic_id),
        )
        conn.execute(
            "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (?, ?, 1)",
            (100 + i, comic_id),
        )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        survivor = new.execute(
            "SELECT id FROM comics WHERE title='Spider-Man' AND issue='300' AND year=1988"
        ).fetchone()
        assert survivor is not None

        fmvs = {r["grade"]: r["low"] for r in new.execute(
            "SELECT grade, low FROM fmv WHERE comic_id=?", (survivor["id"],)
        )}
        assert fmvs == {9.0: 800, 9.2: None, 8.0: None, 9.4: None}

        fmv_id_by_grade = {r["grade"]: r["id"] for r in new.execute(
            "SELECT id, grade FROM fmv WHERE comic_id=?", (survivor["id"],)
        )}
        all_bids = new.execute("SELECT item_id, fmv_id FROM bids").fetchall()
        assert all(b["fmv_id"] is not None for b in all_bids)
        item_to_grade = {f"99{i:07d}": g for i, g in enumerate(
            [9.0, 9.0, 9.0, 9.0, 9.2, 9.2, 9.2, 8.0, 8.0, 8.0, 9.4, 9.4, 9.4, 9.4]
        )}
        for b in all_bids:
            expected_fmv = fmv_id_by_grade[item_to_grade[b["item_id"]]]
            assert b["fmv_id"] == expected_fmv

        bid_at_9 = next(b for b in all_bids if item_to_grade[b["item_id"]] == 9.0)
        bid_id = new.execute(
            "SELECT id FROM bids WHERE item_id=?", (bid_at_9["item_id"],)
        ).fetchone()["id"]
        recovered = get_fmv_for_bid(new, bid_id)
        assert recovered["low"] == 800
    finally:
        new.close()


def test_migration_partial_failure_recovers(tmp_path, monkeypatch):
    """Inject a failure inside the rebuild block. Migration must propagate the
    exception, restore foreign_keys=ON, and leave the legacy tables intact so
    the migration can be retried (no data loss)."""
    path = tmp_path / "partial.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.commit()
    conn.close()

    # Patch the rebuild helper to raise. The migration must rollback cleanly
    # so legacy `comics.grade` and `bids.comic_id` are still present.
    import server.db as sdb
    real_rebuild = sdb._rebuild_tables

    def boom(*args, **kwargs):
        raise RuntimeError("simulated mid-rebuild failure")

    monkeypatch.setattr(sdb, "_rebuild_tables", boom)
    with pytest.raises(RuntimeError, match="simulated mid-rebuild failure"):
        init_db(path)

    # Connect raw to inspect state. (PRAGMA foreign_keys is per-connection in
    # SQLite, so we can't observe whether the migration's conn restored it
    # via a fresh connection here — instead we check that re-running the
    # migration converges, which proves recovery indirectly.)
    raw = _sqlite3.connect(str(path))
    raw.row_factory = _sqlite3.Row
    try:
        cols = {row[1] for row in raw.execute("PRAGMA table_info(comics)")}
        assert "grade" in cols, "legacy column should still exist (rebuild rolled back)"
        bids_cols = {row[1] for row in raw.execute("PRAGMA table_info(bids)")}
        assert "comic_id" in bids_cols, "legacy bids.comic_id should still exist"
        # comic 1 (the only one) is the survivor — it must still be reachable
        # so re-running the migration converges.
        row = raw.execute("SELECT title FROM comics WHERE id=1").fetchone()
        assert row is not None
        assert row["title"] == "Hulk"
    finally:
        raw.close()

    # Un-patch and retry. With the real rebuild, init_db should succeed.
    monkeypatch.setattr(sdb, "_rebuild_tables", real_rebuild)
    new = init_db(path)
    try:
        row = new.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
        assert row["fmv_id"] is not None
    finally:
        new.close()


def _build_legacy_db_no_unique(path):
    """Like _build_legacy_db but without the UNIQUE(title,issue,year,grade)
    constraint. Used to simulate legacy DBs that pre-date the constraint, where
    two shadow rows at the same grade can coexist (the merge-on-conflict
    branch's reason for existing)."""
    conn = _sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = _sqlite3.Row
    conn.executescript("""
    CREATE TABLE comics (
        id              INTEGER PRIMARY KEY,
        title           TEXT NOT NULL,
        issue           TEXT NOT NULL,
        year            INTEGER NOT NULL,
        grade           REAL,
        fmv_low         REAL,
        fmv_high        REAL,
        fmv_comps       INTEGER,
        fmv_confidence  TEXT,
        fmv_notes       TEXT,
        fmv_updated_at  TEXT,
        locg_id         INTEGER,
        locg_variant_id INTEGER,
        created_at      TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE bids (
        id              INTEGER PRIMARY KEY,
        item_id         TEXT NOT NULL,
        comic_id        INTEGER REFERENCES comics(id),
        max_bid         REAL NOT NULL,
        bid_offset      INTEGER DEFAULT 6,
        snipe_group     INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'PENDING',
        winning_bid     REAL,
        seller          TEXT,
        auction_end_at      TEXT,
        local_snipe_at      TEXT,
        local_snipe_result  TEXT,
        notes               TEXT,
        added_at            TEXT DEFAULT (datetime('now')),
        resolved_at         TEXT,
        ebay_title          TEXT,
        status_mirror       TEXT,
        cached_current_bid  TEXT,
        cached_at           TEXT
    );
    CREATE TABLE bid_comics (
        bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
        comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
        is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bid_id, comic_id)
    );
    """)
    conn.commit()
    return conn


def test_migration_tied_fmv_low_tiebreaks_by_updated_at(tmp_path):
    """Two legacy shadow rows at the same grade with non-NULL fmv_low.
    Tiebreak: the row with the most recent fmv_updated_at wins. Loser's notes
    are preserved with a 'merged from legacy comic_id=X' prefix.

    Uses a no-UNIQUE legacy schema to simulate the case the merge branch was
    designed for (pre-UNIQUE legacy DBs)."""
    path = tmp_path / "tied.db"
    conn = _build_legacy_db_no_unique(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, "
        "fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at, locg_id) "
        "VALUES (1, 'ASM', '300', 1988, 9.0, 800, 900, 5, 'high', 'older notes', "
        "'2026-04-01T00:00:00', 11111)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, "
        "fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at) "
        "VALUES (2, 'ASM', '300', 1988, 9.0, 850, 1000, 12, 'high', 'newer notes', "
        "'2026-05-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        fmv = new.execute(
            "SELECT low, high, notes, updated_at FROM fmv WHERE grade=9.0"
        ).fetchone()
        assert fmv is not None
        # Newer row wins (850 vs 800).
        assert fmv["low"] == 850
        # Loser's notes preserved with merge prefix.
        assert "merged from legacy comic_id=" in (fmv["notes"] or "")
    finally:
        new.close()


def test_migration_dangling_bids_comic_id_does_not_crash(tmp_path):
    """A bid pointing at a comic_id that doesn't exist must not abort the
    migration. We pre-clean dangling refs and continue with .get() fallbacks.

    Simulates the production reality where legacy bids may have been inserted
    with FK enforcement off (the legacy bids.comic_id had no FK protection
    until WAL mode + foreign_keys=ON was added)."""
    path = tmp_path / "dangling.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    # Bid 2 references comic 99 which doesn't exist (dangling) — insert with
    # FK enforcement off to simulate the legacy data condition. PRAGMA must
    # run outside any open transaction so commit first.
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (2, '222', 99, 30)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        # Migration completed — both bids survive, the dangling one has fmv_id NULL.
        rows = new.execute("SELECT item_id, fmv_id FROM bids ORDER BY item_id").fetchall()
        assert len(rows) == 2
        by_item = {r["item_id"]: r["fmv_id"] for r in rows}
        assert by_item["111"] is not None
        assert by_item["222"] is None
    finally:
        new.close()


def test_migration_fresh_db_uses_post_split_schema_directly(tmp_path):
    """A brand-new DB skips the rebuild entirely. The post-split schema is the
    source of truth in `_SCHEMA`; no legacy columns are ever created."""
    path = tmp_path / "fresh.db"
    conn = init_db(path)
    try:
        # Fresh DB has no legacy columns at all.
        comic_cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
        assert "grade" not in comic_cols
        assert "fmv_low" not in comic_cols
        bid_cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
        assert "comic_id" not in bid_cols
        # No bid_comics table on a fresh DB.
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "bid_comics" not in tables
    finally:
        conn.close()


def test_migration_creates_backup_at_expected_path(tmp_path):
    """When the migration runs (legacy DB), an automatic .pre-fmv-split.bak
    file is created next to the live DB before the rebuild."""
    path = tmp_path / "needs_backup.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        bak = path.with_suffix(path.suffix + ".pre-fmv-split.bak")
        assert bak.exists(), f"expected backup at {bak}"
        # Backup is a real file with bytes (the legacy DB contents).
        assert bak.stat().st_size > 0
    finally:
        new.close()


def test_migration_post_state_drops_legacy_columns(tmp_path):
    """After migration: comics.grade, comics.fmv_*, bids.comic_id are gone."""
    path = tmp_path / "post.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        comic_cols = {row[1] for row in new.execute("PRAGMA table_info(comics)")}
        bid_cols = {row[1] for row in new.execute("PRAGMA table_info(bids)")}
        assert "grade" not in comic_cols
        assert "fmv_low" not in comic_cols
        assert "fmv_high" not in comic_cols
        assert "fmv_comps" not in comic_cols
        assert "fmv_confidence" not in comic_cols
        assert "fmv_notes" not in comic_cols
        assert "fmv_updated_at" not in comic_cols
        assert "comic_id" not in bid_cols
        assert "grade" not in bid_cols
        assert "fmv_id" in bid_cols
    finally:
        new.close()
