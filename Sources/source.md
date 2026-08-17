# Source Catalog

Human-readable reference for all official ingestion sources. The **authoritative runtime catalog** is [`voucherbot/database/bootstrap.py`](../voucherbot/database/bootstrap.py), which seeds the database on app startup.

Collectors prefer RSS/APIs, identify as `VoucherBot`, obey `robots.txt` / Crawl-delay, and skip sources marked `unsupported` (ToS bans HTML scraping).

## Files

| File | Contents |
|------|----------|
| [`Subreddit.txt`](Subreddit.txt) | Reddit subreddits (Tier A/B), plus disabled subs |
| [`RSS_List.txt`](RSS_List.txt) | RSS, blog, and forum feeds |
| [`Website_List.txt`](Website_List.txt) | HTML scrapers (vendor pages, aggregators) |
| [`Event_List.txt`](Event_List.txt) | Vendor event listing pages |

## Scheduling defaults

| Tier | Poll interval | Queue priority |
|------|---------------|----------------|
| A | 15 min | Highest |
| B | 60 min | High |
| C | 4 h | Medium |
| D | 12 h | Low |

## Fetch limits (per poll)

| Collector | Items requested |
|-----------|-----------------|
| Reddit | 25 (`REDDIT_FETCH_LIMIT`) |
| RSS / Website / Pearson VUE / Training Provider | 10 |
| Curated voucher pages (`note_selector`) | 50 |

Reddit is collected from public RSS feeds by default. The `REDDIT_INGESTION_ENABLED` flag in `.env` (default `false`) gates only the OAuth API: when `false`, the OAuth API is never called and posts come from the RSS feeds.

## Policy-blocked HTML sources

These remain in the catalog but are `enabled=false` / `unsupported=true` (RSS alternatives used where available):

- Pluralsight / Cloud Academy blog
- Cisco Live
- ISC2 Insights
- Red Hat Training Specials
- AWS Events / re:Invent pages
- The Register (feed blocked by a proof-of-work challenge)

## Verify sources

```bash
python scripts/verify_sources.py
```
