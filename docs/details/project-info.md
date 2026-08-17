# VoucherBot — Project Information

This document captures the product-facing context for VoucherBot. It complements the more implementation-oriented reference in [architecture.md](architecture.md) and the settings reference in [configuration.md](configuration.md).

## What the project is for

VoucherBot is a Python service for discovering certification-related promotions, voucher opportunities, and discount announcements from a mix of public sources. The current scope is to collect candidate posts, filter likely voucher content, extract structured promotion details, and surface them through a small API and email notifications.

## Current capabilities

The repository currently supports:

- monitoring Reddit communities when Reddit credentials are configured
- ingesting RSS and Atom feeds from blogs, news sites, and forums
- scraping website and event pages through configurable collectors
- scoring posts against a keyword catalog before AI analysis
- using AI providers to extract structured promotion data such as vendor, voucher code, discount, registration URL, and dates
- storing the results in PostgreSQL and linking candidate posts to canonical events
- sending voucher alerts by email through Resend

## Source catalog and ingestion model

The source catalog is not hard-coded in the database schema. Instead, it is seeded from [voucherbot/database/bootstrap.py](../../voucherbot/database/bootstrap.py), which defines the initial source list and keyword inventory. The catalog covers official vendor feeds, community feeds, event pages, and curated voucher pages.

The ingestion model is intentionally modular:

- collectors are provider-specific,
- the pipeline is shared across sources,
- the scheduler is DB-driven rather than tied to a separate job runner,
- the system can add new sources by extending the seeded catalog and config rather than changing the schema.

## Project structure at a glance

The repository is organized around a few major concerns:

- API and runtime entry point: [voucherbot/main.py](../../voucherbot/main.py)
- configuration and environment handling: [voucherbot/config/settings.py](../../voucherbot/config/settings.py)
- persistence and schema: [voucherbot/models](../../voucherbot/models) and [migrations](../../migrations)
- ingestion and matching logic: [voucherbot/services](../../voucherbot/services)
- external integrations: [voucherbot/providers](../../voucherbot/providers)
- test coverage: [tests](../../tests)

## How it is operated

The project is designed to run as a single service with a local Docker setup and a Render-friendly deployment configuration. In local development, it uses `.env`-based configuration and starts the API plus scheduler together. In production, startup is expected to skip bootstrapping and rely on existing schema/data.

## Documentation map

Use the docs in this order:

- [README.md](../../README.md) for the short project overview and local setup entry point
- [docs/details/architecture.md](architecture.md) for the technical implementation details
- [docs/details/testing.md](testing.md) for the test workflow
- [docs/setup/render-deployment.md](../setup/render-deployment.md) and the environment guides in [docs/setup](../setup) for deployment and external service setup

## Technical detail preserved from the architecture reference

The earlier architecture document contained a deeper implementation guide that is still relevant to the project. The most important pieces are summarized below so the information is not lost.

### Runtime architecture

VoucherBot is a single-process FastAPI application backed by PostgreSQL. The process contains:

- a FastAPI API layer for health and read-only data access,
- a background scheduler loop that repeatedly processes due sources,
- a processing pipeline that ingests, filters, deduplicates, analyzes, and stores promotion candidates.

The scheduler and the API share the same Python process and the same SQLAlchemy async engine.

### High-level data flow

```text
Sources (RSS / Web / Reddit)
  ↓
Collector
  ↓
Keyword filtering
  ↓
Deduplication
  ↓
AI extraction
  ↓
Event matching
  ↓
Email notification
```

### Scheduler and dispatcher behavior

The scheduler runs as one `asyncio.Task` and processes eligible sources sequentially. It uses the `sources` table as its scheduling state. Each source becomes eligible when:

- it is enabled,
- it is not in backoff,
- its `next_due_at` is empty or already in the past.

The dispatcher owns the lease and lifecycle behavior:

- it acquires a row-level lease from the `pipeline_lock` table,
- selects one due source at a time,
- updates the source schedule after success or failure,
- applies exponential backoff for recoverable failures,
- disables a source immediately for unrecoverable client or HTTP errors such as 404, 403, 401, or 410.

### Ingestion pipeline

Each source passes through the same sequence:

1. Collect
   - Reddit sources use the Reddit collector.
   - Feed-based sources use the RSS collector.
   - Page-based sources use the website collector.

2. Keyword filtering
   - the system loads enabled keywords from the database and scores the text.
   - posts below the threshold are dropped.
   - curated pages with a `note_selector` bypass the filter.

3. Deduplication and upsert
   - identity is derived from a normalized URL,
   - content changes are detected by a content hash,
   - unchanged content is skipped and new or changed content is inserted or updated.

4. AI extraction
   - new or updated posts are sent to the AI analyzer,
   - the analyzer returns structured promotion data and an `is_voucher` flag.

5. Event matching
   - the extracted data is matched to an existing canonical event or used to create a new one,
   - match confidence is classified as `AUTO_MERGED`, `POSSIBLE_MATCH`, or `NEW`.

6. Email notification
   - voucher candidates are emailed through Resend,
   - the post is only marked as notified after the send succeeds.

### Provider architecture

The collector layer is provider-specific but normalized through a shared interface:

- `BaseCollector` defines the contract for collectors.
- RSS collectors handle feed formats such as RSS, Atom, and JSON Feed.
- Reddit collectors use the Reddit API when available and fall back to public RSS endpoints when needed.
- Website collectors parse HTML using CSS selectors and can extract structured notes from curated pages.

HTTP requests go through a shared policy layer that enforces robots policy, crawl delay, and a consistent user-agent.

### AI service details

The current AI layer tries Groq models first and uses Gemini as a fallback. The implementation is designed to avoid depending on a single provider and includes:

- concurrent provider attempts,
- rate limiting and token-budget management,
- retry handling for rate-limit responses,
- a structured JSON schema for extracted event data.

### Data model reference

The core database model is split across several tables and views:

- `sources` stores the source catalog, polling state, priority tiers, and scheduling metadata.
- `posts` stores ingested content and the AI analysis result alongside deduplication fields.
- `events` stores canonical promotional events and merge log history.
- `keywords` stores the keyword scoring catalog.
- `vendor_mappings` resolves a source URL/name to its canonical vendor.
- `notification_outbox` is the transactional outbox for voucher alert emails.
- `pipeline_lock` is used for the dispatcher lease.
- `voucher_posts` is a read-only view of AI-confirmed vouchers.

The important relationship is that many posts can reference the same canonical event, while the posts themselves remain distinct and are never merged.

### REST API surface

The API is intentionally minimal and read-only, and does not implement authentication. The single route is:

- `GET /health`

This endpoint reports service liveness and database reachability, and is rate-limited per client IP.

### Email and notifications

The notification flow is implemented through the email service and Resend integration using a transactional outbox. Alerts contain voucher details such as vendor, promotion name, certification list, discount, voucher code, registration URL, and date information. The same voucher payload can also be POSTed to a remote bot server webhook.

### Database and deployment details

The project uses SQLAlchemy async with `asyncpg`, Alembic migrations, and Docker-based local deployment. The startup path differs by environment:

- in non-production mode, the app applies Alembic migrations and seeds the source catalog,
- in production mode, the app assumes the schema and seed data already exist and uses a DML-only role.

The repository also includes Render deployment configuration and a source-catalog verification script for smoke testing ingestion sources.

### Configuration reference

The runtime settings are centralized in [configuration.md](configuration.md) and the settings module. The important categories are:

- core runtime settings,
- email settings,
- Reddit ingestion settings,
- scraper policy settings,
- scheduler and backoff settings,
- AI provider settings,
- event-matching weights and source-priority ordering.

### Key design decisions

The current implementation favors a pragmatic architecture:

- single-process operation with one scheduler loop,
- PostgreSQL as the source of truth for state and coordination,
- DB-driven scheduling rather than a separate queue worker,
- shared ingestion pipeline across all source types,
- canonical event deduplication rather than merging posts together.

## Current limitations and design choices

The current implementation is intentionally simple and pragmatic:

- it runs as one process with one scheduler loop,
- it uses PostgreSQL for state and coordination,
- it relies on a shared catalog of sources and keywords,
- it does not implement user authentication or a separate worker queue.

That makes it easy to operate and reason about, while still allowing the ingestion and matching pipeline to grow over time.
