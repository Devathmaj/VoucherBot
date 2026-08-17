# Database schema

**Current revision:** `o9p8q7r6s5t4`  
Apply with: `alembic upgrade head`

## Objects

| Object | Type | Purpose |
|--------|------|---------|
| `sources` | table | Feeds, pages, subreddits + scheduler fields |
| `posts` | table | Ingested items, `vendor`, `ai_result`, `is_notified`, `event_id` |
| `events` | table | Canonical promotions |
| `keywords` | table | Keyword scoring catalog |
| `vendor_mappings` | table | URL/source-name pattern → vendor lookup |
| `pipeline_lock` | table | Dispatcher lease |
| `notification_outbox` | table | Transactional outbox for voucher alert emails |
| `alembic_version` | table | Migration pointer |
| `voucher_posts` | **view** | AI-confirmed vouchers only (`is_voucher` + `PROCESSED`) |

## Enums

- `sourcetype` (includes `PEARSONVUE`, `TRAINING_PROVIDER`)
- `poststatus`
- `eventstatus`
- `notificationstatus`

## Prod rule

With `IS_PROD=true`, the app must not run DDL. Schema changes are admin-only via Alembic.
