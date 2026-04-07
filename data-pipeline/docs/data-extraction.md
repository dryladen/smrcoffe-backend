## Objective

Transform raw scraper JSON into normalized JSON contracts aligned to `database_schema_architecture.md`.

This stage is transform-only:

- reads raw JSON
- writes normalized JSON
- does not write to PostgreSQL/Directus

## Input

- `data/raw/<run_date>/<source>/`

## Output

Directory:

```text
data/normalized/<run_date>/
```

Primary normalized contracts:

- `cafes.json`
- `cafe_photos.json`
- `cafe_tags.json`
- `influencer_reviews.json`
- `events.json` (optional in Phase 1; may be empty)

Operational/quality contracts:

- `scrape_runs.json` (run telemetry contract, if used)
- `quarantine_<dataset>.json`

## File-to-Table Alignment

| Normalized file | Directus/PostgreSQL table |
|---|---|
| `cafes.json` | `cafes` |
| `cafe_photos.json` | `cafe_photos` |
| `cafe_tags.json` | `cafe_tags` |
| `influencer_reviews.json` | `influencer_reviews` |
| `events.json` | `events` |

`scrape_runs.json` is an operational contract and may live outside this schema set.

## Core Transformation Rules

### 1) Canonical cafe identity

- Build stable `slug` (lowercase, URL-safe, unique).
- Resolve source aliases to one canonical cafe record when confidence is high.
- Child records reference cafe via `cafe_slug` (loader resolves to `cafes.id`).

### 2) Type normalization

- Normalize timestamps to ISO-8601 with timezone.
- Normalize date/time-only fields for schema compatibility.
- Standardize arrays (for example `opening_days`) and JSON blobs.

### 3) URL and text normalization

- Canonicalize URLs (strip tracking params when safe).
- Trim and normalize text spacing.
- Normalize platform values for influencer data (`instagram` or `tiktok`).

### 4) Dedupe keys

- Cafes: `slug`
- Cafe photos: `cafe_slug + canonical_url`
- Cafe tags: `cafe_slug + normalized_tag`
- Influencer reviews: `platform + canonical_post_url`
- Events: `cafe_slug + event_date + normalized_title`

### 5) Publication/approval defaults

- `cafes.is_featured = false` unless explicitly curated.
- `cafes.is_published = false` unless explicitly approved by content workflow.
- `influencer_reviews.is_approved = false` for auto-scraped rows.
- `events.is_published = false` by default.

## Required Fields by Contract

- `cafes.json`: `name`, `slug`, `address`, `latitude`, `longitude`
- `cafe_photos.json`: `cafe_slug`, `url`
- `cafe_tags.json`: `cafe_slug`, `tag`
- `influencer_reviews.json`: `cafe_slug`, `influencer_name`, `platform`, `post_url`
- `events.json` (when present): `cafe_slug`, `title`, `event_date`

Records missing required fields must be quarantined, not dropped.

## Quarantine Policy

- Write invalid rows to `quarantine_<dataset>.json`.
- Include `reason_code` and source trace fields (`run_id`, `source_platform`, `source_record_id` when available).

Recommended `reason_code` values:

- `missing_required_field`
- `invalid_url`
- `invalid_timestamp`
- `invalid_platform`
- `dedupe_conflict`
- `schema_mismatch`

## Minimal Record Examples

### `cafes.json`

```json
{
  "slug": "kopi-kenangan-samarinda",
  "name": "Kopi Kenangan Samarinda",
  "address": "Jl. Contoh No. 10, Samarinda",
  "district": "Sungai Kunjang",
  "gmaps_url": "https://maps.google.com/?cid=123",
  "latitude": -0.502,
  "longitude": 117.153,
  "price_range": "Rp15k-40k",
  "opening_time": "08:00",
  "closing_time": "22:00",
  "opening_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
  "ig_url": "https://www.instagram.com/kopikenangan",
  "tiktok_url": "https://www.tiktok.com/@kopikenangan",
  "is_featured": false,
  "is_published": false
}
```

### `influencer_reviews.json`

```json
{
  "cafe_slug": "kopi-kenangan-samarinda",
  "influencer_name": "@creator",
  "platform": "tiktok",
  "post_url": "https://www.tiktok.com/@creator/video/1234567890",
  "embed_code": "<blockquote>...</blockquote>",
  "review_date": "2026-04-04",
  "is_approved": false
}
```

## Non-Target Datasets

Google Maps review text can still be normalized for analytics/search experiments, but it is not a required Directus table target in the current schema.

## Loader Handshake

- Loader consumes only normalized files from this stage.
- Loader resolves `cafe_slug` to `cafes.id` for child-table inserts.
- DB-generated fields (`id`, `created_at`, `updated_at`) are not set by extraction.

## References

- Pipeline stage boundaries: `data-pipeline-overview.md`
- Source capture rules: `data-scraping.md`
- Team handoff constraints: `tech-spec.md`
