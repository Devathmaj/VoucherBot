"""Event consolidation: periodically merge duplicate canonical Events.

Two Posts describing the same real-world promotion can become separate Events
when their sources were processed at different times (the ingestion-time
matcher only sees candidates that already exist at that moment).  This module
runs a periodic sweep that finds those duplicates retroactively:

1. Events sharing a cheap identity signal (normalised registration URL,
   voucher code, or vendor) are grouped into candidate pairs.
2. Each pair is gated by the same deterministic weighted score used at
   ingestion; only pairs at or above ``possible_match_threshold`` are kept.
3. When Groq is configured, qwen is asked whether the pair is the same
   real-world promotion (see ``compare_events``).  A ``same`` decision with
   ``confidence >= ai_possible_match_confidence`` merges the pair; a model
   outage falls back to the deterministic score >= auto-merge threshold.
4. The pair's merge picks a survivor (the Event with more Posts; ties keep the
   older Event), folds the absorber's fields into it via the shared
   ``_merge_fields`` machinery, re-points the absorber's Posts, and archives
   it.  Provenance is preserved: Posts are never merged.

The sweep is cross-instance serialised with a Postgres advisory transaction
lock, throttled so qwen spend stays bounded, and isolated from the rest of the
scheduler loop — it never raises.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Optional, cast

import structlog
from sqlalchemy import CursorResult, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.config.settings import settings as settings
from voucherbot.database.connection import session_scope
from voucherbot.models.event import Event, EventStatus, MatchConfidence
from voucherbot.models.post import Post
from voucherbot.models.source import SourceType
from voucherbot.services.ai.event_matcher_ai import (
    EventMatchDecision,
    compare_events,
)
from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.ingestion.dedup import normalise_url
from voucherbot.services.ingestion.event_matcher import (
    _merge_fields,
    _score_candidate,
)

logger = structlog.get_logger(__name__)

# pg_advisory_xact_lock key scoping this job to a single scheduler instance.
_LOCK_KEY = 734_001
# Per-bucket cap while building the candidate-pair graph (bounds quadratic work).
_MAX_BUCKET_SAMPLE = 200

# Decision function shared by discovery and the sweep entry point.
CompareFn = Callable[[Event, Event], Awaitable[Optional[EventMatchDecision]]]

_last_merge_ts: float = 0.0


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


def _candidate_pairs(events: list[Event]) -> list[tuple[Event, Event]]:
    """Return distinct Event pairs sharing a cheap identity signal.

    Pair order (a, b) is deterministic in the input list; duplicates are
    collapsed by the canonical ``(min_id, max_id)`` key so a pair found via
    both a shared URL and voucher code is only reported once.
    """
    buckets: dict[tuple[str, str], list[Event]] = {}
    for event in events:
        if event.registration_url:
            key = ("url", normalise_url(event.registration_url))
            buckets.setdefault(key, []).append(event)
        if event.voucher_code and event.voucher_code.upper() not in ("N/A", "NA"):
            key = ("code", event.voucher_code.upper())
            buckets.setdefault(key, []).append(event)
        if event.vendor:
            key = ("vendor", event.vendor.lower())
            buckets.setdefault(key, []).append(event)

    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[Event, Event]] = []
    for bucket in buckets.values():
        sampled = bucket[:_MAX_BUCKET_SAMPLE]
        for i in range(len(sampled)):
            a = sampled[i]
            for j in range(i + 1, len(sampled)):
                b = sampled[j]
                pair_key = (min(a.id, b.id), max(a.id, b.id))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                pairs.append((a, b))
    return pairs


def _event_to_extracted(event: Event) -> ExtractedEvent:
    """Project an Event onto the ExtractedEvent shape the scorer understands."""
    return ExtractedEvent(
        is_voucher=True,
        confidence=1.0,
        vendor=event.vendor,
        promotion_name=event.promotion_name,
        promotion_type=event.promotion_type,
        certifications=event.certifications,
        voucher_code=event.voucher_code,
        discount=event.discount,
        registration_url=event.registration_url,
        start_date=event.start_date.isoformat() if event.start_date else None,
        end_date=event.end_date.isoformat() if event.end_date else None,
        regions=event.regions,
    )


def _pair_score(a: Event, b: Event) -> int:
    """Reuse the ingestion-time deterministic weighted score between two Events."""
    return _score_candidate(a, _event_to_extracted(b))


def _choose_survivor(
    a: Event, b: Event, post_counts: dict[int, int]
) -> tuple[Event, Event]:
    """Return (survivor, absorbed): the Event with more Posts wins.

    Ties keep the older Event (lower id) as the canonical record.
    """
    a_count = post_counts.get(a.id, 0)
    b_count = post_counts.get(b.id, 0)
    if a_count > b_count:
        return a, b
    if b_count > a_count:
        return b, a
    if a.id < b.id:
        return a, b
    return b, a


def _origin_source(event: Event) -> SourceType:
    """Best-guess originating source of an Event for the merge audit log.

    The earliest readable ``source_type`` in the Event's merge_log (the source
    that created it) is used; falls back to RSS (lowest authority) when
    unrestorable.
    """
    for entry in event.merge_log or []:
        raw = entry.get("source_type")
        if not isinstance(raw, str):
            continue
        try:
            return SourceType(raw)
        except ValueError:
            continue
    return SourceType.RSS


# ---------------------------------------------------------------------------
# Merge discovery + application
# ---------------------------------------------------------------------------


async def _discover_merges(
    events: list[Event],
    post_counts: dict[int, int],
    compare: CompareFn,
) -> tuple[list[tuple[Event, Event, int, Optional[str]]], int, int, int]:
    """Decide which Event pairs to merge this sweep.

    Returns ``(merges, ai_calls, candidate_pairs, gated_pairs)`` where each
    merge is ``(survivor, absorbed, match_score, match_reason)``.  Deterministic
    gating bounds qwen volume, and the absorbed-set prevents one Event from
    being folded into two targets in a single sweep.
    """
    cfg = settings.event_matcher
    cons = settings.consolidation

    merges: list[tuple[Event, Event, int, Optional[str]]] = []
    absorbed: set[int] = set()
    ai_calls = 0

    pairs = _candidate_pairs(events)[: cons.max_pairs_per_sweep]
    gated: list[tuple[int, Event, Event]] = []
    for a, b in pairs:
        score = _pair_score(a, b)
        if score >= cfg.possible_match_threshold:
            gated.append((score, a, b))
    gated.sort(key=lambda item: item[0], reverse=True)

    for score, a, b in gated:
        if a.id in absorbed or b.id in absorbed:
            continue
        survivor, loser = _choose_survivor(a, b, post_counts)

        if ai_calls < cons.max_ai_calls_per_sweep:
            ai_calls += 1
            decision = await compare(survivor, loser)
            if decision is None:
                # Model unavailable — fall back to the deterministic floor.
                if score < cons.deterministic_auto_merge_threshold:
                    continue
                merges.append((survivor, loser, score, None))
            elif (
                not decision.is_same_promotion
                or decision.confidence < cfg.ai_possible_match_confidence
            ):
                # Model (or its weak confidence) says these are different.
                continue
            else:
                ai_score = int(round(decision.confidence * 100))
                merges.append((survivor, loser, ai_score, decision.reason))
        else:
            # AI budget spent — deterministic floor only.
            if score < cons.deterministic_auto_merge_threshold:
                continue
            merges.append((survivor, loser, score, None))

        absorbed.update({a.id, b.id})

    return merges, ai_calls, len(pairs), len(gated)


async def _post_counts(session: AsyncSession) -> dict[int, int]:
    """Count Posts per Event for survivor selection."""
    result = await session.execute(
        select(Post.event_id, func.count())
        .where(Post.event_id.is_not(None))
        .group_by(Post.event_id)
    )
    return {
        int(event_id): int(count)
        for event_id, count in result.all()
        if event_id is not None
    }


async def _apply_merge(
    session: AsyncSession,
    survivor: Event,
    loser: Event,
    score: int,
    reason: Optional[str],
) -> int:
    """Fold ``loser`` into ``survivor`` and archive it.

    Repoints the absorbed Event's Posts to the survivor, merges its fields via
    source priority, and records the consolidation on both audit logs.  Returns
    the number of Posts re-pointed.
    """
    loser_source = _origin_source(loser)
    _merge_fields(
        survivor,
        loser,
        loser_source,
        loser.id,
        score,
        MatchConfidence.AUTO_MERGED,
        match_reason=reason,
    )
    result = await session.execute(
        text("UPDATE posts SET event_id = :survivor_id WHERE event_id = :loser_id"),
        {"survivor_id": survivor.id, "loser_id": loser.id},
    )
    rowcount = cast(CursorResult[Any], result).rowcount or 0

    loser.status = EventStatus.ARCHIVED
    loser.merge_log = (loser.merge_log or []) + [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_type": loser_source.value,
            "post_id": loser.id,
            "match_score": score,
            "match_confidence": EventStatus.ARCHIVED.value,
            "fields_updated": [],
            "reason": f"consolidated into event {survivor.id}",
        }
    ]
    return int(rowcount)


# ---------------------------------------------------------------------------
# Sweep entry point
# ---------------------------------------------------------------------------


async def consolidate_events() -> dict[str, int]:
    """Run one throttled consolidation sweep, returning stats. Never raises."""
    stats = {
        "candidate_pairs": 0,
        "gated_pairs": 0,
        "ai_calls": 0,
        "merged": 0,
        "posts_repointed": 0,
    }
    global _last_merge_ts
    cfg = settings.consolidation
    if not cfg.enabled:
        return stats
    if time.monotonic() - _last_merge_ts < cfg.interval_minutes * 60:
        return stats
    _last_merge_ts = time.monotonic()

    try:
        async with session_scope() as session:
            # Cross-instance serialisation: hold a transaction-level advisory
            # lock for the whole sweep so only one scheduler runs it at a time.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
            events = list(
                (
                    await session.execute(
                        select(Event).where(Event.status == EventStatus.ACTIVE)
                    )
                )
                .scalars()
                .all()
            )
            post_counts = await _post_counts(session)
            merges, ai_calls, candidate_pairs, gated_pairs = await _discover_merges(
                events, post_counts, compare_events
            )
            repointed = 0
            for survivor, loser, score, reason in merges:
                repointed += await _apply_merge(session, survivor, loser, score, reason)
            await session.commit()

        stats["candidate_pairs"] = candidate_pairs
        stats["gated_pairs"] = gated_pairs
        stats["ai_calls"] = ai_calls
        stats["merged"] = len(merges)
        stats["posts_repointed"] = repointed
        if stats["merged"]:
            logger.info("event_consolidation: merged duplicate events", **stats)
    except Exception as exc:
        logger.warning("event_consolidation: sweep failed", error=str(exc)[:200])
    return stats
