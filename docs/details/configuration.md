# VoucherBot Configuration

This document collects the configuration values used by VoucherBot and maps them to the settings model in [voucherbot/config/settings.py](../../voucherbot/config/settings.py).

## Environment-based settings

These values are loaded from `.env` through Pydantic settings.

### Core runtime

| Variable | Default | Purpose |
|---|---:|---|
| `DATABASE_URL` | required | Async SQLAlchemy connection string for PostgreSQL |
| `IS_PROD` | `false` | When `true`, startup skips schema/bootstrap work and assumes the database is already prepared |
| `LOG_LEVEL` | `INFO` | Logging level used by the application |

### Email

| Variable | Default | Purpose |
|---|---:|---|
| `RESEND_API_KEY` | `None` | API key for Resend-based email delivery |
| `EMAIL_FROM` | `VoucherBot <onboarding@resend.dev>` | Sender address used for alerts |
| `EMAIL_ID` | `None` | Recipient address for voucher notifications |
| `EMAIL_MIN_INTERVAL_SECONDS` | `5.0` | Minimum delay between email sends |

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

### Event matching weights

These are defined in the `EventMatcherConfig` model:

| Setting | Default | Purpose |
|---|---:|---|
| `weight_registration_url` | `50` | Score weight for exact registration URL matches |
| `weight_voucher_code` | `40` | Score weight for exact voucher-code matches |
| `weight_promotion_name` | `20` | Score weight for promotion-name similarity |
| `weight_vendor` | `15` | Score weight for vendor matches |
| `weight_certifications` | `15` | Score weight for certification overlap |
| `weight_date_overlap` | `10` | Score weight for date-range overlap |
| `auto_merge_threshold` | `75` | Threshold above which an event is auto-merged |
| `possible_match_threshold` | `60` | Threshold above which a possible match is flagged |
| `name_similarity_threshold` | `0.60` | Similarity cutoff for promotion-name credit |

### Source priority ordering

The `SOURCE_PRIORITY` list defines how source types are ranked when merging event fields:

1. `WEBSITE`
2. `EVENT`
3. `BLOG`
4. `RSS`
5. `FORUM`
6. `REDDIT`
7. `API`

Higher-priority sources overwrite lower-priority values when a new post updates an existing event.

## Notes for local setup

- Copy `.env.example` to `.env` before running the app locally.
- The project uses these settings at startup, during scheduler execution, and during AI extraction.
- Missing database or provider credentials will prevent the relevant runtime features from working correctly.

## Related documentation

- [architecture.md](architecture.md) for how these settings affect runtime behavior
- [project-info.md](project-info.md) for the broader product context
