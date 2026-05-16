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
    assert "bid_comics" in tables


def test_wal_mode_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_upsert_comic_inserts(db):
    comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="300",
                            year=1988, grade=9.2,
                            fmv_low=800.0, fmv_high=1000.0,
                            fmv_comps=12, fmv_confidence="high",
                            fmv_notes="Key issue")
    assert isinstance(comic_id, int)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["grade"] == 9.2
    assert row["fmv_confidence"] == "high"


def test_upsert_comic_updates_on_conflict(db):
    id1 = upsert_comic(db, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=500.0, fmv_high=700.0,
                       fmv_comps=5, fmv_confidence="medium", fmv_notes="")
    id2 = upsert_comic(db, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=550.0, fmv_high=750.0,
                       fmv_comps=8, fmv_confidence="high", fmv_notes="Updated")
    assert id1 == id2
    row = db.execute("SELECT fmv_low FROM comics WHERE id=?", (id1,)).fetchone()
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


def test_insert_bid_links_comic(db):
    comic_id = upsert_comic(db, "Hulk", "181", 1974, 9.0,
                            50.0, 70.0, 10, "high", "")
    bid_id = insert_bid(db, "987654321", 60.0, comic_id, 6, 0, "seller2")
    row = db.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
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


def test_upsert_comic_persists_locg_ids(db):
    """locg_id and locg_variant_id round-trip through upsert_comic."""
    comic_id = upsert_comic(
        db, title="Amazing Spider-Man", issue="300", year=1988, grade=9.2,
        fmv_low=800.0, fmv_high=1000.0,
        fmv_comps=12, fmv_confidence="high", fmv_notes="",
        locg_id=6977652, locg_variant_id=6977652,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] == 6977652
    assert row["locg_variant_id"] == 6977652


def test_upsert_comic_locg_ids_default_to_null(db):
    """Backwards compat: existing call sites without locg_id keep working."""
    comic_id = upsert_comic(
        db, title="Hulk", issue="181", year=1974, grade=9.0,
        fmv_low=50.0, fmv_high=70.0,
        fmv_comps=10, fmv_confidence="high", fmv_notes="",
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] is None
    assert row["locg_variant_id"] is None


def test_upsert_comic_locg_ids_preserved_on_conflict(db):
    """A second upsert without locg_id must not clobber the existing values."""
    id1 = upsert_comic(
        db, title="X-Men", issue="1", year=1963, grade=8.0,
        fmv_low=500.0, fmv_high=700.0,
        fmv_comps=5, fmv_confidence="medium", fmv_notes="",
        locg_id=12345, locg_variant_id=67890,
    )
    id2 = upsert_comic(
        db, title="X-Men", issue="1", year=1963, grade=8.0,
        fmv_low=550.0, fmv_high=750.0,
        fmv_comps=8, fmv_confidence="high", fmv_notes="Updated",
        # No locg_id passed — should preserve prior values
    )
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345
    assert row["locg_variant_id"] == 67890


def test_upsert_comic_locg_ids_updated_when_provided(db):
    """A second upsert with new locg_id values should overwrite the stored ones."""
    id1 = upsert_comic(
        db, title="Spawn", issue="1", year=1992, grade=9.8,
        fmv_low=100.0, fmv_high=150.0,
        fmv_comps=5, fmv_confidence="high", fmv_notes="",
        locg_id=100, locg_variant_id=None,
    )
    id2 = upsert_comic(
        db, title="Spawn", issue="1", year=1992, grade=9.8,
        fmv_low=110.0, fmv_high=160.0,
        fmv_comps=6, fmv_confidence="high", fmv_notes="",
        locg_id=200, locg_variant_id=300,
    )
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 200
    assert row["locg_variant_id"] == 300


# ---------------------------------------------------------------------------
# bid_comics junction table
# ---------------------------------------------------------------------------

def _make_lot(db, item_id="900000001", n=3, series="Daredevil: The Man Without Fear"):
    """Helper: insert a bid + N comics, return (bid_id, [comic_id, ...])."""
    bid_id = insert_bid(db, item_id, 100.0, None, 6, 0, "s")
    comic_ids = [
        upsert_comic(db, series, str(i), 1993, None,
                     None, None, None, None, None)
        for i in range(1, n + 1)
    ]
    return bid_id, comic_ids


def test_link_comic_to_bid_basic(db):
    bid_id, comic_ids = _make_lot(db, n=2)
    link_comic_to_bid(db, bid_id, comic_ids[0])
    rows = db.execute(
        "SELECT * FROM bid_comics WHERE bid_id=?", (bid_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["comic_id"] == comic_ids[0]
    assert rows[0]["is_primary"] == 0


def test_link_comic_to_bid_idempotent(db):
    bid_id, comic_ids = _make_lot(db, n=1)
    link_comic_to_bid(db, bid_id, comic_ids[0])
    link_comic_to_bid(db, bid_id, comic_ids[0])
    rows = db.execute(
        "SELECT COUNT(*) AS n FROM bid_comics WHERE bid_id=?", (bid_id,)
    ).fetchone()
    assert rows["n"] == 1


def test_link_comic_to_bid_primary_demotes_prior(db):
    bid_id, comic_ids = _make_lot(db, n=3)
    link_comic_to_bid(db, bid_id, comic_ids[0], is_primary=True)
    link_comic_to_bid(db, bid_id, comic_ids[1], is_primary=True)
    rows = db.execute(
        "SELECT comic_id, is_primary FROM bid_comics WHERE bid_id=? ORDER BY comic_id",
        (bid_id,),
    ).fetchall()
    by_comic = {r["comic_id"]: r["is_primary"] for r in rows}
    assert by_comic[comic_ids[0]] == 0
    assert by_comic[comic_ids[1]] == 1


def test_link_comic_to_bid_primary_mirrors_to_bids(db):
    bid_id, comic_ids = _make_lot(db, n=2)
    link_comic_to_bid(db, bid_id, comic_ids[0], is_primary=True)
    row = db.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["comic_id"] == comic_ids[0]


def test_link_comic_to_bid_promotes_existing_row(db):
    """Calling with is_primary=True on an already-linked non-primary row promotes it."""
    bid_id, comic_ids = _make_lot(db, n=2)
    link_comic_to_bid(db, bid_id, comic_ids[0])  # non-primary
    link_comic_to_bid(db, bid_id, comic_ids[0], is_primary=True)
    row = db.execute(
        "SELECT is_primary FROM bid_comics WHERE bid_id=? AND comic_id=?",
        (bid_id, comic_ids[0]),
    ).fetchone()
    assert row["is_primary"] == 1


def test_get_comics_for_bid_orders_primary_first(db):
    bid_id, comic_ids = _make_lot(db, n=3)
    # Link in reverse to verify primary-first ordering, not insertion order
    link_comic_to_bid(db, bid_id, comic_ids[2])
    link_comic_to_bid(db, bid_id, comic_ids[1])
    link_comic_to_bid(db, bid_id, comic_ids[0], is_primary=True)
    rows = get_comics_for_bid(db, bid_id)
    assert len(rows) == 3
    assert rows[0]["id"] == comic_ids[0]
    assert rows[0]["is_primary"] == 1
    # Remaining two ordered by issue number
    assert rows[1]["issue"] == "2"
    assert rows[2]["issue"] == "3"


def test_get_comics_for_bid_empty(db):
    bid_id = insert_bid(db, "900000099", 50.0, None, 6, 0, "s")
    assert get_comics_for_bid(db, bid_id) == []


def test_get_primary_comic_for_bid_returns_primary(db):
    bid_id, comic_ids = _make_lot(db, n=2)
    link_comic_to_bid(db, bid_id, comic_ids[0], is_primary=True)
    link_comic_to_bid(db, bid_id, comic_ids[1])
    row = get_primary_comic_for_bid(db, bid_id)
    assert row is not None
    assert row["id"] == comic_ids[0]


def test_get_primary_comic_for_bid_none_when_only_secondary(db):
    bid_id, comic_ids = _make_lot(db, n=1)
    link_comic_to_bid(db, bid_id, comic_ids[0])  # not primary
    assert get_primary_comic_for_bid(db, bid_id) is None


def test_migration_backfills_bid_comics_from_legacy_bids(tmp_path):
    """Pre-existing bids with bids.comic_id should populate bid_comics on init.

    Simulates the upgrade path: an old DB already has bids.comic_id values; the
    new code creates bid_comics + backfills via INSERT OR IGNORE.
    """
    db_path = tmp_path / "upgrade.db"
    # First init: create the schema (under the new code, that's everything).
    conn = init_db(db_path)
    comic_id = upsert_comic(conn, "Hulk", "181", 1974, 9.0,
                            50.0, 70.0, 10, "high", "")
    # Insert a bid that already has comic_id set, then wipe the junction
    # to simulate a DB created before bid_comics existed.
    bid_id = insert_bid(conn, "999000001", 60.0, comic_id, 6, 0, "s")
    conn.execute("DELETE FROM bid_comics")
    conn.commit()
    conn.close()

    # Second init: should re-run migrations, which backfill bid_comics.
    conn2 = init_db(db_path)
    rows = conn2.execute(
        "SELECT bid_id, comic_id, is_primary FROM bid_comics WHERE bid_id=?",
        (bid_id,),
    ).fetchall()
    conn2.close()
    assert len(rows) == 1
    assert rows[0]["comic_id"] == comic_id
    assert rows[0]["is_primary"] == 1


def test_migration_backfill_is_idempotent(tmp_path):
    """Running init_db a second time on a fresh DB doesn't duplicate junction rows."""
    db_path = tmp_path / "idem.db"
    conn = init_db(db_path)
    comic_id = upsert_comic(conn, "Hulk", "181", 1974, 9.0,
                            50.0, 70.0, 10, "high", "")
    bid_id = insert_bid(conn, "999000002", 60.0, comic_id, 6, 0, "s")
    # Drop+recreate junction wouldn't happen in real life, but the backfill
    # should still be safe to run after the row already exists.
    conn.close()

    # Re-open: backfill runs again. Existing row should not be duplicated.
    conn2 = init_db(db_path)
    rows = conn2.execute(
        "SELECT COUNT(*) AS n FROM bid_comics WHERE bid_id=?", (bid_id,)
    ).fetchone()
    conn2.close()
    assert rows["n"] == 1


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
    cid = upsert_comic(db, title="Hulk", issue="181", year=1974,
                       grade=None, fmv_low=None, fmv_high=None,
                       fmv_comps=None, fmv_confidence=None, fmv_notes=None)
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
    cid = upsert_comic(db, "ASM", "300", 1988, None,
                       None, None, None, None, None)
    fid = upsert_fmv(db, cid, 9.2, low=None, high=None, comps=None,
                     confidence=None, notes=None)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["grade"] == 9.2
    assert row["low"] is None
    assert row["high"] is None
    assert row["updated_at"] is None


def test_upsert_fmv_idempotent_on_conflict(db):
    cid = upsert_comic(db, "X-Men", "1", 1963, None,
                       None, None, None, None, None)
    f1 = upsert_fmv(db, cid, 8.0, low=500.0, high=700.0, comps=5,
                    confidence="medium", notes="")
    f2 = upsert_fmv(db, cid, 8.0, low=550.0, high=750.0, comps=8,
                    confidence="high", notes="Updated")
    assert f1 == f2
    row = db.execute("SELECT low, confidence FROM fmv WHERE id=?", (f1,)).fetchone()
    assert row["low"] == 550.0
    assert row["confidence"] == "high"


def test_upsert_fmv_preserves_on_partial_update(db):
    cid = upsert_comic(db, "Spawn", "1", 1992, None,
                       None, None, None, None, None)
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
    cid = upsert_comic(db, "ASM", "300", 1988, None,
                       None, None, None, None, None)
    f1 = upsert_fmv(db, cid, 9.2, 800.0, 1000.0, 12, "high", "")
    f2 = upsert_fmv(db, cid, 7.0, 200.0, 300.0, 8, "high", "")
    assert f1 != f2
    rows = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
        (cid,),
    ).fetchall()
    assert [(r["grade"], r["low"]) for r in rows] == [(7.0, 200.0), (9.2, 800.0)]


def test_set_bid_fmv_sets_value(db):
    cid = upsert_comic(db, "Hulk", "181", 1974, None,
                       None, None, None, None, None)
    fid = upsert_fmv(db, cid, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "111111", 50.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_set_bid_fmv_accepts_none(db):
    cid = upsert_comic(db, "Hulk", "181", 1974, None,
                       None, None, None, None, None)
    fid = upsert_fmv(db, cid, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "111112", 50.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    set_bid_fmv(db, bid_id, None)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] is None


def test_get_fmv_for_bid_returns_joined_row(db):
    cid = upsert_comic(db, "ASM", "300", 1988, None,
                       None, None, None, None, None)
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
    cid = upsert_comic(db, "ASM", "300", 1988, None,
                       None, None, None, None, None)
    fid = upsert_fmv(db, cid, 9.2, None, None, None, None, None)
    bid_id = insert_bid(db, "111115", 600.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, fid)
    rows = db.execute("SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == fid
    assert rows[0]["is_primary"] == 0


def test_link_fmv_to_bid_primary_mirrors_to_bids_fmv_id(db):
    cid = upsert_comic(db, "ASM", "300", 1988, None,
                       None, None, None, None, None)
    fid = upsert_fmv(db, cid, 9.2, None, None, None, None, None)
    bid_id = insert_bid(db, "111116", 600.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_link_fmv_to_bid_primary_demotes_prior(db):
    cid = upsert_comic(db, "Daredevil", "1", 1993, None,
                       None, None, None, None, None)
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
