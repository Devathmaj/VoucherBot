# VoucherBot Configuration

This document collects the configuration values used by VoucherBot and maps them to the settings model in [voucherbot/config/settings.py](../../voucherbot/config/settings.py).

## Environment-based settings

These values are loaded from `.env` through Pydantic settings.

### Core runtime

| Variable | Default | Purpose |
|---|---:|---|
| `DATABASE_URL` | required | Async SQLAlchemy connection string for PostgreSQL |
| `IS_PROD` | `false` | When `true`, startup skips schema/bootstrap work and assumes the database is already prepared |
| `IS_TEST` | `false` | When `true`, seeds a `website:local_test` source pointing at `http://localhost:35926/` for end-to-end pipeline testing |
| `LOG_LEVEL` | `INFO` | Logging level used by the application |

### Email

| Variable | Default | Purpose |
|---|---:|---|
| `RESEND_API_KEY` | `None` | API key for Resend-based email delivery |
| `EMAIL_FROM` | `VoucherBot <onboarding@resend.dev>` | Sender address used for alerts |
| `EMAIL_ID` | `None` | Recipient address for voucher notifications |
| `EMAIL_REPLY_TO` | `None` | Optional per-email Reply-To; when unset Resend falls back to the From address |
| `EMAIL_MIN_INTERVAL_SECONDS` | `5.0` | Minimum delay between email sends |

### API rate limiting

| Variable | Default | Purpose |
|---|---:|---|
| `HEALTH_RATE_LIMIT_PER_MINUTE` | `60` | Max `/health` requests per IP per minute; `0` disables the limit |
| `RATE_LIMIT_TRUSTED_PROXIES` | `[]` | Comma-separated proxy IPs whose `X-Forwarded-For` values are trusted for rate limiting |

### Bot webhook notification

| Variable | Default | Purpose |
|---|---:|---|
| `NOTIFICATION_BOT_SERVER_URL` | `None` | Endpoint that receives a POST with the same voucher alert data as the email, for a remote bot server |
| `WEBHOOK_SECRET` | `None` | Secret sent in the `Authorization` header of the webhook POST |

### Reddit ingestion

| Variable | Default | Purpose |
|---|---:|---|
| `REDDIT_CLIENT_ID` | `None` | Reddit API client ID |
| `REDDIT_CLIENT_SECRET` | `None` | Reddit API client secret |
| `REDDIT_USER_AGENT` | `None` | Reddit user agent string |
| `REDDIT_FETCH_INTERVAL_MINUTES` | `3` | Poll cadence used by the Reddit collector |
| `REDDIT_CONCURRENCY_LIMIT` | `5` | Max concurrent Reddit collection work |
| `REDDIT_FETCH_LIMIT` | `25` | Maximum number of posts fetched per Reddit poll |
| `REDDIT_INGESTION_ENABLED` | `false` | Allow the Reddit OAuth API; when `false`, Reddit is collected via RSS feeds only |

### Scraping and HTTP policy

| Variable | Default | Purpose |
|---|---:|---|
| `SCRAPER_USER_AGENT` | `None` | Override for the HTTP user-agent string |
| `SCRAPER_CONTACT_EMAIL` | `None` | Contact email embedded into the default user-agent when present |
| `SCRAPER_RESPECT_ROBOTS` | `true` | Whether requests should obey `robots.txt` rules |
| `SCRAPER_MIN_DELAY_SECONDS` | `2.0` | Minimum crawl delay between requests to the same host |

### Scheduler and backoff

| Variable | Default | Purpose |
|---|---:|---|
| `TICK_LEASE_TTL_SECONDS` | `21600` | Lease TTL for the pipeline lock used to coordinate scheduler instances |
| `TICK_JOB_TIMEOUT_SECONDS` | `None` | Optional timeout for scheduler jobs |
| `SOURCE_BACKOFF_BASE_MINUTES` | `5` | Base delay used for recoverable source failures |
| `SOURCE_BACKOFF_MAX_MINUTES` | `360` | Maximum backoff delay for a source |
| `CONTENT_RETENTION_DAYS` | `7` | Posts older than this are content-purged each scheduler sweep |

### AI providers

| Variable | Default | Purpose |
|---|---:|---|
| `GEMINI_API_KEY` | `None` | API key for Gemini fallback provider |
| `GROQ_API_KEY` | `None` | API key for Groq provider |
| `GROQ_REQUESTS_PER_MINUTE` | `30` | Per-model request rate limit |
| `GROQ_TOKENS_PER_MINUTE` | `None` | Optional override for per-model token limit |
| `GROQ_MAX_COMPLETION_TOKENS` | `1024` | Maximum completion tokens requested from Groq |
| `GROQ_MAX_INPUT_CHARS` | `12000` | Maximum number of input characters sent to the AI provider |

## Non-environment configuration

Some settings are not loaded from `.env` directly. They are defined in code and can be overridden in tests or custom runtime wiring.

### Event matching

These are defined in the `EventMatcherConfig` model:

| Setting | Default | Purpose |
|---|---:|---|
| `use_ai_matcher` | `True` | When enabled, the qwen reasoning model decides whether an incoming promotion matches an existing event |
| `ai_candidate_limit` | `5` | Maximum deterministic-matched candidates submitted to the model per post |
| `ai_auto_merge_confidence` | `0.8` | Model confidence above which a same-promotion decision is an AUTO_MERGED |
| `ai_possible_match_confidence` | `0.5` | Model confidence below which a same-promotion decision is treated as a new event |
| `weight_registration_url` | `50` | Deterministic-fallback score weight for exact registration URL matches |
| `weight_voucher_code` | `40` | Deterministic-fallback score weight for exact voucher-code matches |
| `weight_promotion_name` | `25` | Deterministic-fallback score weight for promotion-name similarity |
| `weight_vendor` | `20` | Deterministic-fallback score weight for vendor matches |
| `weight_discount` | `20` | Deterministic-fallback score weight for discount matches |
| `weight_promotion_type` | `10` | Deterministic-fallback score weight for promotion-type matches |
| `weight_certifications` | `15` | Deterministic-fallback score weight for certification overlap |
| `weight_date_overlap` | `10` | Deterministic-fallback score weight for date-range overlap |
| `auto_merge_threshold` | `70` | Deterministic-fallback threshold above which an event is auto-merged |
| `possible_match_threshold` | `45` | Deterministic-fallback threshold above which a possible match is flagged |
| `name_similarity_threshold` | `0.60` | Deterministic-fallback similarity cutoff for promotion-name credit |
| `candidate_limit` | `100` | Maximum candidate events retrieved for matching |

The deterministic weighted score is only used as a fallback when the qwen model is unavailable, no `GROQ_API_KEY` is configured, or no candidates exist.

### Event consolidation

These are defined in the `EventConsolidationConfig` model and tune the periodic sweep that merges duplicate canonical events ([voucherbot/services/event_consolidation.py](../../voucherbot/services/event_consolidation.py)):

| Setting | Default | Purpose |
|---|---:|---|
| `enabled` | `True` | Master switch for the consolidation sweep |
| `interval_minutes` | `60` | Minimum wall-clock time between sweeps (rate-limits the qwen spend) |
| `max_pairs_per_sweep` | `1000` | Hard cap on candidate pairs examined per sweep |
| `max_ai_calls_per_sweep` | `25` | How many qwen confirmations to allow per sweep |
| `deterministic_auto_merge_threshold` | `70` | Deterministic-score floor for merging when the model is unavailable |

The sweep runs after each scheduler sweep, groups active events by normalised registration URL, voucher code, or vendor, gates pairs with the deterministic weighted score (`possible_match_threshold`), and lets qwen confirm whether each pair is the same real-world promotion before merging and archiving the loser.

### Source priority ordering

The `SOURCE_PRIORITY` list defines how source types are ranked when merging event fields:

1. `WEBSITE`
2. `PEARSONVUE`
3. `TRAINING_PROVIDER`
4. `EVENT`
5. `BLOG`
6. `RSS`
7. `FORUM`
8. `REDDIT`
9. `API`

Higher-priority sources overwrite lower-priority values when a new post updates an existing event.

## Notes for local setup

- Copy `.env.example` to `.env` before running the app locally.
- The project uses these settings at startup, during scheduler execution, and during AI extraction.
- Missing database or provider credentials will prevent the relevant runtime features from working correctly.

## Related documentation

- [architecture.md](architecture.md) for how these settings affect runtime behavior
- [project-info.md](project-info.md) for the broader product context
