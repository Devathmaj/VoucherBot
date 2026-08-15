"""
Shared processing pipeline: Collect → Keyword Filter → Dedup → AI Extraction → Event Matching

Stage 1 — Deduplication
    identity_hash = SHA-256(normalised URL)  — stable page identity.
    content_hash  = SHA-256(title|content|date) — changes when the page changes.
    INSERT on new identity; UPDATE title/content/content_hash when content_hash
    differs (page was updated). Unchanged pages are skipped entirely.

Stage 2 — AI Extraction
    Only new or updated posts are sent to the AI analyzer.

Stage 3 — Canonical Event Matching
    Score >= auto_merge_threshold  → attach to existing Event (AUTO_MERGED).
    Score in [possible_match, auto_merge) → POSSIBLE_MATCH.
    Score < possible_match_threshold → create new Event.

Stage 4 — Email notification
    On is_voucher for NEW / POSSIBLE_MATCH / updated posts, a voucher alert is
    staged into the notification outbox (same transaction as the pipeline) and
    delivered with a stable idempotency key; undelivered rows are retried by
    the scheduler until SENT, then posts.is_notified is set.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlparse

import structlog
from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.config.settings import settings
from voucherbot.models.keyword import Keyword
from voucherbot.models.post import Post, PostStatus
from voucherbot.models.source import Source, SourceType
from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.services.ai.analyzer import analyze_post_batch
from voucherbot.services.email.notifications import (
    deliver_pending_notifications,
    stage_voucher_notification,
)
from voucherbot.services.bot_notification import send_bot_notification
from voucherbot.services.ingestion.dedup import identity_hash, content_hash
from voucherbot.services.ingestion.event_matcher import EventMatcher
from voucherbot.models.event import MatchConfidence
from voucherbot.models.vendor_mapping import VendorMapping

logger = structlog.get_logger(__name__)


async def _load_vendor_mappings(
    db: AsyncSession,
) -> list[dict[str, str | None]]:
    """Load all vendor mappings from the database table."""
    result = await db.execute(
        select(
            VendorMapping.url_pattern,
            VendorMapping.source_name_pattern,
            VendorMapping.vendor,
        )
    )
    return [
        {
            "url_pattern": str(row.url_pattern) if row.url_pattern else None,
            "source_name_pattern": str(row.source_name_pattern)
            if row.source_name_pattern
            else None,
            "vendor": str(row.vendor),
        }
        for row in result.all()
    ]


def _normalise_url_for_match(url: str) -> str:
    """Strip trailing slashes and query params for consistent startswith matching."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _resolve_vendor(
    source_name: str,
    base_url: str | None,
    mappings: list[dict[str, str | None]],
) -> str | None:
    """Resolve the vendor for a source using the loaded mappings.

    Priority:
      1. URL-pattern match against the source's base URL (startswith).
      2. Source-name-pattern match (lowercase substring).
    Returns ``None`` for aggregators / unknown sources so the AI can guess.
    """
    # 1. Try URL pattern match
    if base_url:
        normalised = _normalise_url_for_match(base_url)
        for m in mappings:
            pat = m.get("url_pattern")
            if pat and normalised.startswith(pat.rstrip("/")):
                return m["vendor"]

    # 2. Fallback to source name pattern match
    name_lower = source_name.lower()
    for m in mappings:
        pat = m.get("source_name_pattern")
        if pat and pat in name_lower:
            return m["vendor"]

    return None


SCORE_THRESHOLD = 1
_AI_CONTENT_LIMIT = 500
_event_matcher = EventMatcher()


def _resolve_collector(
    source: Source,
    collectors: dict[str, BaseCollector],
) -> BaseCollector | None:
    config = source.config or {}
    if source.type == SourceType.REDDIT:
        return collectors.get("reddit")
    if "feed_url" in config:
        return collectors.get("rss")
    if "article_selector" in config:
        return collectors.get("web")
    if source.type == SourceType.PEARSONVUE:
        return collectors.get("pearsonvue")
    if source.type == SourceType.TRAINING_PROVIDER:
        return collectors.get("training_provider")
    return None


def _fetch_limit_for_source(source: Source, fetch_limit: int | None = None) -> int:
    if fetch_limit is not None:
        return fetch_limit
    if source.type == SourceType.REDDIT:
        return settings.reddit_fetch_limit
    if (source.config or {}).get("note_selector"):
        return 50
    return 10


def _ai_content(content: str | None) -> str | None:
    """Return only the Note line if present, otherwise a short content snippet."""
    if not content:
        return None
    first_line = content.split("\n", 1)[0].strip()
    if first_line.startswith("Note:"):
        return first_line
    return content[:_AI_CONTENT_LIMIT] or None


async def run_pipeline_for_source(
    db: AsyncSession,
    source: Source,
    collectors: dict[str, BaseCollector],
    fetch_limit: int | None = None,
) -> dict[str, Any]:
    """Run the full ingestion pipeline for a single source."""
    sync_id = f"Sync-{str(uuid.uuid4())[:8]}"
    start_time = datetime.now(timezone.utc)
    limit = _fetch_limit_for_source(source, fetch_limit)

    collector = _resolve_collector(source, collectors)
    if not collector:
        logger.error(f"{sync_id} No suitable collector found for {source.name}")
        return {"errors": 1}

    kw_result = await db.execute(select(Keyword).where(Keyword.enabled == True))  # noqa: E712
    keywords: list[Keyword] = list(kw_result.scalars().all())

    stats = await _process_one_source(db, source, collector, keywords, limit)

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(
        f"{sync_id} [{source.name}] complete",
        duration=f"{duration:.1f}s",
        **stats,
    )
    return stats


async def _process_one_source(
    db: AsyncSession,
    source: Source,
    collector: BaseCollector,
    keywords: list[Keyword],
    fetch_limit: int,
) -> dict[str, Any]:
    stats = {
        "fetched": 0,
        "keyword_filtered": 0,
        "unchanged": 0,
        "new_posts": 0,
        "updated_posts": 0,
        "ai_analyzed": 0,
        "events_created": 0,
        "events_attached": 0,
        "possible_matches": 0,
        "notified": 0,
        "bot_notified": 0,
    }

    # ── Stage 0: Collect ──────────────────────────────────────────────────────
    posts: list[NormalizedPost] = await collector.collect(
        source_config=source.config or {},
        limit=fetch_limit,
    )
    stats["fetched"] = len(posts)

    # ── Keyword Filtering ─────────────────────────────────────────────────────
    # Sources with note_selector are curated voucher pages — skip keyword filter.
    skip_keyword_filter = bool((source.config or {}).get("note_selector"))
    scored: list[tuple[NormalizedPost, int]] = []
    for post in posts:
        if skip_keyword_filter:
            scored.append((post, SCORE_THRESHOLD))
            continue
        text = f"{post.title} {post.content or ''}".lower()
        score = sum(kw.score for kw in keywords if kw.keyword.lower() in text)
        if score < SCORE_THRESHOLD:
            stats["keyword_filtered"] += 1
        else:
            scored.append((post, score))

    logger.info(
        "pipeline: keyword filter",
        source=source.name,
        fetched=len(posts),
        passed=len(scored),
        filtered=stats["keyword_filtered"],
    )
    if not scored:
        source.last_checked_utc = datetime.now(timezone.utc)
        source.error_count = 0
        await db.commit()
        return stats

    # ── Stage 1: Dedup + upsert ───────────────────────────────────────────────
    # identity_hash = SHA-256(normalised URL)       — stable page identity
    # content_hash  = SHA-256(title|content|date)   — changes when page changes
    # Intra-batch: drop duplicate URLs seen more than once in this fetch.
    seen_ids: set[str] = set()
    deduped: list[tuple[NormalizedPost, int, str, str]] = []
    for post, score in scored:
        ih = identity_hash(post.url)
        ch = content_hash(post.title, post.content, post.published_at)
        if ih in seen_ids:
            stats["unchanged"] += 1
        else:
            seen_ids.add(ih)
            deduped.append((post, score, ih, ch))

    # INSERT on new identity; UPDATE only when content_hash changed.
    inserted_posts: list[tuple[NormalizedPost, Post, bool]] = []  # (raw, db, is_new)

    vendor_mappings = await _load_vendor_mappings(db)
    source_vendor = _resolve_vendor(source.name, source.base_url, vendor_mappings)

    for post, score, ih, ch in deduped:
        stmt = (
            insert(Post)
            .values(
                source_id=source.id,
                external_id=ih,
                url=post.url,
                title=post.title,
                content=post.content,
                summary=post.summary,
                author=post.author,
                published_at=post.published_at,
                status=PostStatus.QUEUED,
                score=score,
                raw_data=post.raw_data,
                content_hash=ch,
                is_notified=False,
                vendor=source_vendor,
            )
            .on_conflict_do_update(
                constraint="uq_posts_source_external",
                set_={
                    "title": insert(Post).excluded.title,
                    "content": insert(Post).excluded.content,
                    "summary": insert(Post).excluded.summary,
                    "content_hash": insert(Post).excluded.content_hash,
                    "status": PostStatus.QUEUED,
                    "vendor": insert(Post).excluded.vendor,
                },
                where=Post.content_hash != ch,
            )
        )

        try:
            async with db.begin_nested():
                result = await db.execute(stmt)
        except IntegrityError:
            stats["unchanged"] += 1
            continue

        if cast(CursorResult[Any], result).rowcount == 0:
            # Check if existing post is stuck from a previously failed run.
            stuck_result = await db.execute(
                select(Post).where(
                    Post.source_id == source.id,
                    Post.external_id == ih,
                    Post.status == PostStatus.QUEUED,
                    Post.event_id == None,  # noqa: E711
                )
            )
            stuck_post = stuck_result.scalars().first()
            if stuck_post:
                inserted_posts.append((post, stuck_post, True))
                stats["new_posts"] += 1
            else:
                stats["unchanged"] += 1
            continue

        inserted_row = await db.execute(
            select(Post).where(
                Post.source_id == source.id,
                Post.external_id == ih,
            )
        )
        db_post = inserted_row.scalars().first()
        if db_post:
            # rowcount=1 on INSERT sets lastrowid; on DO UPDATE it does not.
            # Distinguish by checking whether the row already existed before
            # this upsert: a pure INSERT returns the new id via inserted_primary_key,
            # a DO UPDATE returns nothing there. Use result.inserted_primary_key.
            inserted_primary_key = cast(
                tuple[Any, ...] | None,
                cast(CursorResult[Any], result).inserted_primary_key,
            )
            is_new = bool(inserted_primary_key and inserted_primary_key[0])
            if is_new:
                stats["new_posts"] += 1
            else:
                stats["updated_posts"] += 1
            inserted_posts.append((post, db_post, is_new))

    # ── Stage 2: AI Extraction ────────────────────────────────────────────────
    pending_notifications = []
    if inserted_posts:
        batch_inputs = [
            (post.title, _ai_content(post.content)) for post, _, _ in inserted_posts
        ]
        batch_results = await analyze_post_batch(batch_inputs, source.name)

        for (post, db_post, is_new), extracted in zip(inserted_posts, batch_results):
            if extracted is None:
                continue

            db_post.ai_result = extracted.model_dump()
            stats["ai_analyzed"] += 1

            # Override vendor from source mapping for known sources.
            # For unknown sources (aggregators), keep AI's educated guess.
            if source_vendor:
                extracted.vendor = source_vendor

            if not extracted.is_voucher:
                db_post.status = PostStatus.FILTERED
                logger.debug(
                    "pipeline: AI classified as non-voucher",
                    post_id=db_post.id,
                    confidence=extracted.confidence,
                )
                continue

            # ── Stage 3: Canonical Event Matching ─────────────────────────────
            if db_post.event_id is not None:
                _event, confidence = await _event_matcher.update_existing(
                    db, extracted, db_post, source.type
                )
            else:
                _event, confidence = await _event_matcher.match_or_create(
                    db, extracted, db_post, source.type
                )
            db_post.status = PostStatus.PROCESSED

            if confidence == MatchConfidence.NEW:
                stats["events_created"] += 1
            elif confidence == MatchConfidence.POSSIBLE_MATCH:
                stats["possible_matches"] += 1
            else:
                stats["events_attached"] += 1

            # Notify on new posts and on possible matches (not for auto-merge or updates).
            if confidence not in (MatchConfidence.AUTO_MERGED, MatchConfidence.UPDATED):
                pending_notifications.append((db_post, extracted))

    # ── Finalise + Stage 4: Email alert (transactional outbox) ───────────────
    # Delivery intent is persisted BEFORE the commit so the outbox row and the
    # pipeline data commit atomically — delivery state survives commit failures.
    source.last_checked_utc = datetime.now(timezone.utc)
    source.error_count = 0
    staged = 0
    for db_post, extracted in pending_notifications:
        if await stage_voucher_notification(db, db_post, extracted):
            staged += 1
    await db.commit()

    # Attempt immediate delivery; failures stay PENDING for the scheduler to
    # retry (idempotency keyed, so replays cannot duplicate emails).
    stats["notified"] = 0
    if staged:
        stats["notified"] = await deliver_pending_notifications(db)

    # Send the same voucher alert to the bot webhook alongside the email.
    # Best-effort: a webhook failure never fails the pipeline or the email.
    stats["bot_notified"] = 0
    for db_post, extracted in pending_notifications:
        if await send_bot_notification(db_post, extracted):
            stats["bot_notified"] += 1

    return stats
