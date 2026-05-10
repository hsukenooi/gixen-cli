"""FMV orchestrator — wires DB cache, ebay-cli, math, and DB upsert together.

Lives separately from cli.py so it can be tested without invoking Click.
The CLI command in cli.py is a thin wrapper around fmv_runner.run().
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import requests

import fmv_math


# ─── Public entry point ──────────────────────────────────────────────────────

def run(*, batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool, ebay_cli_path: str,
        quiet: bool, server_url: str | None) -> None:
    """Driver for `gixen-cli fmv`. Exits with sys.exit on hard failures."""
    if not server_url:
        click.echo("Error: GIXEN_SERVER_URL must be set. The fmv command "
                   "needs the server for cache reuse and DB upsert.", err=True)
        sys.exit(1)

    if not batch_path:
        click.echo("Error: --batch is required (path or '-' for stdin).", err=True)
        sys.exit(2)

    books = _read_batch(batch_path)
    if not books:
        click.echo("Empty batch.", err=True)
        sys.exit(0)

    # 1. DB cache reuse (skipped if --force)
    cached, needs_compute = _split_by_db_cache(
        books, server_url=server_url, max_age_days=max_age_days, force=force,
    )

    # 2. Fetch comps for the books that need fresh compute
    fresh_results: list[dict] = []
    if needs_compute:
        fresh_results = _fetch_comps(needs_compute, ebay_cli_path, force=force)

    # 3. Run FMV math + DB upsert for fresh books, keyed by original input idx
    needs_indices = [b["_idx"] for b in needs_compute]
    fresh_fmvs: dict[int, dict] = {}
    for ordinal, result in enumerate(fresh_results):
        if ordinal >= len(needs_indices):
            break
        idx = needs_indices[ordinal]
        fresh_fmvs[idx] = _compute_and_upsert_one(
            result, books[idx], server_url=server_url,
        )

    # 4. Stitch cached + fresh in input order
    final = _stitch(books, cached, fresh_fmvs)

    if not quiet:
        _print_table(final)

    if out_path:
        _write_json(out_path, final)


# ─── Step 1 — DB cache reuse ──────────────────────────────────────────────────

def _split_by_db_cache(books: list[dict], *, server_url: str,
                       max_age_days: float, force: bool
                       ) -> tuple[dict[int, dict], list[dict]]:
    """Bucket each book into (cached, needs_compute).

    Returns (cached_by_idx, needs_compute_list). cached_by_idx maps the
    original input index → DB row dict. needs_compute_list preserves only
    the books that need a fresh fetch+compute.
    """
    cached: dict[int, dict] = {}
    needs: list[dict] = []
    for i, book in enumerate(books):
        if force or not book.get("locg_id") or book.get("grade") is None:
            needs.append({"_idx": i, **book})
            continue
        row = _db_lookup(server_url, locg_id=book["locg_id"],
                         grade=book["grade"], max_age_days=max_age_days)
        if row:
            cached[i] = row
        else:
            needs.append({"_idx": i, **book})
    return cached, needs


def _db_lookup(server_url: str, *, locg_id: int, grade: float,
               max_age_days: float) -> dict | None:
    """Return the freshest matching FMV row, or None if not cached/stale.

    Defensive verification: even if the server returns rows, we re-check
    locg_id and grade match what we asked for. Older server versions silently
    ignore unknown query params (FastAPI behavior), so without this check a
    stale server would happily return ANY row at the matching grade and we'd
    write the wrong comic's FMV onto the input book.
    """
    try:
        resp = requests.get(
            f"{server_url}/api/comics",
            params={"locg_id": locg_id, "grade": grade,
                    "max_age_days": max_age_days},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        click.echo(f"Warning: DB cache lookup failed (locg_id={locg_id}): {e}",
                   err=True)
        return None
    rows = resp.json()
    # Belt-and-braces: filter by what we asked for, in case the server is
    # running an older version that doesn't honor the locg_id/max_age filters.
    rows = [r for r in rows
            if r.get("locg_id") == locg_id and r.get("grade") == grade]
    if not rows:
        return None
    # Tiebreak: pick the freshest. list_comics orders by id, not timestamp.
    rows.sort(key=lambda r: r.get("fmv_updated_at") or "", reverse=True)
    return rows[0]


# ─── Step 2 — Fetch comps via ebay-cli ─────────────────────────────────────────

def _fetch_comps(books: list[dict], ebay_cli_path: str, *,
                 force: bool) -> list[dict]:
    """Subprocess to ebay-cli sold_comps.py. Returns the parsed result list,
    in the same order as `books`."""
    sold_comps_py = os.path.join(ebay_cli_path, "sold_comps.py")
    if not os.path.exists(sold_comps_py):
        click.echo(f"Error: ebay-cli sold_comps.py not found at {sold_comps_py}.\n"
                   f"Set EBAY_CLI_PATH or pass --ebay-cli-path.", err=True)
        sys.exit(1)

    py = _resolve_python(ebay_cli_path)

    # Strip the orchestrator's _idx field; sold_comps.py doesn't expect it
    payload = [{k: v for k, v in b.items() if k != "_idx"} for b in books]

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as ftmp:
        json.dump(payload, ftmp)
        in_path = ftmp.name
    out_path = in_path + ".out.json"

    try:
        cmd = [py, sold_comps_py, "--batch", in_path, "--out", out_path, "--quiet"]
        if force:
            cmd.append("--force")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            click.echo(f"Error: ebay-cli sold_comps failed (exit {result.returncode}):\n"
                       f"{result.stderr}", err=True)
            sys.exit(1)
        return json.loads(Path(out_path).read_text())
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _resolve_python(ebay_cli_path: str) -> str:
    """Prefer the ebay-cli venv python (it has `requests` installed); fall
    back to system python3."""
    venv_py = os.path.join(ebay_cli_path, ".venv", "bin", "python")
    if os.path.exists(venv_py):
        return venv_py
    return shutil.which("python3") or sys.executable


# ─── Step 3 — Math + DB upsert ────────────────────────────────────────────────

def _compute_and_upsert_one(result: dict, original_book: dict, *,
                            server_url: str) -> dict:
    """Run FMV math + DB upsert for a single book. Returns the assembled result."""
    inp = result.get("input") or {}
    # Carry forward fields we may need that ebay-cli didn't echo (locg_id etc.)
    inp = {**inp, **{k: v for k, v in original_book.items()
                     if k not in ("_idx",) and v is not None}}
    target_grade = inp.get("grade")
    comps = result.get("comps", [])

    if target_grade is None:
        return {
            "input": inp, "fmv": None, "comp_count_total": len(comps),
            "queries_used": result.get("queries_used", []),
            "db_row": None, "source": "error",
            "error": "no target grade in input",
        }

    fmv = fmv_math.compute_fmv(comps, target_grade=target_grade)
    upserted = _upsert_fmv(server_url, inp, fmv) if fmv["fmv_low"] is not None else None

    return {
        "input": inp, "fmv": fmv, "comp_count_total": len(comps),
        "queries_used": result.get("queries_used", []),
        "db_row": upserted, "source": "fresh",
    }


def _upsert_fmv(server_url: str, inp: dict, fmv: dict) -> dict | None:
    """POST /api/comics with the computed FMV. Returns the row JSON or None."""
    body = {
        "title": inp["title"],
        "issue": str(inp["issue"]),
        "year": inp.get("year"),
        "grade": inp.get("grade"),
        "fmv_low": fmv["fmv_low"],
        "fmv_high": fmv["fmv_high"],
        "fmv_comps": fmv["n"],
        "fmv_confidence": _confidence_to_db_label(fmv["confidence"]),
        "fmv_notes": _build_notes(fmv),
    }
    if inp.get("locg_id"):
        body["locg_id"] = inp["locg_id"]
    if inp.get("locg_variant_id"):
        body["locg_variant_id"] = inp["locg_variant_id"]

    try:
        resp = requests.post(f"{server_url}/api/comics", json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        click.echo(f"Warning: DB upsert failed for {inp.get('title')} "
                   f"#{inp.get('issue')}: {e}", err=True)
        return None


def _confidence_to_db_label(label: str) -> str:
    """The DB schema constrains fmv_confidence to {'high','medium','low'}.
    Collapse our finer-grained label set onto that."""
    high_ish = {"HIGH"}
    med_ish = {"MEDIUM-HIGH", "MEDIUM"}
    if label in high_ish:
        return "high"
    if label in med_ish:
        return "medium"
    return "low"  # MEDIUM-LOW and LOW


def _build_notes(fmv: dict) -> str:
    parts = [f"window=±{fmv['window']}", f"cv={fmv['cv_pct']}",
             f"label={fmv['confidence']}"]
    return " | ".join(parts)


# ─── Step 4 — Stitch + present ────────────────────────────────────────────────

def _stitch(books: list[dict], cached: dict[int, dict],
            fresh: dict[int, dict]) -> list[dict]:
    """Combine cached and fresh results back into the input order."""
    out: list[dict] = []
    for i, book in enumerate(books):
        if i in cached:
            row = cached[i]
            out.append({
                "input": _input_summary(book),
                "fmv": _fmv_from_db_row(row),
                "comp_count_total": row.get("fmv_comps") or 0,
                "queries_used": [],
                "db_row": row,
                "source": "cached",
            })
        elif i in fresh:
            out.append(fresh[i])
        else:
            out.append({
                "input": _input_summary(book),
                "fmv": None,
                "comp_count_total": 0,
                "queries_used": [],
                "db_row": None,
                "source": "error",
                "error": "no comps fetched and no cache",
            })
    return out


def _input_summary(book: dict) -> dict:
    return {k: book.get(k) for k in
            ("item_id", "title", "issue", "year", "publisher", "grade",
             "locg_id", "locg_variant_id", "notes")
            if book.get(k) is not None}


def _fmv_from_db_row(row: dict) -> dict:
    """Project a Gixen `comics` row back into our fmv dict shape."""
    fmv_high = row.get("fmv_high")
    return {
        "n": row.get("fmv_comps") or 0,
        "window": None,
        "fmv_low": row.get("fmv_low"),
        "fmv_high": fmv_high,
        "median": None,
        "max_bid": fmv_math.clean_round(fmv_high * 0.80) if fmv_high else None,
        "cv": None,
        "cv_pct": "n/a",
        "confidence": (row.get("fmv_confidence") or "low").upper(),
        "trimmed_pool": [],
    }


# ─── I/O helpers ──────────────────────────────────────────────────────────────

def _read_batch(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise click.UsageError("Batch file must contain a JSON array.")
    return data


def _write_json(path: str, data) -> None:
    if path == "-":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        Path(path).write_text(json.dumps(data, indent=2))


def _print_table(rows: list[dict]) -> None:
    click.echo(f"{'#':>3}  {'Comic':<30} {'Grade':>5}  "
               f"{'FMV':<14} {'Med':>5}  {'n':>3}  {'CV':>5}  "
               f"{'Conf':<12} {'Max bid':>7}  Source")
    click.echo("-" * 110)
    for i, r in enumerate(rows, 1):
        inp = r["input"]
        label = f"{inp.get('title','?')} #{inp.get('issue','?')}"
        grade = inp.get("grade")
        fmv = r.get("fmv") or {}
        if fmv.get("fmv_low") is not None:
            fmv_str = f"${fmv['fmv_low']}–${fmv['fmv_high']}"
            med_str = f"${fmv.get('median') or '?'}"
            mb_str = f"${fmv.get('max_bid') or '?'}"
        else:
            fmv_str = "n/a"
            med_str = "n/a"
            mb_str = "n/a"
        click.echo(
            f"{i:>3}  {label[:30]:<30} {str(grade):>5}  "
            f"{fmv_str:<14} {med_str:>5}  {fmv.get('n','?'):>3}  "
            f"{fmv.get('cv_pct','?'):>5}  "
            f"{fmv.get('confidence','?'):<12} {mb_str:>7}  {r['source']}"
        )
