# Technical Spec

## Purpose

Define the technical handoff boundary between the data pipeline team and the database/loader team.

## Tech Components

### Core Stack

| Component | Choice | Version | Notes |
|---|---|---|---|
| Scraper | crawl4ai | ^0.4.x | Handles JS-rendered pages (TikTok/Instagram). |
| Browser Automation | Playwright | ^1.48.0 | Python sync API for browser control. |
| Language | Python | ^3.11+ | Team already familiar with Python ecosystem. |
| Async HTTP | httpx / aiohttp | ^3.9+ | Async HTTP client for Directus API calls. |
| JSON Processing | orjson | ^3.10.0 | Fast JSON parsing for large datasets. |
| Validation | Pydantic | ^2.8.0 | Data validation and schema enforcement. |
| Scheduler | APScheduler | ^3.10.0 | Python-based job scheduling (alternative to cron). |
| Scheduler | Linux cron | N/A | Alternative: OS-level scheduling for simple workflows. |
| Storage | Directus | ^11.x | Headless CMS with PostgreSQL backend. |
| Database | PostgreSQL | ^16 | Managed by Directus on DO App Platform. |
| Infra | DO App Platform worker service | N/A | Runs as separate cron worker; no separate VPS needed. |

### Python Dependencies

```txt
# requirements.txt
crawl4ai>=0.4.0
playwright>=1.48.0
httpx>=0.11.0
aiohttp>=3.9.0
orjson>=3.10.0
pydantic>=2.8.0
apscheduler>=3.10.0
python-dotenv>=1.0.0
structlog>=24.4.0
```

### Directus API Configuration

| Setting | Value | Notes |
|---|---|---|
| API Base URL | `https://your-directus-instance.com` | Configurable via environment variable. |
| Authentication | Static Token | Bearer token for API access. |
| Connection Pool | 10 connections | Reuse connections for performance. |
| Request Timeout | 30 seconds | Timeout for API requests. |
| Retry Attempts | 3 | Retry failed requests with exponential backoff. |
| Batch Size | 100 records | Insert records in batches for performance. |

## Team Boundary

Pipeline team responsibility:

- Produce clean raw JSON and normalized JSON outputs.
- Enforce validation, dedupe, timestamp normalization, and quarantine in extraction.
- Deliver stable file contracts for loader ingestion.

Out of scope for pipeline team:

- PostgreSQL schema design (DDL), migrations, indexing, and DB runtime operations.
- Directus content modeling decisions beyond input contract alignment.

## File-to-Table Handoff Mapping

| Normalized file | Directus/PostgreSQL table | Handoff notes |
|---|---|---|
| `cafes.json` | `cafes` | Primary cafe upsert source. Generate `slug` as dedupe key; loader resolves `cafe_slug` to `cafes.id` for child records. |
| `cafe_photos.json` | `cafe_photos` | Loader maps `cafe_slug` → `cafe_id` FK. Dedupe on `cafe_slug + url`. |
| `cafe_tags.json` | `cafe_tags` | Loader maps `cafe_slug` → `cafe_id` FK. Dedupe on `cafe_slug + tag`. |
| `influencer_reviews.json` | `influencer_reviews` | Loader maps `cafe_slug` → `cafe_id` FK. Map `influencer_name`, `platform`, `post_url`, `embed_code`, `review_date`. Default `is_approved = false`. Dedupe on `platform + post_url`. |
| `events.json` | `events` | Optional in Phase 1. Loader maps `cafe_slug` → `cafe_id` FK. Default `is_published = false`. Dedupe on `cafe_slug + event_date + title`. |

Non-Phase-1 targets:

- `google_reviews.json` is not a required Directus table target. Can be normalized for analytics/search experiments if needed.
- `scrape_runs.json` is an operational run telemetry contract and may live outside Directus schema unless explicitly modeled.

## Loader FK Resolution

Loader must resolve `cafe_slug` references to `cafes.id` for all child tables:

1. Upsert `cafes.json` first to ensure parent rows exist.
2. For each child record (`cafe_photos`, `cafe_tags`, `influencer_reviews`, `events`):
   - Look up `cafes.id` where `cafes.slug = child.cafe_slug`
   - Set `child.cafe_id = cafes.id`
   - Remove `cafe_slug` from final insert payload

## Field Mapping Rules

### `influencer_reviews.json` → `influencer_reviews` table

| Normalized field | DB column | Notes |
|---|---|---|
| `cafe_slug` | `cafe_id` (FK) | Resolved via lookup |
| `influencer_name` | `influencer_name` | Direct map |
| `platform` | `platform` | Must be `instagram` or `tiktok` |
| `post_url` | `post_url` | Canonical URL |
| `embed_code` | `embed_code` | Raw HTML embed |
| `review_date` | `review_date` | Date-only format |
| - | `is_approved` | Default `false` for scraped rows |

### `cafes.json` → `cafes` table

| Normalized field | DB column | Notes |
|---|---|---|
| `slug` | `slug` | Unique, URL-safe |
| `name` | `name` | Direct map |
| `address` | `address` | Direct map |
| `district` | `district` | Direct map |
| `gmaps_url` | `gmaps_url` | Direct map |
| `latitude` | `latitude` | Float |
| `longitude` | `longitude` | Float |
| `price_range` | `price_range` | Direct map |
| `opening_time` | `opening_time` | Time format |
| `closing_time` | `closing_time` | Time format |
| `opening_days` | `opening_days` | Array of day strings |
| `ig_url` | `ig_url` | Direct map |
| `tiktok_url` | `tiktok_url` | Direct map |
| `is_featured` | `is_featured` | Default `false` |
| `is_published` | `is_published` | Default `false` |
| - | `id` | DB-generated |
| - | `created_at` | DB-generated |
| - | `updated_at` | DB-generated |

## Handoff Quality Gates

1. All normalized files are parseable JSON and schema-consistent with `data-extraction.md`.
2. Invalid records are quarantined with explicit `reason_code`, never silently dropped.
3. Dedupe keys exist for all target tables.
4. Time fields are ISO-8601 with timezone awareness; date-only fields use `YYYY-MM-DD`.
5. Platform values are normalized to `instagram` or `tiktok` for influencer data.
6. Required fields are present for each contract (see `data-extraction.md`).

## Critical Decisions Required

- Confirm whether `scrape_runs` should be a Directus table or remain an operational file contract.
- Confirm publication workflow ownership for influencer rows (team approves via Directus UI).
- Confirm whether Google Maps review text should be persisted to a separate table for analytics.

## Rate Limits and Quotas

| Source | Rate Limit | Quota | Notes |
|---|---|---|---|
| Google Maps API | 10 req/sec | 200 req/day | Geocoding API quota (adjust as needed). |
| TikTok Web Scrape | 1 req/2 sec | 500 posts/run | Rotating proxies recommended; 2-3 concurrent workers max. |
| Instagram Web Scrape | 1 req/3 sec | 300 posts/run | Phase 2 only; harder to scrape. |
| Directus API | 10 req/sec | 1000 req/hour | Write operations for loader stage. |

## Data Volume Constraints

| Metric | Limit | Notes |
|---|---|---|
| Max Cafes per Run | 200 | To prevent overwhelming the system. |
| Max Photos per Cafe | 10 | Quality over quantity. |
| Max Reviews per Run | 1000 | Configurable via command line. |
| Max Raw JSON Size | 50 MB | Per file; larger files split by date. |
| Max Normalized JSON Size | 20 MB | Per file; compressed and deduped data. |
| Raw Data Retention | 30 days | Auto-cleanup old raw files. |
| Normalized Data Retention | 90 days | Keep for reprocessing and debugging. |

## Resource Constraints

| Resource | Limit | Notes |
|---|---|---|
| Memory per Worker | 2 GB | DO App Platform basic worker tier. |
| CPU per Worker | 1 vCPU | Shared CPU on DO App Platform. |
| Disk Space | 10 GB | Temporary storage for raw/normalized JSON. |
| Concurrent Workers | 2-3 | Browser instances for parallel scraping. |
| Headless Browser Memory | 512 MB | Per Playwright instance. |

## Network Constraints

| Constraint | Limit | Notes |
|---|---|---|
| Bandwidth | 100 GB/month | DO App Platform outbound limit. |
| Proxy Rotation | 5-10 IPs | Rotating proxy pool for TikTok/IG. |
| Request Timeout | 30 seconds | Per HTTP request timeout. |
| Connection Pool | 100 connections | Maximum concurrent connections. |
| Retry Delay | 1-5 seconds | Exponential backoff for failed requests. |

## Performance Benchmarks

| Metric | Target | Notes |
|---|---|---|
| Full Pipeline Runtime | < 15 minutes | End-to-end for all stages. |
| Scraping Throughput | 50 cafes/hour | With 2-3 concurrent workers. |
| Extraction Speed | 500 records/minute | JSON processing and validation. |
| Loader Throughput | 100 records/minute | Directus API batch inserts. |
| Memory Usage | < 1.5 GB | Peak memory usage per worker. |

## Security Considerations

| Aspect | Implementation | Notes |
|---|---|---|
| API Authentication | Static Bearer Token | Stored in environment variable. |
| Proxy Authentication | Username/Password | For authenticated proxy services. |
| Credential Storage | Environment Variables | Never commit credentials to code. |
| Rate Limiting | Token Bucket | Per-source rate limiting to prevent abuse. |
| Data Encryption | HTTPS only | All API calls over HTTPS. |

## Monitoring and Logging

| Component | Tool | Notes |
|---|---|---|
| Application Logs | Structlog | JSON structured logging with context. |
| Error Tracking | Sentry | Exception tracking and alerting. |
| Performance Monitoring | Custom Metrics | Track throughput, latency, errors per stage. |
| Health Checks | HTTP Endpoint | `/health` endpoint for worker monitoring. |
| Alerting | Sentry Alerts | Critical errors and quota exceeded alerts. |

## Risks

| Risk | Level | Mitigation |
|---|---|---|
| TikTok blocks scraper IPs | High | Rotating proxies, rate limiting, headless browser. |
| FK resolution fails for orphan child records | High | Quarantine child records when `cafe_slug` not found; require parent upsert first. |
| Contract-to-table mismatch at loader stage | High | Maintain explicit mapping sheet; run contract validation before release. |
| Silent data quality drift from source changes | High | Preserve raw payloads; quarantine-first extraction; reprocess from raw when needed. |
| Embed code breaks if post deleted | Medium | Always store `post_url` as fallback link. |
| Low-quality or irrelevant posts scraped | Medium | Manual approval step via `is_approved = false` default; team curates in Directus admin. |
| Instagram scraping harder than TikTok | Medium | Prioritize TikTok first (Phase 1); defer Instagram to Phase 2. |
| Platform value drift | Medium | Validate platform values in extraction; add enum constraint in DB if needed. |
| Proxy IP banned mid-run | Medium | Implement proxy rotation every 50-100 requests. |
| Memory exhaustion on large runs | Medium | Implement pagination and streaming for large datasets. |
