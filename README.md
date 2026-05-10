# Gixen CLI

A command-line tool for managing your [Gixen](https://www.gixen.com) eBay snipes.

Gixen's official API is disabled for some accounts. This CLI works by automating the Gixen web interface directly, submitting the same forms your browser would.

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file (or export the variables):

```
GIXEN_USERNAME=your_username
GIXEN_PASSWORD=your_password
```

## Usage

```bash
# List all current snipes
python cli.py list

# Add a snipe
python cli.py add <item_id> <max_bid>
python cli.py add 123456789 25.50 --offset 3 --group 1

# Edit an existing snipe
python cli.py edit <item_id> <new_max_bid>

# Remove a snipe
python cli.py remove <item_id>

# Purge completed/ended snipes
python cli.py purge

# Compute FMV for a batch of comics (writes to Gixen DB)
python cli.py fmv --batch books.json --out results.json
python cli.py fmv --batch books.json --force          # bypass caches
python cli.py fmv --batch books.json --max-age-days 1 # only reuse FMVs from last 24h
```

### Options

- `--offset` — Seconds before auction end to place the bid (1-15, default: 6)
- `--group` — Snipe group (0=none, 1-10). Items in the same group are mutually exclusive: Gixen will only bid on one.

### `fmv` command

Computes fair market value for one or more comics by:

1. Checking the Gixen DB for a recent FMV (default: 7-day TTL via `--max-age-days`)
2. For books needing fresh comps, shelling out to [`ebay-cli sold_comps.py`](https://github.com/hsukenooi/ebay-cli) (which itself caches SerpApi responses for 7 days)
3. Running IQR + quartile math + a confidence rubric on the comp pool
4. Upserting the result into `comics` via `POST /api/comics`

**Input shape** (`--batch books.json`):

```json
[
  {"item_id": "147295505028", "title": "Uncanny X-Men", "issue": "185",
   "year": 1984, "grade": 8.0, "locg_id": 1081721}
]
```

`item_id` is used for self-exclusion (don't comp against your own active listing). `locg_id` enables DB cache reuse.

**Performance** with caches warm: ~200ms for a 19-book batch (vs ~13s on cold fetch).

## Tests

```bash
# Unit tests (no credentials needed)
pytest tests/test_gixen_client.py

# Integration tests (requires credentials)
pytest -m integration
```
