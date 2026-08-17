# VoucherBot — Detailed Summary

This document is the fuller, implementation-oriented summary for VoucherBot. It complements the shorter technical reference in [architecture.md](architecture.md) and the product-oriented overview in [project-info.md](project-info.md).

## Overview

VoucherBot is an async Python service that continuously monitors RSS feeds, vendor blogs, event pages, and Reddit subreddits for IT/cloud certification vouchers and promotions. When a voucher is detected, it stores the candidate post, extracts structured promotion data with AI, matches it to a canonical event, and can dispatch an email alert through Resend.

The runtime shape is intentionally simple: one FastAPI process, one PostgreSQL database, and one background scheduler loop. There are no separate worker queues or external job runners.

---

## High-level data flow

```text
Sources (RSS / Web / Reddit)
  ↓
Collector (provider-specific)
  ↓
Keyword filter
  ↓
Deduplication (identity_hash / content_hash)
  ↓
AI extraction (Groq → Gemini fallback)
  ↓
Event matcher (score-based dedup of promotions)
  ↓
Email notification (Resend)
```

This flow is shared by every source type. The pipeline is deliberately provider-agnostic once a source has been normalized into a list of posts.

---

## Runtime architecture

```text
FastAPI process (uvicorn)
├─ REST API
│  └─ /health (rate-limited liveness + DB probe)
└─ Background scheduler
   └─ sweep → dispatch_tick → pipeline → sleep

PostgreSQL (asyncpg / SQLAlchemy async)
```

The API and the scheduler share the same Python process and the same SQLAlchemy async engine. The scheduler runs as a single `asyncio.Task` started in the FastAPI lifecycle context.

---

## Module map

```text
voucherbot/
├── main.py
├── config/settings.py
├── core/
│   ├── exceptions.py
│   └── logging.py
├── database/
│   ├── connection.py
│   ├── init_db.py
│   └── bootstrap.py
├── models/
│   ├── source.py
│   ├── post.py
│   ├── event.py
│   ├── keyword.py
│   ├── vendor_mapping.py
│   ├── notification.py
│   └── pipeline_lock.py
├── providers/
│   ├── base.py
│   ├── http_policy.py
│   ├── rss/collector.py
│   ├── reddit/
│   │   ├── client.py
│   │   └── collector.py
│   ├── website/collector.py
│   ├── pearsonvue/collector.py
│   └── training_provider/collector.py
├── services/
│   ├── scheduler.py
│   ├── dispatcher.py
│   ├── event_consolidation.py
│   ├── retention.py
│   ├── ingestion/
│   │   ├── pipeline.py
│   │   ├── dedup.py
│   │   └── event_matcher.py
│   ├── ai/
│   │   ├── analyzer.py
│   │   ├── event_matcher_ai.py
│   │   └── schema.py
│   ├── email/
│   │   ├── sender.py
│   │   └── notifications.py
│   └── bot_notification/
│       └── notifier.py
└── api/
    ├── rate_limit.py
    └── routers/
        └── health.py
```

---

## Scheduler

The scheduler is implemented in [voucherbot/services/scheduler.py](../../voucherbot/services/scheduler.py). It runs as a single `asyncio.Task` called from the app lifespan. It never uses APScheduler or Celery; scheduling state lives in the `sources` table.

### Loop logic

```python
while True:
    ran = await _run_sweep()
    sleep = await _seconds_until_next_due()
    await asyncio.sleep(sleep)
```

During each pass the scheduler tries to process every due source sequentially. There is no concurrent source processing in the current design.

### Sweep logic

Each sweep calls `dispatch_tick` repeatedly until it returns `idle`. The loop is intentionally conservative to keep CPU flat and avoid overloading downstream APIs.

### Due-source selection

A source is eligible when:

- `enabled = true`
- config is not marked `unsupported`
- `next_due_at IS NULL OR next_due_at <= now()`
- `backoff_until IS NULL OR backoff_until <= now()`
- Reddit sources are excluded when `reddit_ingestion_enabled = false`

The scheduler orders eligible sources by `next_due_at ASC NULLS FIRST, priority_tier ASC`.

### Priority tiers and poll intervals

| Tier | Default interval | Typical sources |
|---|---:|---|
| A | 15 min | High-signal Reddit subs, key announcements |
| B | 60 min | Official vendor blogs |
| C | 240 min | Community forums, aggregators |
| D | 720 min | Event pages, podcast feeds |

Each source can override the interval through its config entry.

---

## Dispatcher

The dispatcher in [voucherbot/services/dispatcher.py](../../voucherbot/services/dispatcher.py) owns the lease and source lifecycle.

### Lease mechanism

A single row in `pipeline_lock` acts as a distributed mutex. If horizontal scaling is introduced later, the service can safely coordinate by acquiring that row atomically. The lease TTL defaults to 6 hours and is always released in a `finally` block.

### Source lifecycle after a tick

Success:

- `consecutive_failures = 0`
- `backoff_until = NULL`
- `next_due_at = now() + poll_interval_minutes`
- `avg_runtime_ms` is updated with a rolling average

Recoverable failure:

- `consecutive_failures += 1`
- exponential backoff is applied, capped at `source_backoff_max_minutes`
- `next_due_at = backoff_until`

Unrecoverable failure (404, 403, 401, 410, and similar):

- `enabled = false` immediately
- the source is disabled permanently for that deployment
- no backoff is applied

---

## Ingestion pipeline

`run_pipeline_for_source` runs several sequential stages for each source per tick.

### Stage 0 — Collect

A collector is resolved from the source type and config:

- `SourceType.REDDIT` → Reddit collector
- config contains `feed_url` → RSS collector
- config contains `article_selector` → Website collector

The collector returns a list of normalized posts.

### Stage 1 — Keyword filter

Each post title and content is scored against the `keywords` table. Posts scoring below the threshold are dropped. Curated pages with a `note_selector` bypass the keyword filter and are treated as voucher pages even when the wording is sparse.

Example scores:

| Keyword | Score |
|---|---:|
| voucher, coupon, promo code, free exam | 5 |
| free certification, discount, redeem | 4 |
| retake, limited time, beta access | 3 |
| certification, exam, webinar | 1 |

### Stage 2 — Deduplication

Two hashes per post provide the core deduplication logic:

- `identity_hash` based on a normalized URL, which gives a stable identity for a page
- `content_hash` based on title/content/date, which detects changed content

The logic inserts new URLs, updates changed content, and skips unchanged items. This prevents the pipeline from endlessly reprocessing the same page while still capturing updates.

### Stage 3 — AI extraction

New or updated posts are sent to [voucherbot/services/ai/analyzer.py](../../voucherbot/services/ai/analyzer.py). The analyzer returns an `ExtractedEvent` object with structured promotion fields and an `is_voucher` flag.

The structured fields include:

- `vendor`
- `promotion_name`
- `promotion_type`
- `certifications`
- `voucher_code`
- `discount`
- `registration_url`
- `start_date`
- `end_date`
- `regions`

### Stage 4 — Event matching

The matcher in [voucherbot/services/ingestion/event_matcher.py](../../voucherbot/services/ingestion/event_matcher.py) decides whether an extracted promotion is the same real-world promotion as an existing active event. By default it asks the qwen reasoning model ([voucherbot/services/ai/event_matcher_ai.py](../../voucherbot/services/ai/event_matcher_ai.py)) to compare the incoming promotion against the candidate events that the deterministic weighted score flags as possible matches (score >= `possible_match_threshold`, capped by `ai_candidate_limit`):

- `is_same_promotion` and `confidence >= ai_auto_merge_confidence` → `AUTO_MERGED`
- `is_same_promotion` and `confidence >= ai_possible_match_confidence` → `POSSIBLE_MATCH`
- otherwise → `NEW`

When the model is unavailable, no `GROQ_API_KEY` is configured, or no candidates exist, it falls back to the legacy weighted score over registration URL, voucher code, promotion-name similarity, vendor, discount, promotion type, certification overlap, and date overlap.

The result is one of `AUTO_MERGED`, `POSSIBLE_MATCH`, or `NEW`.

### Stage 5 — Email notification

If the AI extraction yields a voucher candidate and the event decision is not `AUTO_MERGED`, the pipeline stages a delivery intent into the transactional outbox (`notification_outbox`) before the final commit. The scheduler then delivers PENDING rows through Resend with a stable idempotency key; the post is marked `is_notified` only after the send succeeds. The same payload is POSTed to the optional bot server webhook alongside the email.

---

## Providers

### BaseCollector

All collectors implement a common contract around normalized posts.

### RSS collector

- uses the HTTP policy layer and robots-aware requests,
- supports RSS, Atom, and JSON Feed formats,
- can recover malformed XML and rewrite known-bad URLs.

### Reddit collector

- uses the Reddit API when available,
- falls back to public RSS endpoints when needed,
- is controlled by the Reddit ingestion feature flag.

### Website collector

- uses CSS selectors to scrape content,
- can extract structured notes from curated voucher pages,
- skips sources that are blocked by policy.

### Pearson VUE collector

- scrapes official vendor pages (AWS, Microsoft, Cisco, CompTIA, etc.) for exam promotions,
- extracts slide/promo/card content with per-vendor heuristics.

### Training provider collector

- scrapes training partner promotion pages (Global Knowledge, Ascendient, etc.),
- supports per-vendor extractors and generic card/heading fallbacks.

### HTTP policy

All HTTP traffic goes through a polite request layer that checks `robots.txt`, enforces crawl delays, and uses an identifying user-agent. This keeps the service aligned with site policies while still allowing broad ingestion.

---

## AI service

The AI analyzer uses a provider chain anchored around Groq and Gemini.

### Provider chain

- Groq models are tried first — each post is routed 50/50 across `openai/gpt-oss-20b` and `openai/gpt-oss-120b`,
- low-confidence primary results are re-analyzed by the qwen reasoning model (`qwen/qwen3.6-27b`),
- the first successful response wins,
- Gemini is used as the final fallback,
- retries are applied for rate-limit errors,
- a global concurrency limit bounds the number of simultaneous requests.

The prompt instructs the model to set `is_voucher=true` for any promotional intent, even partial signals. Markdown fences in the model response are stripped before parsing.

### Rate limiting

Per-model rate limiting tracks requests and token budgets. The system also uses a global concurrency semaphore so large batches do not overrun memory or downstream limits.

---

## Data model summary

### sources

| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| name | string UNIQUE | |
| type | enum | REDDIT, RSS, BLOG, EVENT, FORUM, WEBSITE, API, PEARSONVUE, TRAINING_PROVIDER |
| base_url | string | |
| enabled | boolean | false skips the scheduler |
| priority | integer | higher processed first within tier |
| priority_tier | char(1) | A/B/C/D |
| config | JSONB | feed URL, selectors, intervals, etc. |
| next_due_at | timestamptz | scheduler target time |
| backoff_until | timestamptz | set on failure |
| consecutive_failures | integer | drives backoff |
| avg_runtime_ms | integer | rolling average |
| error_count | integer | cumulative |
| last_checked_utc | timestamptz | |

### posts

| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| source_id | integer FK | |
| external_id | string | identity hash for the URL |
| url | string | |
| title | string | |
| content | text | |
| summary | text | optional short description |
| author | string | |
| published_at | timestamptz | |
| status | enum | NEW / FILTERED / QUEUED / PROCESSING / PROCESSED / NOTIFIED / FAILED |
| score | integer | keyword score |
| raw_data | JSONB | original collection payload |
| vendor | string (nullable) | resolved from vendor_mappings table |
| ai_result | JSONB | full extracted-event payload |
| content_hash | string(40) | hash of title/content/date |
| event_id | integer FK | canonical event link |
| is_notified | boolean | true after email send |

### events

| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| vendor | string | |
| promotion_name | string | |
| promotion_type | string | |
| certifications | JSONB | list of strings |
| voucher_code | string | |
| discount | string | |
| registration_url | string | |
| start_date / end_date | timestamptz | |
| regions | JSONB | list of strings |
| status | enum | ACTIVE / EXPIRED / ARCHIVED |
| merge_log | JSONB | append-only audit trail |

### keywords

| Column | Type |
|---|---|
| keyword | string UNIQUE |
| score | integer |
| enabled | boolean |

### pipeline_lock

| Column | Type |
|---|---|
| name | string PK |
| holder | string |
| acquired_at | timestamptz |
| expires_at | timestamptz |

### vendor_mappings

Lookup table mapping source URLs and source names to canonical vendor names.
URL patterns are checked first (startswith match against post URL), then source name patterns (substring match on source name).

| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| url_pattern | string (nullable, unique) | base URL prefix for startswith matching |
| source_name_pattern | string (nullable, unique) | lowercase substring pattern for source name |
| vendor | string (not null) | canonical vendor name (e.g. "aws", "microsoft") |

### notification_outbox

Transactional outbox for voucher alert emails. Delivery intent is persisted in the same transaction as the pipeline; a background sweep retries PENDING rows until SENT using a stable `idempotency_key` so replays cannot duplicate emails.

| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| post_id | integer FK | linked post |
| idempotency_key | string UNIQUE | stable per (post, content) |
| status | enum | PENDING / SENT / FAILED |
| attempts | integer | delivery attempt counter |
| last_error | string | last failure reason |
| last_attempt_at / sent_at | timestamptz | delivery timing |
| subject / html_body / text_body | string/text | rendered email snapshot |

### voucher_posts view

This read-only view exposes AI-confirmed vouchers for the alerts API. It flattens the AI JSON payload into columns suitable for simple list endpoints. The `vendor` column now comes directly from `posts.vendor` (resolved from `vendor_mappings`) rather than the AI guess in `ai_result->>'vendor'`.

---

## REST API

The API is intentionally minimal and read-only, with a single rate-limited health endpoint:

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Returns service status and checks DB reachability via `SELECT count(*) FROM sources` |

---

## Email notifications

The notification layer uses Resend and sends both HTML and plain-text emails containing voucher details such as vendor, promotion name, certification list, discount, voucher code, registration URL, and dates. Delivery is staged through a transactional outbox (`notification_outbox`) in the same transaction as the pipeline run, then delivered by the scheduler with a stable idempotency key so retries can never duplicate an email. The post is marked `is_notified` only after the provider confirms acceptance. The same voucher payload is also POSTed to an optional bot server webhook (`NOTIFICATION_BOT_SERVER_URL`).

---

## Database access

The project uses async SQLAlchemy with `asyncpg`. The connection pool is intentionally small and uses `pool_pre_ping` to avoid stale connections. The ORM uses SQLAlchemy 2.0-style `Mapped` and `mapped_column` declarations.

---

## Schema migrations

Alembic manages the schema. The project uses frozen migration revisions and a production rule that prevents the app from applying DDL in production. In non-production mode startup can create tables and seed initial data; in production mode the app assumes the schema already exists and uses a DML-only role.

---

## Configuration reference

All settings are loaded from `.env` through `pydantic-settings`.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | Asyncpg connection string |
| `IS_PROD` | `false` | Skip DB init/bootstrap on startup |
| `IS_TEST` | `false` | Seed a localhost test source for end-to-end pipeline testing |
| `LOG_LEVEL` | `INFO` | Logging level |
| `RESEND_API_KEY` | — | Email sending |
| `EMAIL_FROM` | `VoucherBot <onboarding@resend.dev>` | Sender address |
| `EMAIL_ID` | — | Recipient address for alerts |
| `EMAIL_REPLY_TO` | — | Optional per-email Reply-To |
| `EMAIL_MIN_INTERVAL_SECONDS` | `5.0` | Throttle between sends |
| `NOTIFICATION_BOT_SERVER_URL` | — | Webhook endpoint that receives the same voucher alert data |
| `WEBHOOK_SECRET` | — | Secret sent in the `Authorization` header of the webhook POST |
| `REDDIT_CLIENT_ID` | — | Reddit API credentials |
| `REDDIT_CLIENT_SECRET` | — | Reddit API credentials |
| `REDDIT_USER_AGENT` | — | Reddit API credentials |
| `REDDIT_INGESTION_ENABLED` | `false` | Allow the Reddit OAuth API (false → RSS-only collection) |
| `REDDIT_FETCH_LIMIT` | `25` | Posts per subreddit fetch |
| `GROQ_API_KEY` | — | Primary AI provider |
| `GROQ_REQUESTS_PER_MINUTE` | `30` | RPM cap |
| `GROQ_MAX_COMPLETION_TOKENS` | `1024` | Max tokens per response |
| `GROQ_MAX_INPUT_CHARS` | `12000` | Input truncation limit |
| `GEMINI_API_KEY` | — | Fallback AI provider |
| `SCRAPER_RESPECT_ROBOTS` | `true` | Obey robots.txt |
| `SCRAPER_MIN_DELAY_SECONDS` | `2.0` | Minimum per-host crawl delay |
| `SCRAPER_USER_AGENT` | — | Override default UA string |
| `HEALTH_RATE_LIMIT_PER_MINUTE` | `60` | /health requests per IP per minute (0 disables) |
| `TICK_LEASE_TTL_SECONDS` | `21600` | Pipeline lease TTL |
| `SOURCE_BACKOFF_BASE_MINUTES` | `5` | Backoff base for failures |
| `SOURCE_BACKOFF_MAX_MINUTES` | `360` | Backoff ceiling |
| `CONTENT_RETENTION_DAYS` | `7` | Posts older than this are content-purged on each sweep |

---

## Deployment

### Local (Docker)

```bash
cp .env.example .env
docker compose up --build
# API at http://localhost:8000
```

With `IS_PROD=false`, startup runs DB initialization and bootstrap. With `IS_PROD=true`, the app assumes schema and seed data already exist.

### Production (Render)

The repository includes [render.yaml](../../render.yaml) for Render deployment. The main startup command is the FastAPI app served through Uvicorn.

### Source catalog management

Sources are seeded from [voucherbot/database/bootstrap.py](../../voucherbot/database/bootstrap.py). The catalog is config-driven, so adding a new feed or page typically requires only a new entry in the source definitions rather than a schema migration.

Smoke-test all feeds/pages:

```bash
python scripts/verify_sources.py
```

---

## Key design decisions

- Single-process, sequential scheduling keeps CPU usage flat and avoids distributed coordination complexity.
- DB-driven scheduler state means the scheduler can restart safely and re-evaluate all due sources.
- Two-hash deduplication balances stable identity with content-change detection.
- Canonical event deduplication ensures posts remain distinct while multiple sources can still attach to one promotion.
- The AI provider chain isolates the pipeline from a single vendor and allows Groq and Gemini to act as interchangeable backends.
- robots.txt compliance is built into the HTTP layer so the project behaves politely while collecting data.
