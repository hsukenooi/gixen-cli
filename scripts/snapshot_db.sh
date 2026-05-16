#!/usr/bin/env bash
# Pre/post-migration snapshot for the FMV split. Prints counts and a small
# sample so before/after diffs are obvious. Read-only — safe to run anytime.
set -euo pipefail

DB_PATH="${1:-$HOME/.gixen-server/db.sqlite}"
if [[ ! -f "$DB_PATH" ]]; then
  echo "DB not found at $DB_PATH" >&2
  exit 1
fi

echo "=== Snapshot of $DB_PATH at $(date -u +%FT%TZ) ==="

echo
echo "-- Schema --"
sqlite3 "$DB_PATH" ".schema comics"
sqlite3 "$DB_PATH" ".schema fmv" 2>/dev/null || echo "(fmv: does not exist yet)"
sqlite3 "$DB_PATH" ".schema bid_fmvs" 2>/dev/null || echo "(bid_fmvs: does not exist yet)"
sqlite3 "$DB_PATH" "PRAGMA table_info(bids);" \
  | grep -E '^[0-9]+\|(fmv_id|comic_id|grade)\|'

echo
echo "-- Counts --"
sqlite3 "$DB_PATH" "SELECT 'comics rows', COUNT(*) FROM comics;"
sqlite3 "$DB_PATH" "SELECT 'distinct (title,issue,year)', COUNT(*) FROM (SELECT DISTINCT title, issue, year FROM comics);"
sqlite3 "$DB_PATH" "SELECT 'comics with fmv_low NOT NULL', COUNT(*) FROM comics WHERE fmv_low IS NOT NULL;" 2>/dev/null \
  || echo "(legacy fmv_low column gone — post-migration DB)"
sqlite3 "$DB_PATH" "SELECT 'fmv rows', COUNT(*) FROM fmv;" 2>/dev/null || echo "(fmv: 0 / missing)"
sqlite3 "$DB_PATH" "SELECT 'fmv rows with low NOT NULL', COUNT(*) FROM fmv WHERE low IS NOT NULL;" 2>/dev/null || true
sqlite3 "$DB_PATH" "SELECT 'bids rows', COUNT(*) FROM bids;"
sqlite3 "$DB_PATH" "SELECT 'bids with comic_id NOT NULL (legacy)', COUNT(*) FROM bids WHERE comic_id IS NOT NULL;" 2>/dev/null \
  || echo "(bids.comic_id: column gone — post-migration DB)"
sqlite3 "$DB_PATH" "SELECT 'bids with fmv_id NOT NULL', COUNT(*) FROM bids WHERE fmv_id IS NOT NULL;" 2>/dev/null \
  || echo "(bids.fmv_id: column missing — pre-migration DB)"
sqlite3 "$DB_PATH" "SELECT 'bid_comics rows (legacy)', COUNT(*) FROM bid_comics;" 2>/dev/null || true
sqlite3 "$DB_PATH" "SELECT 'bid_fmvs rows', COUNT(*) FROM bid_fmvs;" 2>/dev/null || echo "(bid_fmvs: missing)"

echo
echo "-- Suspected shadow comics (same title+issue, multiple rows) --"
sqlite3 "$DB_PATH" "
  SELECT title, issue, COUNT(*) AS n_rows
  FROM comics
  GROUP BY title, issue
  HAVING n_rows > 1
  ORDER BY n_rows DESC, title
  LIMIT 20;
"

echo
echo "-- Sample of bids with FMV linkage --"
sqlite3 "$DB_PATH" "
  SELECT b.item_id, b.fmv_id,
         f.grade, f.low, f.high,
         c.title, c.issue, c.year
  FROM bids b
  LEFT JOIN fmv   f ON f.id = b.fmv_id
  LEFT JOIN comics c ON c.id = f.comic_id
  WHERE b.status != 'PURGED'
  ORDER BY b.added_at DESC
  LIMIT 10;
" 2>/dev/null || sqlite3 "$DB_PATH" "
  SELECT b.item_id, b.comic_id, c.title, c.issue, c.year, c.grade
  FROM bids b
  LEFT JOIN comics c ON c.id = b.comic_id
  WHERE b.status != 'PURGED'
  ORDER BY b.added_at DESC
  LIMIT 10;
"
