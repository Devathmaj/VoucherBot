# Contributing to VoucherBot

Thank you for your interest in contributing to VoucherBot. This document outlines the guidelines, policies, and technical expectations for anyone modifying or extending this codebase. Please read it in full before opening a pull request.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ethical & Legal Obligations](#ethical--legal-obligations)
  - [robots.txt Compliance](#robotstxt-compliance)
  - [Rate Limiting](#rate-limiting)
  - [Reddit API Policy](#reddit-api-policy)
  - [Reddit Responsible Builder Policy](#reddit-responsible-builder-policy)
- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Development Workflow](#development-workflow)
- [Adding or Modifying Sources](#adding-or-modifying-sources)
- [Working with the Reddit Integration](#working-with-the-reddit-integration)
- [Working with the HTTP Policy Layer](#working-with-the-http-policy-layer)
- [Working with the AI Layer](#working-with-the-ai-layer)
- [Database & Migrations](#database--migrations)
- [Testing](#testing)
- [Pull Request Guidelines](#pull-request-guidelines)

---

## Code of Conduct

Be respectful, collaborative, and constructive. Contributions that deliberately circumvent access controls, abuse third-party services, or violate the policies described below will be rejected and may result in the contributor being blocked from the project.

---

## Ethical & Legal Obligations

VoucherBot collects data from RSS feeds, vendor websites, and Reddit. This creates a real obligation to the services it depends on. **Any contribution that weakens these protections will not be merged.**

### robots.txt Compliance

All HTTP traffic in VoucherBot flows through the policy layer in `voucherbot/providers/http_policy.py`. This layer:

- Fetches and caches each host's `robots.txt` before making any content request.
- Refuses to fetch URLs that the crawl rules disallow for the configured user-agent.
- Enforces the `Crawl-delay` directive when present.

**When contributing:**

- Never bypass or disable the `robots.txt` check. The `SCRAPER_RESPECT_ROBOTS` setting exists only for controlled local testing — it must never default to `false` and must never be disabled in code paths that could reach production.
- If you add a new collector or a new HTTP helper, route all outgoing requests through the existing policy layer. Do not use `httpx` or `aiohttp` directly outside of it.
- If a site's `robots.txt` disallows the content you want to collect, do not add that site as a source. Find an alternative (e.g. an official RSS feed or API endpoint) or raise a discussion first.

### Rate Limiting

VoucherBot is deliberately conservative about request frequency. The scheduler enforces per-source poll intervals, the HTTP policy layer enforces per-host crawl delays, and the AI service enforces per-model token and request budgets.

**When contributing:**

- Do not reduce `SCRAPER_MIN_DELAY_SECONDS` below `2.0` in any default or example configuration.
- Do not introduce concurrent source polling without a corresponding per-host concurrency guard.
- Do not shorten the priority-tier poll intervals (A: 15 min, B: 60 min, C: 240 min, D: 720 min) without a specific, justified reason discussed in the PR.
- Do not add retry logic that retries immediately on a `429` or `503`. Always use exponential backoff, and defer to the existing backoff mechanism in the dispatcher where possible.
- When adding AI provider calls, respect the existing `GROQ_REQUESTS_PER_MINUTE` cap and the global concurrency semaphore. Do not add parallel AI calls outside the existing `analyzer.py` abstraction.

### Reddit API Policy

VoucherBot uses the Reddit API via OAuth credentials (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`). Reddit's API access is governed by their [Developer Terms](https://www.redditinc.com/policies/developer-terms) and [Data API Terms](https://www.reddit.com/wiki/api/).

**When contributing:**

- Always send a descriptive, identifying `User-Agent` string in the format Reddit requires: `platform:app_id:version (by /u/username)`. Never use a generic or blank user-agent for Reddit requests.
- Never exceed Reddit's stated rate limits (currently 100 requests per minute for OAuth clients). The `reddit/client.py` module is the single place to enforce this — do not make Reddit API calls from outside it.
- The RSS fallback path (public `.json` or RSS endpoints) must still identify itself with the same user-agent and must still respect crawl delays.
- Do not store or expose raw Reddit post content beyond what is necessary for voucher detection. Strip or omit personal data (usernames, user-submitted contact info) from stored records where feasible.
- Do not use the Reddit API or Reddit data to build features unrelated to voucher detection.

### Reddit Responsible Builder Policy

VoucherBot interacts with Reddit communities and their members' posts. All contributors must read and comply with Reddit's **[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)** before modifying any Reddit-related code.

Key obligations that apply directly to this project:

- **Transparency:** The bot's `User-Agent` must clearly identify the application and its operator. Do not impersonate a browser or a human user.
- **Minimal data collection:** Only collect what is necessary to detect voucher promotions. Do not scrape user profiles, comment histories, or subreddit metadata beyond what the pipeline requires.
- **No manipulation:** Do not add features that vote, comment, post, or otherwise interact with Reddit on behalf of any user. VoucherBot is read-only with respect to Reddit.
- **Respect community rules:** Before adding a new subreddit as a source, confirm that scraping or automated reading is not explicitly prohibited by that subreddit's rules or moderators.
- **Honor disabling:** The `REDDIT_INGESTION_ENABLED` flag disables the Reddit OAuth API. When it is `false`, the collector must fall back to the public RSS feeds and never call the Reddit API. Any new Reddit API feature must respect this flag and exit cleanly when it is `false`.

---

## Getting Started

**Prerequisites:** Python 3.11+, Docker, and `psql` (for running migrations locally).

```bash
# 1. Clone and install dependencies
git clone <repo-url>
cd voucherbot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure environment
cp .env.example .env
# Fill in DATABASE_URL, GROQ_API_KEY, and optionally REDDIT_* and GEMINI_API_KEY

# 3. Start the database and app
docker compose up --build

# 4. Verify sources
python scripts/verify_sources.py
```

The API will be available at `http://localhost:8000`. With `IS_PROD=false` (the default), the database schema and seed data are created automatically on startup.

---

## Project Structure

```
voucherbot/
├── main.py                        # FastAPI app and lifespan
├── config/settings.py             # All configuration via pydantic-settings
├── core/                          # Exceptions and logging
├── database/                      # Engine, migrations, and bootstrap
├── models/                        # SQLAlchemy ORM models
├── providers/
│   ├── base.py                    # BaseCollector contract
│   ├── http_policy.py             # robots.txt + crawl-delay enforcement ⚠️
│   ├── rss/collector.py
│   ├── reddit/
│   │   ├── client.py              # Reddit API client ⚠️
│   │   └── collector.py
│   ├── website/collector.py
│   ├── pearsonvue/collector.py    # Pearson VUE vendor page scraper
│   └── training_provider/collector.py  # Training partner page scraper
├── services/
│   ├── scheduler.py               # Asyncio scheduler loop
│   ├── dispatcher.py              # Lease + source lifecycle
│   ├── event_consolidation.py     # Periodic merge of duplicate events
│   ├── retention.py               # Null out stale post content
│   ├── ingestion/                 # Pipeline, dedup, event matching
│   ├── ai/                        # Groq + Gemini provider chain, AI event matcher
│   ├── email/                     # Resend notifications (transactional outbox)
│   └── bot_notification/          # Voucher webhook to a bot server
└── api/
    ├── rate_limit.py              # Health-endpoint rate limiter
    └── routers/health.py          # Read-only health endpoint
```

Files marked ⚠️ contain policy-sensitive logic. Changes to these files require extra care and a detailed explanation in the PR.

---

## Development Workflow

1. **Branch** from `main` with a descriptive name: `feat/add-microsoft-feed`, `fix/reddit-ratelimit`.
2. **Keep changes focused.** One concern per PR. A PR that adds a new source and refactors the scheduler is two PRs.
3. **Run the linter and formatter** before committing:
   ```bash
   ruff check . && ruff format .
   ```
4. **Run tests** (see [Testing](#testing)).
5. **Update documentation** if you change configuration variables, the data model, or the ingestion pipeline stages.

---

## Adding or Modifying Sources

New sources are defined in `voucherbot/database/bootstrap.py`. A source entry requires at minimum:

- `name` — unique, descriptive
- `type` — one of `REDDIT`, `RSS`, `BLOG`, `EVENT`, `FORUM`, `WEBSITE`, `API`, `PEARSONVUE`, `TRAINING_PROVIDER`
- `base_url`
- `priority_tier` — A, B, C, or D (see the scheduler table above)
- `config` — a JSONB object with `feed_url` (RSS), `article_selector` + `content_selector` (Website), `subreddit` (Reddit), or the vendor page type for Pearson VUE / training provider sources

**Before adding a new web source:**

1. Check the site's `robots.txt` manually and confirm it does not disallow automated access.
2. Choose a priority tier that matches the expected update frequency — do not assign tier A unless the source genuinely needs 15-minute polling.
3. Run `python scripts/verify_sources.py` and confirm the new source resolves without errors.

**Before adding a new Reddit source:**

1. Read the subreddit's sidebar rules and confirm automated reading is not prohibited.
2. Confirm the subreddit content is likely to contain voucher promotions (not just adjacent topics).
3. Set `priority_tier` to A or B only — Reddit sources should not be polled at C or D intervals since the RSS fallback path is already conservative.

---

## Working with the Reddit Integration

The Reddit integration lives in `voucherbot/providers/reddit/`. All Reddit API calls must go through `client.py`. The collector in `collector.py` calls the client and normalises results into the shared post format.

**Rules for this module:**

- The collector must check `REDDIT_INGESTION_ENABLED` before making any Reddit OAuth call. When it is `false` — or the OAuth credentials are missing — it must collect via the public RSS feeds and must not raise.
- Rate limiting logic lives in the client, not the collector. Do not add sleep calls or retry loops in the collector.
- The RSS path is used when the OAuth API is disabled via `REDDIT_INGESTION_ENABLED=false`, when OAuth credentials are missing, or when OAuth returns a retriable error — not as a way to avoid rate limits.
- Do not log full post bodies or user metadata at `INFO` level or above. Use `DEBUG` for raw API payloads.
- The `REDDIT_FETCH_LIMIT` setting caps the number of posts fetched per subreddit per tick. Do not bypass this cap in the collector.

---

## Working with the HTTP Policy Layer

`voucherbot/providers/http_policy.py` is the single chokepoint for all outbound HTTP requests (except Reddit OAuth, which has its own client). This is intentional.

**When extending the HTTP layer:**

- If you add a new request method or helper, it must call the `robots_allowed(url)` check and the `crawl_delay_for(host)` enforcement before yielding a response.
- Cache `robots.txt` per host. Do not re-fetch it on every request.
- Use the `SCRAPER_USER_AGENT` setting for the `User-Agent` header on all requests. Do not hardcode a UA string.
- If a host returns a `429`, back off for at least the value of `Retry-After` if present, or a minimum of 60 seconds. Propagate the backoff signal to the dispatcher so the source is not retried immediately.

---

## Working with the AI Layer

The AI analyzer lives in `voucherbot/services/ai/`. It uses a Groq-first, Gemini-fallback provider chain.

**When contributing to this layer:**

- Do not increase `GROQ_MAX_COMPLETION_TOKENS` or `GROQ_MAX_INPUT_CHARS` defaults without measuring the impact on cost and latency.
- The global concurrency semaphore must wrap all provider calls. Do not await provider responses outside of the semaphore context.
- Prompt changes must be tested against a representative sample of real posts before merging. Include a brief before/after comparison in the PR description.
- Structured output is extracted from the model response by stripping Markdown fences and parsing JSON. If you change the output schema in `schema.py`, update the prompt accordingly and verify the parser handles partial responses gracefully.

---

## Database & Migrations

VoucherBot uses Alembic for schema migrations with frozen revisions.

- **Never run DDL in production** (`IS_PROD=true`). The app uses a DML-only role in production and assumes the schema already exists.
- **Generate a new migration** for any model change:
  ```bash
  alembic revision --autogenerate -m "describe the change"
  ```
- Review the auto-generated migration before committing — autogenerate is not always correct for JSONB columns or custom types.
- **Do not squash or rebase migration history.** Each revision must be independently replayable.
- If you add a column, provide a sensible server-side default so the migration does not break existing rows.

---

## Testing

```bash
pytest
```

When adding a new feature:

- Add a unit test for any new business logic (keyword scoring, deduplication, event matching).
- Add an integration test for any new collector using a fixture or a recorded HTTP response — do not make live network calls in tests.
- Mock the Reddit API client in all tests. Never use real Reddit credentials in the test suite.
- Mock Groq and Gemini responses. Never make live AI API calls in tests.

---

## Pull Request Guidelines

- **Title:** Use a short imperative sentence: `Add Microsoft Learn RSS source`, `Fix Reddit rate-limit handling`.
- **Description:** Explain _why_, not just _what_. If the PR touches the HTTP policy layer, the Reddit client, or the AI provider chain, include a brief explanation of how the ethical obligations above are preserved.
- **Policy checklist:** For any PR that touches `http_policy.py`, `reddit/client.py`, `reddit/collector.py`, or `scheduler.py`, confirm the following in the PR description:
  - [ ] `robots.txt` compliance is preserved.
  - [ ] No default crawl delays have been reduced.
  - [ ] Reddit rate limits and the Responsible Builder Policy are respected.
  - [ ] `REDDIT_INGESTION_ENABLED=false` still collects Reddit via RSS and makes no OAuth calls.
- **Size:** Keep PRs reviewable. Prefer several small PRs over one large one.
- **Migrations:** If the PR includes a migration, note whether it is safe to apply to a live database without downtime.

---

## Questions

Open a GitHub Discussion or issue before starting significant work. It is better to align on approach early than to invest time in a direction that conflicts with the project's design principles.