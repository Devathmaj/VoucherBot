# 🧪 Testing

This project uses **pytest** for unit and integration testing.

---

## 📋 Table of Contents

1. [Prerequisites](#-prerequisites)
2. [Configure the Environment](#️-configure-the-environment)
3. [Install Development Dependencies](#-install-development-dependencies)
4. [Running the Test Suite](#-running-the-test-suite)
5. [Understanding the Results](#-understanding-the-results)
6. [Test Suite Layout](#-test-suite-layout)
7. [Troubleshooting](#-troubleshooting)

---

## 🧰 Prerequisites

Before running the test suite, create and activate the project's virtual environment.

### Windows (PowerShell)

Create the virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

### Linux / macOS

Create the virtual environment:

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

---

## ⚙️ Configure the Environment

This project requires several environment variables for configuration.

A template is provided as `.env.example`.

1. Copy the template:

   ```bash
   cp .env.example .env
   ```

   **Windows (PowerShell):**

   ```powershell
   Copy-Item .env.example .env
   ```

2. Open `.env` and replace all placeholder values with real configuration values.

   This includes any required database connection strings, email credentials, API keys, and (optionally) Reddit API credentials.

   > **📝 Note:** The test suite runs fully **offline** with mocked dependencies (see [Test Suite Layout](#-test-suite-layout)) — no credentials are required for `pytest`. The values above are still needed to run the application itself (e.g. the startup bootstrap and live pipelines).

---

## 📦 Install Development Dependencies

Install the project together with all development dependencies:

```bash
pip install -e ".[dev]"
```

This installs the project in editable mode and includes development tools such as:

- pytest
- pytest-asyncio
- ruff
- mypy

---

## ▶️ Running the Test Suite

Run all tests:

```bash
pytest
```

or

```bash
python -m pytest
```

For more verbose output:

```bash
pytest -v
```

---

## 📊 Understanding the Results

A typical test run will produce output similar to:

```text
418 passed, 15 skipped, 1 warning in 6.84s
```

### ✅ Passed

Passed tests completed successfully.

### ⏭️ Skipped

Skipped tests are **intentional** and are **not failures**.

Some collector tests only apply to a particular collector type.

For example:

- RSS validation tests skip sources that are implemented as website scrapers.
- Website scraping tests skip sources that are implemented using RSS feeds.

Currently 15 tests skip: 11 RSS-source checks and 4 website-source checks in `test_collectors.py`.

This confirms that the correct collector is configured for each source rather than indicating a problem.

The deduplication suite also includes URL canonicalization checks that verify tracking parameters are removed and host matching is parsed safely rather than relying on substring checks.

### ❌ Failed

A failed test indicates that the implementation does not currently match the expected behaviour.

The suite runs entirely **offline** — no live database, network requests, or third-party APIs (AI providers, email/Resend, Reddit) are touched. Every external dependency is mocked (for example `polite_get`, `AsyncGroq`, `genai.Client`, and `resend.Emails.send`), and database access is simulated with fake async sessions that route on the SQL statement text.

This means no extra credentials or services are required to run the tests, and a failure points to a genuine regression in the code (or a broken test) rather than a missing configuration value.

---

## 🗂️ Test Suite Layout

The suite is organised by module — each file targets one service, provider, or component. All tests are async-friendly (pytest-asyncio) and hermetic.

| Test file (tests) | Module under test | Highlights |
|-------------------|-------------------|------------|
| `test_analyzer.py` (43) | `voucherbot/services/ai/analyzer.py` | JSON extraction parsing (plain/fenced/invalid → safe default), token estimation, Groq/Gemini rate budgets and daily exhaustion, 429 retry handling, model fallback order, qwen low-confidence escalation, `analyze_post_batch` order preservation |
| `test_bootstrap.py` (25) | `voucherbot/database/bootstrap.py` | Reddit tier/cadence rules, invalid-selector warnings, transient-error detection, retry-with-backoff (and no retry on `IntegrityError`), keyword seeding, bootstrap ordering + advisory-lock skip |
| `test_pipeline.py` (29) | `voucherbot/services/ingestion/pipeline.py` | URL normalisation, vendor/collector resolution, fetch-limit resolution, `_process_one_source` state machine (keyword filter → dedup → AI → match → outbox → delivery) |
| `test_pearsonvue_collector.py` (20) | `voucherbot/providers/pearsonvue/collector.py` | Slide/promo/card extraction, card dedup, URL resolution, robots/401/403/429/timeout paths, fetch limits |
| `test_training_provider_collector.py` (18) | `voucherbot/providers/training_provider/collector.py` | GK/Ascendient/generic extractors, nav-link exclusion, description fallbacks, relative-URL resolution, error/limit paths |
| `test_settings.py` (10) | `voucherbot/config/settings.py` | Hermetic pydantic-settings construction: defaults, empty-string→`None` validators, trusted-proxy lists, `EventMatcherConfig`, stable `SOURCE_PRIORITY` order |
| `test_email_sender.py` (8) | `voucherbot/services/email/sender.py` | `send_email` params/reply-to/idempotency key, init state, `send_test_email` skip/send, skip-when-uninitialised |
| `test_init_db.py` (5) | `voucherbot/database/init_db.py` | Source-type enum migration (add-only), `create_all` excluding view models |
| `test_collectors.py` (67) | `voucherbot/providers/{rss,website}.collector` | Feed-URL normalisation, HTML/content-type rejection for RSS, mocked `polite_get` collection, UA identification, per-type skips |
| `test_dedup.py` (21) | `voucherbot/services/ingestion/dedup.py` | URL canonicalisation (tracking params, fragments, scheme), content/identity hashing, batch deduplication |
| `test_dispatcher.py` (22) | `voucherbot/services/dispatcher.py` | Backoff growth/cap, poll-interval resolution, tick lifecycle (busy/idle/ran/failed), due-source selection, unrecoverable-error disables |
| `test_event_matcher.py` (82) | `voucherbot/services/ingestion/event_matcher.py` | Shared-event scenarios (two posts → one event), possible matches, event updates, source-priority field merging |
| `test_event_matcher_ai.py` (14) | `voucherbot/services/ai/event_matcher_ai.py` | `EventMatchDecision` parsing, prompt/serialisation helpers, `compare_candidate`/`compare_events` fallback semantics |
| `test_bot_notification.py` (7) | `voucherbot/services/bot_notification/notifier.py` | Webhook payload builder, `Authorization` header, skip-when-unconfigured, HTTP error handling |
| `test_event_consolidation.py` (24) | `voucherbot/services/event_consolidation.py` | Candidate-pair discovery, deterministic gating, AI/deterministic merge decisions, survivor selection, throttling |
| `test_email_notifications.py` (4) | `voucherbot/services/email/notifications.py` | Safe-URL allow/deny list, voucher email builder |
| `test_http_policy.py` (2) | `voucherbot/providers/http_policy.py` | Robots.txt parsing/caching, politeness delays, per-domain policy state |
| `test_logging.py` (7) | `voucherbot/core/logging.py` | Structlog processor chain, log-level setup |
| `test_main.py` (9) | `voucherbot/main.py` + `api/routers/health.py` | `/health` 200 via dependency-overridden session; rate limiting (boundary, disable-at-0, proxy handling) |
| `test_migrations.py` (4) | `migrations/` | Chain reachable from base to head, numeric `sourcetype` enum values replayable |
| `test_notification_outbox.py` (8) | `voucherbot/services/email/notifications.py` | Idempotency keys, outbox staging, delivery + `is_notified` update, retry/`FAILED` at max attempts, skip-when-unconfigured |
| `test_retention.py` (4) | `voucherbot/services/retention.py` | Content-purge cutoff, untouched columns, config-driven behaviour |

**Conventions:** HTTP providers patch `polite_get` with an `AsyncMock` returning a hand-built `httpx.Response`; AI providers patch the client factories and rate-budget internals; email patches `resend.Emails.send`. Modules reading a global `settings` object get it patched with a `SimpleNamespace(...)` helper, while `test_settings.py` builds fresh `Settings` instances with `_env_file=None` and an autouse fixture clearing the environment. DB-bound functions use fake async sessions that route on `str(statement)` so unexpected SQL fails loudly.

---

## 🧪 Testing the AI Voucher Parser End-to-End

The project includes a **local test source** that lets you verify the full pipeline — scraping, keyword filtering, AI extraction, and notification — without relying on real external feeds.

### 1. Configure the Environment

Set the following in your `.env` file:

```env
IS_TEST=true
IS_PROD=false
```

- `IS_TEST=true` — seeds a `website:local_test` source pointing at `http://localhost:35926/` (see `voucherbot/database/bootstrap.py:983-1001`)
- `IS_PROD=false` — the app creates tables and runs bootstrap on startup

### 2. Start the Local Test Server

The test server is a minimal HTTP server at `D:\components\server.py` that serves:

| Route | Content |
|-------|---------|
| `GET /` | `index.html` — scraped by the WebsiteCollector |
| `GET /api/items` | `items.json` — test data payload |
| `POST /api/items` | Update test data |

**Start it from the `D:\components\` directory:**

```powershell
cd D:\components
python server.py
```

The server listens on `http://localhost:35926/`.

### 3. How the Scraper Works

The test source is defined at **`voucherbot/database/bootstrap.py:983-1001`** (`_test_source`):

```python
"config": {
    "url": "http://localhost:35926/",
    "vendor": "local_test",
    "article_selector": ".item",     # each item <div>
    "title_selector": "h2",          # title inside .item
    "link_selector": "self",         # no link extraction
    "query_terms": [...],            # keywords for filtering
    "poll_interval_minutes": 5,
}
```

The `WebsiteCollector` (`voucherbot/providers/website/collector.py:38-46`) reads these selectors and scrapes the page using BeautifulSoup.

The `index.html` at `D:\components\index.html` contains `.item` divs with `<h2>` titles — this structure matches the default selectors. **To test different content, edit the HTML or the selectors.**

### 4. Customising Test Data

Edit **`D:\components\items.json`** to control what the API returns. Default content:

```json
[
  {
    "title": "free voucher",
    "description": "free test voucher for localhost"
  }
]
```

The scraper parses the rendered HTML page (`/`), *not* the JSON API directly. The API is available if you want to build dynamic test pages.

### 5. Running the Test

Start the main app (keep the test server running in another terminal):

```powershell
uvicorn voucherbot.main:app --host 0.0.0.0 --port 9000
```

On startup the app:

1. Creates tables and seeds data (including the `website:local_test` source)
2. The scheduler picks up the source and runs the pipeline
3. The `WebsiteCollector` fetches `http://localhost:35926/` and extracts `.item` elements
4. Keyword filtering scores each post against your `query_terms`
5. AI extraction analyses matching posts
6. If a voucher is detected, a notification is sent

### 6. Watching the Pipeline

Monitor the server logs. A successful test run produces output like:

```
WebsiteCollector: fetching       url=http://localhost:35926/
WebsiteCollector: collected      url=http://localhost:35926/  count=1
pipeline: keyword filter         fetched=1 filtered=0 passed=1  source=website:local_test
pipeline: AI analysis            posts=1 ...
dispatcher: tick ran             source=website:local_test  ...
```

### 7. Modifying Scraping Behaviour

To change how the test page is parsed, edit:

| File | Lines | What to change |
|------|-------|----------------|
| `voucherbot/database/bootstrap.py` | 983-1001 | Source config (`article_selector`, `title_selector`, `link_selector`, `query_terms`) |
| `voucherbot/providers/website/collector.py` | 38-46 | Default selector fallbacks |
| `voucherbot/config/settings.py` | 103-176 | `is_test` and related settings |

After changing source config, restart the app so bootstrap re-upserts the source.

---

### 🔧 Troubleshooting

### `pytest: command not found`

Development dependencies have not been installed.

Run:

```bash
pip install -e ".[dev]"
```

---

### `Unknown pytest.mark.asyncio`

`pytest-asyncio` has not been installed.

Install the development dependencies:

```bash
pip install -e ".[dev]"
```

---

### Missing Environment Variables

If tests fail during startup because configuration values (such as `DATABASE_URL`) are missing:

- ensure `.env` exists,
- ensure it was created from `.env.example`,
- replace all placeholder values with valid configuration values,
- run the tests from the project root.