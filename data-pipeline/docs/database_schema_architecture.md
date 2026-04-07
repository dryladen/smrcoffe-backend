## Database Schema (Directus / PostgreSQL)

### Table: `cafes`

One row per café. Core table everything else links to.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid (PK) | Auto-generated |
| `name` | text | Café name |
| `slug` | text (unique) | URL slug e.g. `kopi-kenangan-samarinda` |
| `address` | text | Full street address |
| `district` | text | Area/district in Samarinda e.g. Sungai Kunjang |
| `gmaps_url` | text | Google Maps embed or link URL |
| `latitude` | float | Café map pin — used for interactive map feature |
| `longitude` | float | Café map pin — used for interactive map feature |
| `price_range` | text | e.g. `Rp15k–40k`, or enum: `budget` `mid` `premium` |
| `opening_time` | time | e.g. `08:00` |
| `closing_time` | time | e.g. `22:00` |
| `opening_days` | text[] | e.g. `["Mon","Tue","Wed","Thu","Fri"]` |
| `ig_url` | text | Instagram profile URL |
| `tiktok_url` | text | TikTok profile URL |
| `is_featured` | boolean | Pinned on homepage (for future paid tier) |
| `is_published` | boolean | Draft vs live toggle |
| `created_at` | timestamp | Auto |
| `updated_at` | timestamp | Auto |

---

### Table: `cafe_photos`

Separate table — one café has many photos.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid (PK) | Auto |
| `cafe_id` | uuid (FK → [cafes.id](http://cafes.id)) | Which café |
| `url` | text | Image URL (Directus Files or external CDN) |
| `caption` | text | Optional caption |
| `order` | integer | Display order (1 = hero/cover photo) |
| `created_at` | timestamp | Auto |

---

### Table: `cafe_tags`

Many-to-many: one café can have many tags.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid (PK) | Auto |
| `cafe_id` | uuid (FK → [cafes.id](http://cafes.id)) | Which café |
| `tag` | text | e.g. `cozy`, `outdoor`, `study-friendly`, `instagrammable`, `pet-friendly` |

---

### Table: `influencer_reviews`

One café can have many influencer reviews.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid (PK) | Auto |
| `cafe_id` | uuid (FK → [cafes.id](http://cafes.id)) | Which café |
| `influencer_name` | text | e.g. `@namainfluencer` |
| `platform` | text | `instagram` or `tiktok` |
| `post_url` | text | Link to original post |
| `embed_code` | text | Raw HTML embed code from IG/TikTok |
| `review_date` | date | Date of original post |
| `is_approved` | boolean | Default `false`. Scraped content requires manual approval. Manual embeds default to `true`. |
| `created_at` | timestamp | Auto |

---

### Table: `events` *(ready for v2, create now)*

Future-proofing — empty at launch but schema is ready.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid (PK) | Auto |
| `cafe_id` | uuid (FK → [cafes.id](http://cafes.id)) | Which café |
| `title` | text | Event name |
| `description` | text | Short description |
| `event_date` | date | When |
| `event_time` | time | What time |
| `is_published` | boolean | Draft vs live |
| `created_at` | timestamp | Auto |

---

### Relationships Overview

```
cafes
  ├── cafe_photos        (one-to-many)
  ├── cafe_tags          (one-to-many)
  ├── influencer_reviews (one-to-many)
  └── events             (one-to-many, v2)
```

### Schema Decisions & Notes

- `slug` is the URL key — e.g. `/cafe/kopi-kenangan-samarinda`. Must be unique, URL-safe, lowercase.
- `opening_days` stored as array so you can query "open on Sunday" easily later.
- `is_published` lets you add a café as draft before it goes live — useful for QA.
- `is_featured` is the hook for future paid listing feature — just flip to true.
- `embed_code` stores the raw IG/TikTok embed HTML. Log the source `post_url` separately as backup if embed breaks.
- `events` table created now even if empty — avoids a painful migration later.
- `latitude` + `longitude` are separate from `gmaps_url`. `gmaps_url` is for the per-café embed iframe; lat/lng are for the global discovery map.

---

## 🏗️ Architecture

```
Browser
  → Next.js (frontend + API routes) — hosted on DO App Platform
      → Directus REST/GraphQL API — hosted on DO App Platform
          → PostgreSQL (Directus managed DB) — DO Managed Database

DO App Platform (separate worker service, v2 onward)
  → Python scraper (cron job)
      → Directus API (writes scraped reviews, is_approved = false)
```

### How the pieces connect

- **Next.js** fetches café data from **Directus REST/GraphQL API** at build time (SSG) or request time (SSR)
- **Directus** manages its own PostgreSQL DB — team adds/edits cafés through Directus admin UI (no code needed)
- **DO App Platform** hosts both Next.js + Directus as separate services
- **DO Managed Database** handles PostgreSQL — no self-managed DB
- Scraper writes to Directus via API with `is_approved = false`, team approves inside Directus admin

### Why no dedicated backend (Node.js / Go)?

- Next.js API Routes handle any custom logic at MVP scale
- Directus already exposes a full REST + GraphQL API — no need to hand-roll endpoints
- A separate backend adds infra complexity with no benefit at this stage

### When to revisit this decision

- Traffic exceeds DO App Platform limits
- Business logic becomes too complex for Next.js API routes
- Real-time features (live notifications, chat) are needed

---

## 🛠️ Tech Stack

| Layer | Choice | Why |
| --- | --- | --- |
| Frontend | Next.js 14 | SSG per café page = SEO-ready from day 1, React ecosystem |
| Styling | Tailwind CSS | Fast to build, easy to maintain, supports high-quality UI |
| CMS | Directus (self-hosted) | Beautiful admin UI, REST + GraphQL API out of the box, no vendor lock-in |
| Database | PostgreSQL via Directus | Directus manages its own DB — no separate DB service needed |
| Hosting | DigitalOcean App Platform | Hosts Next.js + Directus as separate services, managed DB add-on available |
| Influencer Embeds | Native IG/TikTok embed codes | No API needed, links back to original post |
| Maps | Google Maps Embed (MVP) → Mapbox GL JS (v2) | Iframe for MVP, interactive pins for v2 discovery map |
| Analytics | Umami (self-hosted on DO) | Lightweight, privacy-friendly, no third-party tracking |
