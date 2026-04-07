# Data Scraping Spec

## Objective

Collect source-native data and persist immutable raw JSON for downstream extraction.

This stage is capture-only:

- no PostgreSQL/Directus writes
- no schema flattening
- no cross-record merging

## Phase 1 Source Scope

- Google Maps (baseline source)
- TikTok (enabled after baseline stability)
- Instagram is out of Phase 1 scope

## Input

- Cafe seed list (`name`, `location`, optional `google_place_id`)
- Keyword/query seeds per source
- Source adapter config and crawl limits
- Run metadata (`run_id`, schedule timestamp, source)

## Raw Output Contract

Directory:

```text
data/raw/<run_date>/<source>/
```

Files:

- `<source>_raw_<run_timestamp>.json`
- `run_manifest.json` (optional summary)
- `errors_<run_id>.json` (failure records)

Raw envelope per record:

- `source_platform`
- `run_id`
- `scraped_at`
- `query`
- `source_record_id` (when available)
- `raw` (unaltered source payload)

## Source Collection Contracts

### Google Maps Adapter (P0)

Capture source fields needed to support schema targets in extraction:

- Place identity (`google_place_id`, canonical place URL)
- Cafe profile signals (name, address, district hints, lat/lng, price, opening hours)
- Social/profile links if visible (`ig_url`, `tiktok_url` hints)
- Photo references and metadata when available
- Optional review blocks as raw-only signals (not a Phase 1 schema target)

### TikTok Adapter (P1)

Capture:

- `post_url`
- caption/text
- author handle
- post timestamp when available
- embed metadata if available
- engagement snapshot when available
- cafe mention clues from query/context

### Instagram Adapter (P2)

Not active in Phase 1. Future contract mirrors TikTok shape.

## Extraction Intent (Schema-Aware, No Mapping Logic Here)

Scraping payloads should provide enough evidence for extraction to build:

- `cafes` candidates
- `cafe_photos` candidates
- `cafe_tags` candidates
- `influencer_reviews` candidates

`events` collection is not required in Phase 1.

## Failure Handling

- Continue run when a subset of queries fails.
- Retry transient failures with exponential backoff.
- Persist failures to `errors_<run_id>.json`; never drop silently.

## Done Criteria

- Every scheduled source run writes deterministic raw files.
- Raw records are reproducible inputs for extraction reruns.
- Source-native structure is preserved in `raw` payloads.
- Stage contains no PostgreSQL write path.

## References

- Stage flow and ownership boundaries: `data-pipeline-overview.md`
- Normalization and schema mapping rules: `data-extraction.md`
