# SMR Coffee — Backend

A locally-focused website that helps people discover coffeeshops in Samarinda, East Kalimantan. Each café has its own dedicated page featuring photos, embedded influencer reviews (from Instagram/TikTok), and upcoming events. The site serves both local coffee lovers and visitors who want to explore the city's café scene with confidence.

## Tech Stack

- **[Directus](https://directus.io/)** (v11.14.1) — Headless CMS & API layer
- **[PostGIS](https://postgis.net/)** (PostgreSQL 13) — Spatial database for location-based queries
- **Docker Compose** — Container orchestration

## Prerequisites

- [Docker](https://www.docker.com/) & Docker Compose

## Getting Started

1. **Clone the repository**

   ```bash
   git clone <repository-url>
   cd smrcoffe-backend
   ```

2. **Create your Docker Compose config**

   Copy the sample file and fill in your own credentials:

   ```bash
   cp docker-compose.yml.sample docker-compose.yml
   ```

   Edit `docker-compose.yml` and update the placeholder values:
   - `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`
   - `SECRET` — Directus secret key
   - `ADMIN_EMAIL` / `ADMIN_PASSWORD` — Directus admin credentials
   - `PUBLIC_URL` — Your public-facing URL
   - Storage settings (local or S3)
   - *(Optional)* Email transport settings (AWS SES)

3. **Start the services**

   ```bash
   docker compose up -d
   ```

4. **Access Directus**

   Open [http://localhost:8055](http://localhost:8055) and log in with the admin credentials you configured.

## Project Structure

```
smrcoffe-backend/
├── docker-compose.yml.sample   # Template for Docker Compose config
├── data/database/              # PostgreSQL data volume (git-ignored)
├── uploads/                    # Directus file uploads
└── extensions/                 # Custom Directus extensions
```

## Storage

The sample config includes placeholders for **AWS S3** storage. For local development you can switch to the local filesystem driver — see the Directus [storage docs](https://docs.directus.io/self-hosted/config-options.html#file-storage) for details.

## License

This project is private and not currently licensed for public distribution.