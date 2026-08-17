# VoucherBot — Technical Architecture

This document is the implementation-focused reference for VoucherBot. For the higher-level product context, see [project-info.md](project-info.md). For the full settings reference, see [configuration.md](configuration.md).

## Overview

VoucherBot is an async Python service that monitors certification-related sources, filters likely voucher content, and stores or surfaces promotion candidates through a FastAPI API and PostgreSQL-backed models. The current implementation is a single-process application with one background scheduler loop and one PostgreSQL lease for coordination.

## Runtime shape

The application entry point is [voucherbot/main.py](../../voucherbot/main.py). During startup it:

1. configures logging,
2. applies Alembic migrations and seeds source/keyword data when `IS_PROD=false`,
3. resets all sources to be due again, and
4. starts the scheduler task.

The runtime therefore has three cooperating parts:

- FastAPI routers for health and read-only data access
- a background scheduler loop that picks due sources
- a processing pipeline that ingests, filters, deduplicates, analyzes, and stores data

## Application flow

The high-level execution path is:

1. the scheduler picks one due source,
2. the dispatcher acquires a PostgreSQL lease and runs the pipeline for that source,
3. a collector resolves the source type and fetches posts,
4. keyword scoring filters obvious noise,
5. deduplication decides whether the post is new or updated,
6. AI extraction produces structured promotion data,
7. event matching attaches the post to a canonical event or creates a new one,
8. email notifications are sent for newly confirmed voucher opportunities.

## Core modules

The main implementation areas are:

- [voucherbot/main.py](../../voucherbot/main.py) — FastAPI app and lifespan startup/shutdown
- [voucherbot/config/settings.py](../../voucherbot/config/settings.py) — environment-driven settings and event-matching weights
- [voucherbot/database/bootstrap.py](../../voucherbot/database/bootstrap.py) — seeded source catalog and keyword catalog
- [voucherbot/services/scheduler.py](../../voucherbot/services/scheduler.py) — scheduler loop and sleep logic
- [voucherbot/services/dispatcher.py](../../voucherbot/services/dispatcher.py) — lease handling, due-source selection, success/failure state updates
- [voucherbot/services/ingestion/pipeline.py](../../voucherbot/services/ingestion/pipeline.py) — end-to-end per-source pipeline
- [voucherbot/services/ingestion/event_matcher.py](../../voucherbot/services/ingestion/event_matcher.py) — canonical event matching and field merging
- [voucherbot/services/event_consolidation.py](../../voucherbot/services/event_consolidation.py) — periodic merge of duplicate canonical events
- [voucherbot/services/ai/analyzer.py](../../voucherbot/services/ai/analyzer.py) — AI extraction provider chain and batching
- [voucherbot/services/ai/event_matcher_ai.py](../../voucherbot/services/ai/event_matcher_ai.py) — qwen-based same-promotion judge
- [voucherbot/api/routers/health.py](../../voucherbot/api/routers/health.py) — read-only health endpoint with rate limiting

## Scheduler and dispatcher

The scheduler is implemented in [voucherbot/services/scheduler.py](../../voucherbot/services/scheduler.py). It runs as a single `asyncio.Task` and loops over two phases:

- `_run_sweep()` processes due sources one at a time until none remain due
- `_seconds_until_next_due()` calculates the next sleep interval

The dispatcher in [voucherbot/services/dispatcher.py](../../voucherbot/services/dispatcher.py) is responsible for the coordination logic:

- it acquires a row-level lease from the `pipeline_lock` table,
- selects one eligible source using the same due-source rules as the scheduler,
- updates scheduling state after success or failure,
- applies exponential backoff for recoverable failures,
- disables a source immediately for unrecoverable HTTP failures such as 404/403/410.

A source is considered eligible when it is enabled, not in backoff, and either not due in the future or already due. Reddit sources are skipped when `reddit_ingestion_enabled` is false.

## Ingestion pipeline

The per-source pipeline is implemented in [voucherbot/services/ingestion/pipeline.py](../../voucherbot/services/ingestion/pipeline.py) and runs in this order:

### 1. Collect

A collector is resolved from the source type and config:

- `SourceType.REDDIT` → Reddit collector
- a `feed_url` in config → RSS collector
- an `article_selector` in config → website collector

The collector returns a list of normalized posts.

### 2. Keyword filtering

The pipeline loads enabled keywords from the database and scores the post title and content. Posts below the threshold of `1` are dropped. Sources with `note_selector` in config bypass the keyword filter and are treated as curated voucher pages.

### 3. Deduplication and upsert

Deduplication uses two hashes:

- `identity_hash` derived from a normalized URL, which gives a stable identity for a page
- `content_hash` derived from title/content/date, which detects updated content

The upsert logic inserts new posts, updates changed content, and skips unchanged items. This is implemented with PostgreSQL upsert semantics to avoid duplicate rows.

### 4. AI extraction

New or updated posts are sent to [voucherbot/services/ai/analyzer.py](../../voucherbot/services/ai/analyzer.py). The current implementation tries Groq models first and uses Gemini as a fallback. The analyzer returns an `ExtractedEvent` object with structured promotion fields and an `is_voucher` flag.

### 5. Event matching

The matcher in [voucherbot/services/ingestion/event_matcher.py](../../voucherbot/services/ingestion/event_matcher.py) decides whether an extracted promotion is the same real-world promotion as an existing active event.

By default it runs the incoming promotion through the qwen reasoning model ([voucherbot/services/ai/event_matcher_ai.py](../../voucherbot/services/ai/event_matcher_ai.py)), comparing it against the candidate events that the deterministic weighted score flags as possible matches (score >= `possible_match_threshold`, capped by `ai_candidate_limit`) and letting the model decide whether each is the same promotion:

- `is_same_promotion` and `confidence >= ai_auto_merge_confidence` → `AUTO_MERGED`
- `is_same_promotion` and `confidence >= ai_possible_match_confidence` → `POSSIBLE_MATCH`
- otherwise → `NEW`

When the model is unavailable, no `GROQ_API_KEY` is configured, or no candidates exist, the matcher falls back to the legacy weighted score over registration URL, voucher code, promotion-name similarity, vendor, discount, promotion type, certification overlap, and date overlap. The model's `reason` is recorded in `merge_log` for auditability.

The result is one of `AUTO_MERGED`, `POSSIBLE_MATCH`, or `NEW`, and the matcher may merge fields into the canonical event while appending to `merge_log`.

### 6. Email notification

If the AI extraction yields a voucher candidate and the event decision is not `AUTO_MERGED`, delivery intent is staged into the transactional notification outbox in the same commit as the pipeline. Delivery is attempted immediately through Resend with a stable idempotency key; failures stay `PENDING` and are retried by the scheduler. The post is marked `is_notified` only after a send succeeds. The same payload is POSTed to the optional bot server webhook alongside the email (best-effort — a webhook failure never fails the pipeline).

## Event consolidation

Two posts describing the same promotion can become separate events when their sources were processed at different times — the ingestion-time matcher only sees candidates that already exist at that moment. The consolidation sweep in [voucherbot/services/event_consolidation.py](../../voucherbot/services/event_consolidation.py) fixes this retroactively. It runs after every scheduler sweep (throttled by `settings.consolidation.interval_minutes`) and is cross-instance serialised with a Postgres advisory transaction lock.

1. **Discover** — active events are grouped into candidate pairs sharing a cheap identity signal: normalised registration URL, voucher code (case-normalised), or vendor. Pairs are deduplicated by the canonical `(min_id, max_id)` key and capped by `max_pairs_per_sweep`; buckets are sampled to bound quadratic work.
2. **Gate** — each pair is scored with the same deterministic weighted score used at ingestion; only pairs at or above `possible_match_threshold` proceed.
3. **Confirm** — when a Groq key is configured, qwen is asked whether the pair is the same real-world promotion via `compare_events` (the same judge used by the matcher). A `same` decision at `confidence >= ai_possible_match_confidence` merges; otherwise the pair is kept separate. A model outage falls back to the deterministic score at or above `deterministic_auto_merge_threshold`.
4. **Merge** — the pair's survivor is the event with more posts (ties keep the older event). The absorbed event's fields are folded in through the same `_merge_fields` source-priority machinery, its posts are re-pointed to the survivor, both `merge_log` entries are appended, and the absorbed event is set to `ARCHIVED`.

An absorbed event is never folded into a second target within one sweep, and the whole job never raises — failures are logged so the scheduler loop stays healthy.

## Data model summary

The core SQLAlchemy models are:

- [voucherbot/models/source.py](../../voucherbot/models/source.py) — `Source` and `SourceType`
- [voucherbot/models/post.py](../../voucherbot/models/post.py) — `Post`, `PostStatus`, `VoucherPost`
- [voucherbot/models/event.py](../../voucherbot/models/event.py) — `Event`, `EventStatus`, `MatchConfidence`
- [voucherbot/models/keyword.py](../../voucherbot/models/keyword.py) — keyword scoring rows used by the pipeline
- [voucherbot/models/vendor_mapping.py](../../voucherbot/models/vendor_mapping.py) — URL/source-name pattern → vendor lookup
- [voucherbot/models/notification.py](../../voucherbot/models/notification.py) — notification outbox for voucher alert emails
- [voucherbot/models/pipeline_lock.py](../../voucherbot/models/pipeline_lock.py) — pipeline lease row used by the dispatcher

The important relationships are:

- each post belongs to a source,
- each post may be linked to one canonical event,
- many posts can point to the same event, but posts themselves are never merged.

## API surface

The FastAPI app exposes a single read-only endpoint and does not implement authentication:

- `GET /health` — liveness + DB reachability probe (rate-limited per IP)

## Configuration and deployment

Configuration is loaded from `.env` through [voucherbot/config/settings.py](../../voucherbot/config/settings.py). The important runtime settings include database connection details, AI provider credentials, Reddit credentials, Resend credentials, scraper policy values, and scheduler backoff/lease settings.

The repository includes:

- [docker-compose.yml](../../docker-compose.yml) and [Dockerfile](../../Dockerfile) for local container-based runs
- [render.yaml](../../render.yaml) for deployment to Render
- [alembic.ini](../../alembic.ini) and the [migrations](../../migrations) directory for schema changes

In non-production mode, startup runs DB initialization and bootstrap. In production mode, the app assumes schema and bootstrap data already exist and uses a DML-only role.
