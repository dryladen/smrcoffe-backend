# Data Pipeline

Python CLI for the Samarinda coffee discovery data pipeline.

Current implementation:

- Stage A scraping for Google Maps and TikTok
- Stage B extraction/normalization into JSON contracts
- Run metadata and quarantine outputs for debugging and reruns

Planned but not implemented in this package:

- Stage C loader that writes normalized data into Directus/PostgreSQL
- Downstream product/API reads from database tables

## Scope

This package captures source-native raw JSON, then converts it into normalized JSON files that a separate loader can ingest later.

It does **not** write to PostgreSQL or Directus.

## Pipeline Stages

1. **Stage A - Scrape**
   - Google Maps is the canonical cafe source.
   - TikTok is scraped for social posts and metadata.
   - Raw outputs are written under `data/raw/<date>/<source>/`.

2. **Stage B - Extract**
   - Reads raw JSON for a given run date.
   - Normalizes records into stable JSON contracts.
   - Quarantines invalid, unmatched, or conflicting records instead of dropping them.

3. **Stage C - Load (planned)**
   - Documented in `data-pipeline/docs/`, but not implemented here.
   - Intended to resolve `cafe_slug` foreign keys and upsert into the DB.

## Project Layout

```text
data-pipeline/
├── main.py                    # CLI entrypoint
├── requirements.txt
├── pipeline_utils.py
├── models/                    # Pydantic schemas
├── pipelines/
│   ├── google_maps_scraper.py # compatibility shim re-exporting the package runtime
│   ├── google_maps/           # current Google Maps implementation
│   │   ├── scraper_main.py
│   │   ├── browser.py
│   │   ├── extractors.py
│   │   ├── history.py
│   │   ├── identity.py
│   │   └── constants.py
│   ├── tiktok_scraper.py
│   └── extractor.py
├── docs/
│   ├── data-pipeline-overview.md
│   ├── data-scraping.md
│   ├── data-extraction.md
│   └── tech-spec.md
├── tests/
└── data/
    ├── raw/<date>/<source>/
    └── normalized/<date>/
```

Important runtime paths:

- Raw source data: `data/raw/<run_date>/<source>/`
- Raw source errors: `data/raw/<run_date>/<source>/errors_<run_id>.json`
- Per-run metadata: `data/raw/<run_date>/_runs/<run_id>.json`
- Normalized outputs: `data/normalized/<run_date>/`

Google Maps note:

- The runtime was refactored from the old single-file `pipelines/google_maps_scraper.py` layout into the `pipelines/google_maps/` package.
- `pipelines/google_maps_scraper.py` still exists as a compatibility shim for existing imports and CLI wiring, but new implementation work lives under `pipelines/google_maps/`.

## Setup

From `data-pipeline/`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Notes:

- The codebase depends on `crawl4ai`, `playwright`, and `playwright-stealth` for browser-driven scraping.
- `python -m playwright install chromium` is required before running the scrapers on a fresh environment.
- The docs target Python `3.11+`.

## CLI Usage

The main entrypoint is `main.py`.

### `run`

Run scrape + extract in one command.

```bash
python main.py run --keyword "coffee" --city "Samarinda" --tiktok-query "cafe samarinda"
```

Useful options:

- `--max-results 50` for Google Maps result limit
- `--tiktok-max 30` for TikTok result limit
- `--date 2026-04-07` to force a run date
- `--run-id custom_run_id` to reuse an explicit run ID
- `--show-browser` to run non-headless for debugging

If you pass flags without a subcommand, the CLI treats that as `run`.

### `scrape`

Run Stage A only.

```bash
python main.py scrape --keyword "coffee" --city "Samarinda"
python main.py scrape --tiktok-query "cafe samarinda"
python main.py scrape --keyword "coffee" --city "Samarinda" --tiktok-query "cafe samarinda"
```

### `extract`

Run Stage B only against existing raw data.

```bash
python main.py extract --date 2026-04-07
python main.py extract --date 2026-04-07 --raw-dir data/raw --normalized-dir data/normalized
```

`extract` reads all `*.json` files under `data/raw/<date>/`, skips `errors_*.json`, and writes normalized files into `data/normalized/<date>/`.

For Google Maps raw records, extraction relies on the persisted scrape payload remaining source-shaped and traceable:

- top-level metadata keeps `source_platform`, `run_id`, `scraped_at`, `query`, and `source_record_id`
- nested `raw` payload should continue to include source-facing location fields such as `maps_link`, `gmaps_url`, `latitude`, and `longitude`
- internal canonical identity helpers used during scrape-time binding are not part of the persistence contract and are intentionally excluded from the saved raw payload

### `list`

List available raw and normalized data grouped by date.

```bash
python main.py list
python main.py list --date 2026-04-07
```

## Normalized Outputs

Current extraction writes these files under `data/normalized/<run_date>/`:

- `cafes.json`
- `cafe_photos.json`
- `cafe_tags.json`
- `influencer_reviews.json`
- `events.json`

Current behavior notes:

- `events.json` is written today, but Stage C event loading is still future/planned.
- Google Maps is the source of canonical cafe identities.
- TikTok contributes `influencer_reviews` plus hashtag-derived tags when a cafe match is resolved.

## Quarantine Files

Extraction writes both an aggregate quarantine file and dataset-specific quarantine files:

- `quarantine.json`
- `quarantine_cafes.json`
- `quarantine_cafe_photos.json`
- `quarantine_cafe_tags.json`
- `quarantine_influencer_reviews.json`

Quarantine records preserve source trace fields such as `run_id`, `source_platform`, `source_record_id`, `raw_data`, and `validation_errors`.

Typical quarantine reasons include:

- missing required fields
- invalid timestamps or URLs
- schema mismatches in raw payloads
- dedupe conflicts between candidate cafe identities
- TikTok posts that cannot be matched to a known cafe slug

## Tests

Run the test suite from `data-pipeline/`:

```bash
python -m unittest discover -s tests
```

The test suite covers extraction behavior and regression cases around Google Maps identity and deduplication.

For a faster commit-time subset, also run this from `data-pipeline/`:

```bash
python -m unittest tests.test_dedup_regressions tests.test_extractor
```

## Git Hooks And CI

Install the tracked team hooks from the repository root:

```bash
./scripts/install-hooks.sh
```

Hook behavior:

- `.githooks/pre-commit` runs the fast offline regression subset when staged changes touch `data-pipeline/`.
- `.githooks/pre-push` runs the full offline suite when pushed changes touch `data-pipeline/` or the tracked hook/CI assets.
- Both hooks change into `data-pipeline/` before running Python because the current import layout is directory-sensitive.

Canonical commands used by hooks and CI:

```bash
cd data-pipeline && python -m unittest tests.test_dedup_regressions tests.test_extractor
cd data-pipeline && python -m unittest discover -s tests
```

GitHub Actions runs the same full offline suite on pushes and pull requests that touch `data-pipeline/`, `.githooks/`, `scripts/install-hooks.sh`, or `.github/workflows/data-pipeline.yml`.

## Important Caveats

- **Google Maps is canonical**: the extractor builds known cafe slugs from normalized Google Maps cafe records first, then uses that set to match TikTok posts.
- **TikTok matching depends on Google Maps data**: if a cafe is not present in the Google Maps normalized set for that run date, TikTok posts for that cafe are expected to be quarantined as unmatched.
- **Identity/dedup is intentionally conservative**: the pipeline removes same-day duplicate Google raw records before normalization, merges records that appear to represent the same business, and quarantines later records when a slug collision exists without strong business-match evidence.
- **Extractor sync depends on persisted raw fields**: Google Maps extractor sync expects stable persisted trace/location fields such as `source_record_id`, `maps_link`, `gmaps_url`, and coordinates; scrape-time canonical identity internals are derived at runtime rather than persisted.
- **Extraction is date-scoped**: `extract --date YYYY-MM-DD` only processes raw files under that date directory.
- **Stage C is not here**: normalized files are the handoff boundary; DB writes described in the docs are future/planned work for another stage.

## Related Docs

- `data-pipeline/docs/data-pipeline-overview.md`
- `data-pipeline/docs/data-scraping.md`
- `data-pipeline/docs/data-extraction.md`
- `data-pipeline/docs/tech-spec.md`
