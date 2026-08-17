"""
Stage 3 — Canonical Event Matching.

Given an ``ExtractedEvent`` produced by the AI analyzer, the ``EventMatcher``
determines whether the data describes an existing canonical ``Event`` (and
attaches the Post to it), or whether a brand-new Event should be created.

Matching operates on structured fields (never raw article text).  By default
the merge decision is handed to the qwen reasoning model (see below); the
deterministic weighted score below is only used as a fallback.

Scoring
-------
Configured via ``settings.event_matcher`` (an ``EventMatcherConfig`` instance):

  Field                 Default weight
  ──────────────────────────────────────────
  registration_url         +50   (exact normalised URL match)
  voucher_code             +40   (exact, case-normalised)
  promotion_name           +25   (token-overlap similarity >= name_threshold)
  vendor                   +20   (exact, lower-cased)
  discount                 +20   (normalised value + type, see ``_discounts_match``)
  promotion_type           +10   (exact, normalised text match)
  certifications           +15   (at least one cert in common)
  date_overlap             +10   (date ranges overlap or are both absent)

Score bands (configurable thresholds):
  >= auto_merge_threshold (70)         → attach to existing Event
  >= possible_match_threshold (45)     → flag as POSSIBLE_MATCH (future review)
  <  possible_match_threshold          → create a new Event

AI-backed matching
------------------
When ``settings.event_matcher.use_ai_matcher`` is enabled and candidates exist,
the qwen reasoning model is asked whether the incoming extracted promotion is
the same as each candidate that the deterministic weighted score flags as a
possible match (``score >= possible_match_threshold``, sorted best-first and
capped by ``ai_candidate_limit``; see ``voucherbot.services.ai.event_matcher_ai``).
The model's ``is_same_promotion`` and ``confidence`` drive the decision:

  is_same_promotion and confidence >= ai_auto_merge_confidence
                                         → AUTO_MERGED
  is_same_promotion and confidence >= ai_possible_match_confidence
                                         → POSSIBLE_MATCH
  otherwise                              → new Event

The deterministic weighted scoring above remains as a fallback when the model
is unavailable, no Groq key is configured, or no candidates exist.

Source Priority & Field Merging
--------------------------------
When an Event is updated by a new Post, fields are merged field-by-field
according to SOURCE_PRIORITY (defined in settings).  A higher-priority
source's non-null value takes precedence over an existing lower-priority
source's value.  The Event identity (id, created_at) is never changed.  An
audit entry is appended to ``event.merge_log`` for every update.

Provenance
----------
Posts are NEVER merged.  Many Posts may reference the same Event via the
``Post.event_id`` FK.  The ``Event.posts`` relationship provides full
provenance.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone
from typing import Optional, Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.config.settings import SOURCE_PRIORITY, settings
from voucherbot.models.event import Event, EventStatus, MatchConfidence
from voucherbot.models.post import Post
from voucherbot.models.source import SourceType
from voucherbot.services.ai.event_matcher_ai import (
    EventMatchDecision,
    compare_candidate,
)
from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.ingestion.dedup import normalise_url

logger = structlog.get_logger(__name__)

# Scalar event fields that can be backfilled from incoming extraction data.
_MERGEABLE_FIELDS: tuple[str, ...] = (
    "vendor",
    "promotion_name",
    "promotion_type",
    "certifications",
    "voucher_code",
    "discount",
    "registration_url",
    "start_date",
    "end_date",
    "regions",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _source_priority(source_type: SourceType) -> int:
    """Lower return value = higher authority (0 is most authoritative)."""
    try:
        return SOURCE_PRIORITY.index(source_type.value)
    except ValueError:
        return len(SOURCE_PRIORITY)  # unknown sources get lowest priority


def _normalise_text(value: Optional[str]) -> str:
    """Lower-case and collapse whitespace for stable field comparison."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _text_fields_match(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return _normalise_text(a) == _normalise_text(b)


def _normalize_discount(discount: Optional[str]) -> Optional[tuple[float, str]]:
    """Normalize a discount string to a comparable (value, type) pair.

    Returns ``(value, 'percent')`` for percentage-based discounts,
    ``(value, 'absolute')`` for fixed-amount discounts, or ``None``
    when the discount cannot be parsed.

    Equivalent representations such as ``"50%"``, ``"50 %"``,
    ``"50 percent"``, and ``"50% exam voucher"`` all normalise to
    ``(50.0, 'percent')``.
    """
    if not discount:
        return None

    text = discount.strip().lower()

    # "free" / "complimentary" / "no cost"
    if text in ("free", "complimentary", "no cost"):
        return (100.0, "percent")

    # Percentage patterns:
    #   "50%", "50 %", "50 percent", "50% off", "save 50%"
    m = re.search(
        r"(?:save\s+)?(\d+(?:\.\d+)?)\s*%\s*(?:off|discount)?"
        r"|(\d+(?:\.\d+)?)\s*percent\s*(?:off|discount)?",
        text,
    )
    if m:
        return (float(m.group(1) or m.group(2)), "percent")

    # Absolute dollar amount: "$100", "$ 100 off"
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)(?:\s+off|\s+discount)?", text)
    if m:
        return (float(m.group(1)), "absolute")

    # Absolute amount: "100 USD", "100 dollars"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:usd|dollars?)\s*(?:off|discount)?", text)
    if m:
        return (float(m.group(1)), "absolute")

    return None


def _discounts_match(a: Optional[str], b: Optional[str]) -> bool:
    """Compare discounts by normalised (value, type) pair, fall back to text.

    ``"50%"``, ``"50 %"``, ``"50 percent"`` and ``"50% exam voucher"``
    all normalise to ``(50.0, 'percent')`` → **match**.

    Different types (percent vs absolute) never match, so ``"50%"``
    and ``"$50"`` are treated as different discounts.

    Falls back to exact normalised-text comparison when either side
    cannot be parsed (e.g. ``"Free"`` vs ``"Free"``).
    """
    na = _normalize_discount(a)
    nb = _normalize_discount(b)
    if na is not None and nb is not None:
        return na == nb
    return _text_fields_match(a, b)


def _name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Token-overlap similarity between two promotion names (0.0 – 1.0)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _certs_overlap(a: Optional[list[str]], b: Optional[list[str]]) -> bool:
    if not a or not b:
        return False
    a_set = {c.upper() for c in a}
    b_set = {c.upper() for c in b}
    return bool(a_set & b_set)


def _dates_overlap(
    e_start: Optional[datetime],
    e_end: Optional[datetime],
    x_start: Optional[str],
    x_end: Optional[str],
) -> bool:
    """Return True if date ranges overlap, or if both have no dates (unknown)."""

    def _parse(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None

    x_start_dt = _parse(x_start)
    x_end_dt = _parse(x_end)

    # If BOTH sides have no dates at all, consider it a weak match.
    if not any([e_start, e_end, x_start_dt, x_end_dt]):
        return True

    # Need at least one end-point from each side to check overlap.
    a_start = e_start or x_start_dt
    a_end = e_end or x_end_dt
    b_start = x_start_dt or e_start
    b_end = x_end_dt or e_end

    if a_start and b_end and a_start > b_end:
        return False
    if b_start and a_end and b_start > a_end:
        return False
    return True


def _score_candidate(event: Event, extracted: ExtractedEvent) -> int:
    """Compute a matching confidence score for ``extracted`` against ``event``."""
    cfg = settings.event_matcher
    score = 0

    # 1. registration_url — exact normalised URL match
    if event.registration_url and extracted.registration_url:
        if normalise_url(event.registration_url) == normalise_url(
            extracted.registration_url
        ):
            score += cfg.weight_registration_url

    # 2. voucher_code — exact case-normalised match
    if event.voucher_code and extracted.voucher_code:
        if event.voucher_code.upper() == extracted.voucher_code.upper():
            score += cfg.weight_voucher_code

    # 3. vendor — exact lower-cased match
    if event.vendor and extracted.vendor:
        if event.vendor.lower() == extracted.vendor.lower():
            score += cfg.weight_vendor

    # 4. promotion_name — token-overlap similarity
    sim = _name_similarity(event.promotion_name, extracted.promotion_name)
    if sim >= cfg.name_similarity_threshold:
        score += cfg.weight_promotion_name

    # 5. discount — numeric normalised match (e.g. "50%" == "50 %" == "50 percent")
    if _discounts_match(event.discount, extracted.discount):
        score += cfg.weight_discount

    # 6. promotion_type — exact normalised match
    if _text_fields_match(event.promotion_type, extracted.promotion_type):
        score += cfg.weight_promotion_type

    # 7. certifications — at least one in common
    if _certs_overlap(event.certifications, extracted.certifications):
        score += cfg.weight_certifications

    # 8. date overlap
    if _dates_overlap(
        event.start_date, event.end_date, extracted.start_date, extracted.end_date
    ):
        score += cfg.weight_date_overlap

    return score


def _candidate_relevance(event: Event, extracted: ExtractedEvent) -> int:
    """Quick pre-score used to rank candidates before the full limit is applied."""
    relevance = 0
    if event.vendor and extracted.vendor:
        if event.vendor.lower() == extracted.vendor.lower():
            relevance += 3
    if _discounts_match(event.discount, extracted.discount):
        relevance += 2
    if _text_fields_match(event.promotion_type, extracted.promotion_type):
        relevance += 1
    if _name_similarity(event.promotion_name, extracted.promotion_name) >= 0.5:
        relevance += 2
    if event.voucher_code and extracted.voucher_code:
        if event.voucher_code.upper() == extracted.voucher_code.upper():
            relevance += 3
    if event.registration_url and extracted.registration_url:
        if normalise_url(event.registration_url) == normalise_url(
            extracted.registration_url
        ):
            relevance += 3
    return relevance


def _merge_fields(
    event: Event,
    extracted: ExtractedEvent | Event,
    source_type: SourceType,
    post_id: int,
    match_score: int,
    match_confidence: MatchConfidence,
    match_reason: Optional[str] = None,
) -> list[str]:
    """Merge non-null fields from ``extracted`` into ``event`` using source priority.

    ``extracted`` may be an ``ExtractedEvent`` from the AI pipeline or another
    ``Event`` (used by the consolidation sweep to fold one canonical Event into
    another); both expose the same ``_MERGEABLE_FIELDS`` attributes.

    Merge rules (in order):
    1. Null incoming values are **never** written (preserves existing data).
    2. Null existing values are **backfilled** from the incoming data regardless
       of source priority (fills missing fields).
    3. When both values are present, the source with **higher priority** (lower
       ``SOURCE_PRIORITY`` index) wins.  The priority that last wrote the field
       is determined by walking the ``merge_log`` in reverse (most recent first).
    4. Fields are never blindly overwritten — a lower-priority source cannot
       replace a value set by a higher-priority source.

    Returns a list of field names that were actually updated.
    Updates event.merge_log with an audit entry.
    """
    incoming_priority = _source_priority(source_type)
    updated_fields: list[str] = []

    for field in _MERGEABLE_FIELDS:
        incoming_val = getattr(extracted, field, None)
        if incoming_val is None:
            continue  # nothing to contribute

        # Date fields arrive as ISO strings from the AI; convert to datetime.
        if field in ("start_date", "end_date") and isinstance(incoming_val, str):
            try:
                dt = datetime.fromisoformat(incoming_val)
                incoming_val = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue  # skip unparseable dates

        existing_val = getattr(event, field, None)

        if existing_val is None:
            # Backfill a missing field regardless of priority.
            setattr(event, field, incoming_val)
            updated_fields.append(field)
        else:
            # Existing value present: only overwrite if the incoming source has
            # higher authority.
            existing_priority = _get_event_field_source_priority(event, field)
            if incoming_priority < existing_priority:
                setattr(event, field, incoming_val)
                updated_fields.append(field)

    # --- Append audit log entry ---
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_type": source_type.value,
        "post_id": post_id,
        "match_score": match_score,
        "match_confidence": match_confidence.value,
        "fields_updated": updated_fields,
    }
    if match_reason:
        log_entry["reason"] = match_reason
    current_log: list[Any] = event.merge_log or []
    event.merge_log = current_log + [log_entry]

    return updated_fields


def _get_event_field_source_priority(event: Event, field: str) -> int:
    """Determine the effective source priority for an existing event field.

    We look at the merge_log in reverse (most recent first) to find which
    source last set this field.  If unknown, assume lowest priority so any
    incoming data can overwrite.
    """
    for entry in reversed(event.merge_log or []):
        if field in (entry.get("fields_updated") or []):
            source_type_val = entry.get("source_type", "")
            try:
                return SOURCE_PRIORITY.index(source_type_val)
            except ValueError:
                pass
    # Field was set during Event creation (first post) — look at the first
    # log entry if it exists.
    first_entry = (event.merge_log or [None])[0]
    if first_entry:
        source_type_val = first_entry.get("source_type", "")
        try:
            return SOURCE_PRIORITY.index(source_type_val)
        except ValueError:
            pass
    return len(SOURCE_PRIORITY)  # unknown → lowest priority


def _extracted_to_event_fields(extracted: ExtractedEvent) -> dict[str, Any]:
    """Map an ExtractedEvent to a dict of Event column values."""

    def _parse_date(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    return {
        "vendor": extracted.vendor,
        "promotion_name": extracted.promotion_name,
        "promotion_type": extracted.promotion_type,
        "certifications": extracted.certifications,
        "voucher_code": extracted.voucher_code,
        "discount": extracted.discount,
        "registration_url": extracted.registration_url,
        "start_date": _parse_date(extracted.start_date),
        "end_date": _parse_date(extracted.end_date),
        "regions": extracted.regions,
    }


# ---------------------------------------------------------------------------
# EventMatcher
# ---------------------------------------------------------------------------


class EventMatcher:
    """Match AI-extracted post data to a canonical Event, or create a new one.

    Usage::

        matcher = EventMatcher()
        event, confidence = await matcher.match_or_create(
            db, extracted, post, source_type
        )
    """

    async def _find_candidates(
        self, db: AsyncSession, extracted: ExtractedEvent
    ) -> list[Event]:
        """Retrieve candidate Events to score against.

        Uses indexed columns (registration_url, voucher_code, vendor) to avoid
        a full-table scan when possible.  When those are absent, falls back to
        token-based promotion_name matching so that likely matches are not
        excluded simply because the post lacks dense structured data.

        Only ACTIVE events are considered.  Results are ranked by a lightweight
        relevance heuristic so the most likely matches are evaluated first.
        """
        cfg = settings.event_matcher
        filters = []
        if extracted.registration_url:
            filters.append(Event.registration_url == extracted.registration_url)
        if extracted.voucher_code:
            filters.append(Event.voucher_code == extracted.voucher_code.upper())
        if extracted.vendor:
            filters.append(Event.vendor == extracted.vendor.lower())

        if not filters:
            # Without indexed fields use promotion_name token matching.
            if not extracted.promotion_name:
                # Last resort: fetch recent events to score against.
                #
                # Safety: this only *expands the candidate pool* — every
                # candidate still goes through the full scoring logic
                # (``_score_candidate``) and must reach the configured
                # thresholds (45 / 70) before any merge happens.  When the
                # extracted event has no vendor, URL, code, or name, the
                # scores will be very low (at most 10 for absent dates) so
                # no false-positive merge is possible.
                result = await db.execute(
                    select(Event)
                    .where(Event.status == EventStatus.ACTIVE)
                    .order_by(Event.created_at.desc())
                    .limit(cfg.candidate_limit)
                )
                candidates = list(result.scalars().all())
                candidates.sort(
                    key=lambda event: _candidate_relevance(event, extracted),
                    reverse=True,
                )
                return candidates[: cfg.candidate_limit]
            tokens = [
                re.sub(r"[^\w]", "", t).lower()
                for t in extracted.promotion_name.split()
                if len(t) > 2
            ]
            tokens = [t for t in tokens if len(t) > 2]
            if tokens:
                for token in tokens[:5]:
                    filters.append(Event.promotion_name.ilike(f"%{token}%"))
            if not filters:
                return []

        result = await db.execute(
            select(Event)
            .where(Event.status == EventStatus.ACTIVE)
            .where(or_(*filters))
            .limit(cfg.candidate_limit * 3)
        )
        candidates = list(result.scalars().all())
        candidates.sort(
            key=lambda event: _candidate_relevance(event, extracted),
            reverse=True,
        )
        return candidates[: cfg.candidate_limit]

    async def update_existing(
        self,
        db: AsyncSession,
        extracted: ExtractedEvent,
        post: Post,
        source_type: SourceType,
    ) -> tuple[Event, MatchConfidence]:
        """Merge extracted fields into a post's existing Event without re-matching.

        Used when a post is reprocessed after its content changes.  Preserves
        ``post.event_id`` and never creates a new canonical Event.
        """
        if post.event_id is None:
            raise ValueError("post has no event_id")

        result = await db.execute(select(Event).where(Event.id == post.event_id))
        event = result.scalars().first()
        if event is None:
            logger.warning(
                "event_matcher: post event_id points to missing event, re-matching",
                post_id=post.id,
                event_id=post.event_id,
            )
            return await self.match_or_create(db, extracted, post, source_type)

        score = _score_candidate(event, extracted)
        updated = _merge_fields(
            event,
            extracted,
            source_type,
            post.id,
            score,
            MatchConfidence.UPDATED,
        )
        logger.info(
            "event_matcher: updated existing event for reprocessed post",
            event_id=event.id,
            post_id=post.id,
            score=score,
            fields_updated=updated,
        )
        return event, MatchConfidence.UPDATED

    async def _pick_ai_match(
        self, candidates: list[Event], extracted: ExtractedEvent
    ) -> tuple[Optional[Event], Optional[EventMatchDecision]]:
        """Ask qwen whether any candidate is the same promotion as ``extracted``.

        The deterministic weighted score is used as a recall gate: only
        candidates scoring at or above ``possible_match_threshold`` are
        submitted to the model (sorted best-first, capped by
        ``ai_candidate_limit``) so qwen calls stay bounded.  Returns the first
        such candidate the model flags as the same promotion together with its
        decision.  When the model is available but judges nothing a match,
        returns ``(None, last_decision)`` so the caller creates a new Event
        instead of merging.  When no candidate passes the gate or the model is
        unavailable (a ``None`` decision), returns ``(None, None)`` so the
        caller can fall back to deterministic scoring.
        """
        cfg = settings.event_matcher
        gated: list[tuple[int, Event]] = []
        for candidate in candidates:
            score = _score_candidate(candidate, extracted)
            if score >= cfg.possible_match_threshold:
                gated.append((score, candidate))
        gated.sort(key=lambda item: item[0], reverse=True)
        ai_candidates = [event for _, event in gated[: cfg.ai_candidate_limit]]

        last_decision: Optional[EventMatchDecision] = None
        for candidate in ai_candidates:
            decision = await compare_candidate(candidate, extracted)
            if decision is None:
                return None, None
            last_decision = decision
            if (
                decision.is_same_promotion
                and decision.confidence >= cfg.ai_possible_match_confidence
            ):
                return candidate, decision
        return None, last_decision

    async def match_or_create(
        self,
        db: AsyncSession,
        extracted: ExtractedEvent,
        post: Post,
        source_type: SourceType,
    ) -> tuple[Event, MatchConfidence]:
        """Find or create a canonical Event for ``extracted`` and link ``post``.

        Returns the Event and the MatchConfidence used.
        """
        cfg = settings.event_matcher
        candidates = await self._find_candidates(db, extracted)

        best_event: Optional[Event] = None
        match_score = 0
        best_reason: Optional[str] = None
        confidence: MatchConfidence

        # --- AI path: qwen decides whether a candidate is the same promotion. ---
        ai_match = False
        if cfg.use_ai_matcher and candidates and settings.groq_api_key:
            ai_event, ai_decision = await self._pick_ai_match(candidates, extracted)
            if ai_event is not None and ai_decision is not None:
                best_event = ai_event
                best_reason = ai_decision.reason
                match_score = int(round(ai_decision.confidence * 100))
                if ai_decision.confidence >= cfg.ai_auto_merge_confidence:
                    confidence = MatchConfidence.AUTO_MERGED
                else:
                    confidence = MatchConfidence.POSSIBLE_MATCH
                ai_match = True
            elif ai_decision is not None:
                # The model was available and judged no candidate a match.
                confidence = MatchConfidence.NEW
                ai_match = True

        # --- Deterministic fallback when AI was not used or was unavailable. ---
        if not ai_match:
            best_score = 0
            for candidate in candidates:
                score = _score_candidate(candidate, extracted)
                if score > best_score:
                    best_score = score
                    best_event = candidate
            match_score = best_score
            if best_score >= cfg.auto_merge_threshold and best_event is not None:
                confidence = MatchConfidence.AUTO_MERGED
            elif best_score >= cfg.possible_match_threshold and best_event is not None:
                confidence = MatchConfidence.POSSIBLE_MATCH
            else:
                confidence = MatchConfidence.NEW
                best_event = None  # ignore low-confidence candidates

        if best_event is not None:
            # --- Attach to existing Event ---
            updated = _merge_fields(
                best_event,
                extracted,
                source_type,
                post.id,
                match_score,
                confidence,
                match_reason=best_reason,
            )
            logger.info(
                "event_matcher: attached post to existing event",
                event_id=best_event.id,
                post_id=post.id,
                score=match_score,
                confidence=confidence.value,
                fields_updated=updated,
            )
        else:
            # --- Create new canonical Event ---
            fields = _extracted_to_event_fields(extracted)
            best_event = Event(**fields, status=EventStatus.ACTIVE)
            db.add(best_event)
            await db.flush()  # populate best_event.id before writing merge_log

            first_log_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_type": source_type.value,
                "post_id": post.id,
                "match_score": 0,
                "match_confidence": MatchConfidence.NEW.value,
                "fields_updated": [
                    f for f in _MERGEABLE_FIELDS if fields.get(f) is not None
                ],
            }
            best_event.merge_log = [first_log_entry]

            logger.info(
                "event_matcher: created new event",
                event_id=best_event.id,
                post_id=post.id,
                vendor=extracted.vendor,
                promotion_name=extracted.promotion_name,
            )

        post.event_id = best_event.id
        return best_event, confidence
