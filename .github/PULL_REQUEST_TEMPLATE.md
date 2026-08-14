## Description

<!-- Explain *why* this change is needed, not just what it does. Link any related issue: "Closes #123" or "Related to #123" -->

## Type of Change

<!-- Check all that apply -->

- [ ] Bug fix
- [ ] New feature
- [ ] New source
- [ ] Configuration / settings change
- [ ] Database migration
- [ ] Documentation update
- [ ] Refactor (no functional change)
- [ ] Other: 

## Affected Components

<!-- Check all that apply -->

- [ ] Scheduler / Dispatcher
- [ ] HTTP Policy Layer (`http_policy.py`)
- [ ] RSS Collector
- [ ] Website Collector
- [ ] Reddit Integration
- [ ] AI Layer (Groq / Gemini)
- [ ] Email Notifications
- [ ] Database / Migrations
- [ ] API / Routers
- [ ] Configuration / Settings

## Testing

<!-- How did you verify this change works? -->

- [ ] Ran `pytest` — all tests pass
- [ ] Ran `ruff check . && ruff format .` — no lint errors
- [ ] Ran `python scripts/verify_sources.py` — all sources resolve *(if sources were added or modified)*
- [ ] Added unit tests for new business logic
- [ ] Added integration tests using fixtures or recorded responses *(no live network calls)*
- [ ] Mocked Reddit API client in all new tests
- [ ] Mocked Groq / Gemini responses in all new tests

## Migration

<!-- If this PR includes a database migration, answer the following. Delete this section if not applicable. -->

- [ ] This PR includes an Alembic migration
- [ ] The migration is safe to apply to a live database without downtime
- [ ] A sensible server-side default is provided for any new columns

**Notes:**
<!-- Any additional context about the migration -->

## Policy Checklist

<!-- Required if this PR touches `http_policy.py`, `reddit/client.py`, `reddit/collector.py`, or `scheduler.py`. Delete this section if none of these files are modified. -->

- [ ] This PR touches one or more policy-sensitive files

If checked, confirm all of the following:

- [ ] `robots.txt` compliance is preserved — the policy layer is not bypassed or disabled.
- [ ] No default crawl delays have been reduced below `2.0` seconds.
- [ ] Reddit rate limits (100 req/min) and the Responsible Builder Policy are respected.
- [ ] `REDDIT_INGESTION_ENABLED=false` still collects Reddit via RSS and makes no OAuth calls.
- [ ] No new direct `httpx` or `aiohttp` calls exist outside the policy layer.

**Explanation:**
<!-- Briefly describe how the ethical and legal obligations in CONTRIBUTING.md are preserved by this change. -->

## AI Layer Changes

<!-- If this PR modifies prompts, output schema, or provider logic, answer the following. Delete this section if not applicable. -->

- [ ] Prompt changes have been tested against a representative sample of real posts
- [ ] A before/after comparison is included below
- [ ] The JSON parser handles partial responses gracefully after any schema changes

**Before / After:**
<!-- Paste a brief comparison of model output before and after your prompt or schema change -->

## Additional Notes

<!-- Anything else the reviewer should know — screenshots, references, trade-offs, follow-up work, etc. -->