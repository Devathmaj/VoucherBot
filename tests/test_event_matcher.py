"""
Tests for Stage 3 EventMatcher scoring logic.

Tests are unit-level only (no DB or async I/O).  They exercise the private
scoring and merging helpers directly, and the public API via a stub that
bypasses the database candidate query.

Coverage:
  - _score_candidate: each individual scoring dimension.
  - _merge_fields: backfilling nulls, source-priority-based overwrite.
  - _dates_overlap: various overlap/non-overlap scenarios.
  - Overall confidence band assignment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voucherbot.config.settings import EventMatcherConfig, settings
from voucherbot.models.event import Event, EventStatus, MatchConfidence
from voucherbot.models.source import SourceType
from voucherbot.services.ai.event_matcher_ai import EventMatchDecision
from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.ingestion.event_matcher import (
    _dates_overlap,
    _discounts_match,
    _merge_fields,
    _normalize_discount,
    _score_candidate,
    _name_similarity,
)
from voucherbot.services.ingestion.event_matcher import EventMatcher as EventMatcherCls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(**kwargs: object) -> Event:
    defaults: dict[str, object] = dict(
        vendor=None,
        promotion_name=None,
        promotion_type=None,
        certifications=None,
        voucher_code=None,
        discount=None,
        registration_url=None,
        start_date=None,
        end_date=None,
        regions=None,
        status=EventStatus.ACTIVE,
        merge_log=[],
    )
    defaults.update(kwargs)
    return Event(**defaults)


def _extracted(**kwargs: Any) -> ExtractedEvent:
    defaults: dict[str, Any] = dict(
        is_voucher=True,
        confidence=0.9,
    )
    defaults.update(kwargs)
    return ExtractedEvent(**defaults)


def _db_mock() -> AsyncMock:
    """AsyncMock DB assigning incremental ids to newly added Events on flush."""
    created: list[Event] = []
    db = AsyncMock()

    def _add_side_effect(obj: object) -> None:
        if isinstance(obj, Event):
            created.append(obj)

    db.add = MagicMock(side_effect=_add_side_effect)

    async def _flush_side_effect() -> None:
        for idx, evt in enumerate(created, start=1):
            if evt.id is None:
                evt.id = 9000 + idx

    db.flush.side_effect = _flush_side_effect
    return db


# ---------------------------------------------------------------------------
# _score_candidate
# ---------------------------------------------------------------------------


class TestScoreCandidate:
    cfg: EventMatcherConfig = settings.event_matcher  # default EventMatcherConfig

    def test_registration_url_exact_match_scores_50(self) -> None:
        e: Event = _event(registration_url="https://learn.microsoft.com/promo")
        x: ExtractedEvent = _extracted(
            registration_url="https://learn.microsoft.com/promo"
        )
        assert _score_candidate(e, x) >= self.cfg.weight_registration_url

    def test_registration_url_utm_ignored(self) -> None:
        e: Event = _event(registration_url="https://learn.microsoft.com/promo")
        x: ExtractedEvent = _extracted(
            registration_url="https://learn.microsoft.com/promo?utm_source=tw"
        )
        score: int = _score_candidate(e, x)
        assert score >= self.cfg.weight_registration_url

    def test_voucher_code_exact_scores_40(self) -> None:
        e: Event = _event(voucher_code="AZURE50")
        x: ExtractedEvent = _extracted(voucher_code="AZURE50")
        assert _score_candidate(e, x) >= self.cfg.weight_voucher_code

    def test_voucher_code_case_insensitive(self) -> None:
        e: Event = _event(voucher_code="AZURE50")
        x: ExtractedEvent = _extracted(voucher_code="azure50")
        assert _score_candidate(e, x) >= self.cfg.weight_voucher_code

    def test_vendor_exact_scores_20(self) -> None:
        e: Event = _event(vendor="microsoft")
        x: ExtractedEvent = _extracted(vendor="microsoft")
        assert _score_candidate(e, x) >= self.cfg.weight_vendor

    def test_vendor_mismatch_scores_0(self) -> None:
        e: Event = _event(vendor="microsoft")
        x: ExtractedEvent = _extracted(vendor="amazon")
        # Only vendor differs — score should not include vendor weight.
        score: int = _score_candidate(e, x)
        assert score < self.cfg.weight_vendor

    def test_certification_overlap_scores_15(self) -> None:
        e: Event = _event(certifications=["AZ-104", "SC-300"])
        x: ExtractedEvent = _extracted(certifications=["AZ-104", "DP-203"])
        assert _score_candidate(e, x) >= self.cfg.weight_certifications

    def test_no_certification_overlap_scores_0(self) -> None:
        e: Event = _event(certifications=["AZ-104"])
        x: ExtractedEvent = _extracted(certifications=["AWS-SAA"])
        assert _score_candidate(e, x) < self.cfg.weight_certifications

    def test_discount_exact_match_scores(self) -> None:
        e: Event = _event(discount="50%")
        x: ExtractedEvent = _extracted(discount="50%")
        assert _score_candidate(e, x) >= self.cfg.weight_discount

    def test_discount_mismatch_scores_zero_for_discount(self) -> None:
        e: Event = _event(vendor="microsoft", discount="50%")
        x: ExtractedEvent = _extracted(vendor="microsoft", discount="80%")
        score: int = _score_candidate(e, x)
        assert score < self.cfg.weight_vendor + self.cfg.weight_discount

    def test_promotion_type_exact_match_scores(self) -> None:
        e: Event = _event(promotion_type="discount")
        x: ExtractedEvent = _extracted(promotion_type="discount")
        assert _score_candidate(e, x) >= self.cfg.weight_promotion_type

    def test_realistic_sparse_promotion_reaches_possible_match(self) -> None:
        e: Event = _event(vendor="microsoft", discount="50%")
        x: ExtractedEvent = _extracted(vendor="microsoft", discount="50%")
        score: int = _score_candidate(e, x)
        assert score >= self.cfg.possible_match_threshold

    def test_realistic_named_promotion_reaches_auto_merge(self) -> None:
        e: Event = _event(
            vendor="microsoft",
            promotion_name="Virtual Training Days",
            discount="50%",
        )
        x: ExtractedEvent = _extracted(
            vendor="microsoft",
            promotion_name="Virtual Training Days",
            discount="50%",
        )
        score: int = _score_candidate(e, x)
        assert score >= self.cfg.auto_merge_threshold

    def test_perfect_match_scores_max(self) -> None:
        e: Event = _event(
            registration_url="https://ms.com/promo",
            voucher_code="AZURE50",
            vendor="microsoft",
            promotion_name="AI Skills Fest",
            promotion_type="discount",
            discount="50% off",
            certifications=["AZ-900"],
            start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        x: ExtractedEvent = _extracted(
            registration_url="https://ms.com/promo",
            voucher_code="AZURE50",
            vendor="microsoft",
            promotion_name="AI Skills Fest",
            promotion_type="discount",
            discount="50% off",
            certifications=["AZ-900"],
            start_date="2026-08-01",
            end_date="2026-08-31",
        )
        cfg: EventMatcherConfig = settings.event_matcher
        max_score: int = (
            cfg.weight_registration_url
            + cfg.weight_voucher_code
            + cfg.weight_vendor
            + cfg.weight_promotion_name
            + cfg.weight_discount
            + cfg.weight_promotion_type
            + cfg.weight_certifications
            + cfg.weight_date_overlap
        )
        assert _score_candidate(e, x) == max_score

    def test_zero_if_no_shared_fields(self) -> None:
        e: Event = _event()
        x: ExtractedEvent = _extracted()
        # Date overlap logic explicitly awards points if dates are absent on both sides.
        assert _score_candidate(e, x) == self.cfg.weight_date_overlap


# ---------------------------------------------------------------------------
# _normalize_discount / _discounts_match
# ---------------------------------------------------------------------------


class TestDiscountNormalisation:
    """Discount values in different formats should normalise to the same number."""

    @pytest.mark.parametrize(
        "discount, expected",
        [
            ("50%", (50.0, "percent")),
            ("50 %", (50.0, "percent")),
            ("50percent", (50.0, "percent")),
            ("50 percent", (50.0, "percent")),
            ("50% off", (50.0, "percent")),
            ("50% exam voucher", (50.0, "percent")),
            ("50% discount", (50.0, "percent")),
            ("80%", (80.0, "percent")),
            ("$100", (100.0, "absolute")),
            ("100 USD", (100.0, "absolute")),
            ("$100 off", (100.0, "absolute")),
            ("$50.00", (50.0, "absolute")),
            ("12.5%", (12.5, "percent")),
        ],
    )
    def test_parses_known_format(
        self, discount: str, expected: tuple[float, str]
    ) -> None:
        assert _normalize_discount(discount) == expected

    @pytest.mark.parametrize(
        "discount",
        [
            None,
            "",
            "free exam",
            "N/A",
        ],
    )
    def test_returns_none_for_unparsable(self, discount: str | None) -> None:
        assert _normalize_discount(discount) is None

    @pytest.mark.parametrize(
        "a, b",
        [
            ("50%", "50 %"),
            ("50%", "50 percent"),
            ("50%", "50% off"),
            ("50%", "50% exam voucher"),
            ("50 %", "50 percent"),
            ("$100", "100 USD"),
            ("$100 off", "$100"),
            ("$100", "$100.00"),
        ],
    )
    def test_equivalent_discounts_match(self, a: str, b: str) -> None:
        assert _discounts_match(a, b)

    def test_different_percentages_do_not_match(self) -> None:
        assert not _discounts_match("50%", "80%")

    def test_different_amounts_do_not_match(self) -> None:
        assert not _discounts_match("$50", "$100")

    def test_falls_back_to_text_match_for_non_numeric(self) -> None:
        assert _discounts_match("Free", "Free")
        assert not _discounts_match("Free", "free exam")

    def test_side_effect_on__score_candidate(self) -> None:
        """Verify the discount weight is awarded for equivalent formats."""
        cfg = settings.event_matcher
        e = _event(vendor="microsoft", discount="50%")
        x = _extracted(vendor="microsoft", discount="50 percent")
        score = _score_candidate(e, x)
        assert score >= cfg.weight_vendor + cfg.weight_discount


# ---------------------------------------------------------------------------
# _name_similarity — fuzzy matching requirement
# ---------------------------------------------------------------------------


class TestNameSimilarity:
    """Promotion names should use similarity/fuzzy matching, not exact equality.

    (Requirement 7)
    """

    @pytest.mark.parametrize(
        "a, b, expected_above",
        [
            ("Microsoft Virtual Training Days", "Virtual Training Days", 0.60),
            ("Microsoft Fabric Data Days", "Fabric Data Days", 0.60),
            ("Virtual Training Days", "Microsoft Virtual Training Days", 0.60),
            ("AI Skills Fest", "Azure AI Skills Fest", 0.60),
            (
                "Microsoft Virtual Training Days",
                "Microsoft Azure Virtual Training Days",
                0.55,
            ),
            ("50% Off Azure Exam", "Azure Exam 50% Off", 0.40),
        ],
    )
    def test_fuzzy_similarity(self, a: str, b: str, expected_above: float) -> None:
        sim = _name_similarity(a, b)
        assert sim >= expected_above, f"Similarity {sim:.3f} < {expected_above}"

    def test_exact_same_name_scores_one(self) -> None:
        assert _name_similarity("Virtual Training Days", "Virtual Training Days") == 1.0

    def test_completely_different_names_score_low(self) -> None:
        assert _name_similarity("AI Skills Fest", "Something Else") > 0.0
        # The actual similarity might be > 0 due to character overlap
        # Just verify it's low (well below the 0.60 threshold)
        assert _name_similarity("AI Skills Fest", "Something Else") < 0.40


# ---------------------------------------------------------------------------
# _dates_overlap
# ---------------------------------------------------------------------------


class TestDatesOverlap:
    def _dt(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    def test_overlapping_ranges(self) -> None:
        assert _dates_overlap(
            self._dt("2026-08-01"), self._dt("2026-08-31"), "2026-08-15", "2026-09-15"
        )

    def test_non_overlapping_ranges(self) -> None:
        assert not _dates_overlap(
            self._dt("2026-01-01"), self._dt("2026-01-31"), "2026-03-01", "2026-03-31"
        )

    def test_both_none_returns_true(self) -> None:
        assert _dates_overlap(None, None, None, None)

    def test_one_side_none_returns_true(self) -> None:
        assert _dates_overlap(
            self._dt("2026-08-01"), self._dt("2026-08-31"), None, None
        )


# ---------------------------------------------------------------------------
# _merge_fields
# ---------------------------------------------------------------------------


class TestMergeFields:
    def _make_post(self) -> MagicMock:
        p = MagicMock()
        p.id = 99
        return p

    def test_backfills_null_field(self) -> None:
        e: Event = _event(end_date=None)
        x: ExtractedEvent = _extracted(end_date="2026-09-30")
        updated: list[str] = _merge_fields(
            e, x, SourceType.BLOG, 99, 80, MatchConfidence.AUTO_MERGED
        )
        # end_date should have been set on the event
        assert "end_date" in updated

    def test_does_not_overwrite_with_higher_priority_when_same(self) -> None:
        """Lower-priority source should NOT overwrite higher-priority source."""
        e: Event = _event(vendor="microsoft")
        # Simulate the event's vendor was set by a BLOG (priority 2)
        e.merge_log = [
            {
                "source_type": "BLOG",
                "fields_updated": ["vendor"],
                "timestamp": "",
                "post_id": 1,
                "match_score": 0,
                "match_confidence": "NEW",
            }
        ]
        x: ExtractedEvent = _extracted(vendor="MICROSOFT")
        # REDDIT has lower priority than BLOG
        updated: list[str] = _merge_fields(
            e, x, SourceType.REDDIT, 100, 50, MatchConfidence.AUTO_MERGED
        )
        assert "vendor" not in updated

    def test_overwrites_with_higher_priority_source(self) -> None:
        """Higher-priority source SHOULD overwrite lower-priority source."""
        e: Event = _event(vendor="microsoft")
        e.merge_log = [
            {
                "source_type": "REDDIT",
                "fields_updated": ["vendor"],
                "timestamp": "",
                "post_id": 1,
                "match_score": 0,
                "match_confidence": "NEW",
            }
        ]
        x: ExtractedEvent = _extracted(vendor="microsoft")
        # WEBSITE has higher priority than REDDIT
        updated: list[str] = _merge_fields(
            e, x, SourceType.WEBSITE, 100, 80, MatchConfidence.AUTO_MERGED
        )
        assert "vendor" in updated

    def test_appends_audit_log_entry(self) -> None:
        e: Event = _event(voucher_code=None)
        x: ExtractedEvent = _extracted(voucher_code="CODE99")
        assert e.merge_log == []
        _merge_fields(e, x, SourceType.RSS, 55, 70, MatchConfidence.AUTO_MERGED)
        assert len(e.merge_log) == 1
        entry = e.merge_log[0]
        assert entry["source_type"] == "RSS"
        assert "voucher_code" in entry["fields_updated"]


# ---------------------------------------------------------------------------
# Confidence band assignment (integration-level, no DB)
# ---------------------------------------------------------------------------


class TestConfidenceBands:
    """Verify score thresholds map to correct MatchConfidence values."""

    @pytest.mark.asyncio
    async def test_auto_merge_threshold(self) -> None:

        cfg: EventMatcherConfig = settings.event_matcher

        # Construct a candidate Event whose score will hit >= auto_merge_threshold.
        candidate: Event = _event(
            id=1,
            registration_url="https://ms.com/promo",
            voucher_code="AZURE50",
        )
        extracted: ExtractedEvent = _extracted(
            registration_url="https://ms.com/promo",
            voucher_code="AZURE50",
        )

        # Score: registration_url (50) + voucher_code (40) = 90 >= 70
        score: int = _score_candidate(candidate, extracted)
        assert score >= cfg.auto_merge_threshold

    @pytest.mark.asyncio
    async def test_possible_match_band(self) -> None:
        cfg: EventMatcherConfig = settings.event_matcher
        candidate: Event = _event(
            id=1, vendor="microsoft", promotion_name="AI Skills Fest"
        )
        extracted: ExtractedEvent = _extracted(
            vendor="microsoft", promotion_name="AI Skills Fest"
        )

        # vendor (20) + name (25) + dates (10) = 55 >= possible_match (45)
        score: int = _score_candidate(candidate, extracted)
        assert score >= cfg.possible_match_threshold
        assert score < cfg.auto_merge_threshold

    def test_new_event_below_possible_match(self) -> None:
        cfg: EventMatcherConfig = settings.event_matcher
        candidate: Event = _event(id=1, vendor="microsoft")
        extracted: ExtractedEvent = _extracted(vendor="amazon")  # no match
        score: int = _score_candidate(candidate, extracted)
        assert score < cfg.possible_match_threshold


# ---------------------------------------------------------------------------
# Integration scenarios (A–D) — no database required
# ---------------------------------------------------------------------------
# These tests verify the full EventMatcher flow using mocks for the async
# database session.  They exercise match_or_create, update_existing, and the
# interaction between scoring, candidate retrieval, and event creation/update.


@pytest.fixture
def matcher() -> EventMatcherCls:
    return EventMatcherCls()


class TestScenarioA:
    """Two different posts describing the same promotion share one Event.

    Process Post A  →  Event X is created.
    Process Post B  →  No second Event created; both posts reference Event X.
    """

    @pytest.mark.asyncio
    async def test_two_posts_same_promotion_share_event(
        self, matcher: EventMatcherCls
    ) -> None:
        # Simulate DB flush assigning an ID to newly created Events.
        _created_events: list[Event] = []
        db = AsyncMock()

        def _add_side_effect(obj: object) -> None:
            if isinstance(obj, Event):
                _created_events.append(obj)

        db.add.side_effect = _add_side_effect

        async def _flush_side_effect() -> None:
            for evt in _created_events:
                if evt.id is None:
                    evt.id = 100

        db.flush.side_effect = _flush_side_effect

        # --- Post A: first encounter → new Event created ---
        # Simulate no candidates found.
        with patch.object(matcher, "_find_candidates", return_value=[]):
            post_a = MagicMock()
            post_a.id = 1
            post_a.event_id = None
            extracted_a = _extracted(
                vendor="microsoft",
                promotion_name="Virtual Training Days",
                discount="50%",
            )

            event_a, confidence_a = await matcher.match_or_create(
                db, extracted_a, post_a, SourceType.RSS
            )

        assert confidence_a == MatchConfidence.NEW
        assert post_a.event_id == event_a.id
        created_id = event_a.id

        # --- Post B: same promotion → should match existing Event ---
        with (
            patch.object(matcher, "_find_candidates", return_value=[event_a]),
            patch(
                "voucherbot.services.ingestion.event_matcher.compare_candidate",
                new=AsyncMock(
                    return_value=EventMatchDecision(
                        is_same_promotion=True, confidence=0.95
                    )
                ),
            ),
        ):
            post_b = MagicMock()
            post_b.id = 2
            post_b.event_id = None
            extracted_b = _extracted(
                vendor="microsoft",
                promotion_name="Virtual Training Days",
                discount="50%",
            )

            event_b, confidence_b = await matcher.match_or_create(
                db, extracted_b, post_b, SourceType.BLOG
            )

        # Same Event reused — no duplicate created.
        assert event_b.id == created_id
        assert confidence_b == MatchConfidence.AUTO_MERGED
        assert post_b.event_id == created_id


class TestScenarioB:
    """Reprocessing a post updates its Event without creating a new one.

    Post A is reprocessed → Event X is updated.
    Event ID unchanged, no additional Event rows.
    """

    @pytest.mark.asyncio
    async def test_reprocess_updates_event_in_place(
        self, matcher: EventMatcherCls
    ) -> None:
        # Create an Event directly (simulating the result of first processing).
        event = _event(
            id=200,
            vendor="microsoft",
            promotion_name="Virtual Training Days",
            discount="50%",
        )
        event.merge_log = [
            {
                "source_type": "RSS",
                "post_id": 10,
                "fields_updated": ["vendor", "promotion_name", "discount"],
                "timestamp": "2026-01-01T00:00:00+00:00",
                "match_score": 0,
                "match_confidence": "NEW",
            }
        ]
        original_id = event.id

        # Now reprocess: simulate the event already exists with updated extraction.
        reprocessed_post = MagicMock()
        reprocessed_post.id = 10
        reprocessed_post.event_id = original_id  # already linked

        # Mock the DB load that update_existing performs.
        db = AsyncMock()

        async def _execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            mock_result = MagicMock()
            mock_result.scalars.return_value.first.return_value = event
            return mock_result

        db.execute.side_effect = _execute_side_effect

        improved_extracted = _extracted(
            vendor="microsoft",
            promotion_name="Virtual Training Days – Updated",
            discount="50%",
            promotion_type="discount",
        )

        updated_event, confidence = await matcher.update_existing(
            db, improved_extracted, reprocessed_post, SourceType.RSS
        )

        # Event identity preserved.
        assert updated_event.id == original_id
        # Confidence is UPDATED — not NEW, not AUTO_MERGED.
        assert confidence == MatchConfidence.UPDATED
        # Fields updated (promotion_name improved via source-priority merge).
        assert updated_event.promotion_name is not None
        # No second Event was created — the same object was updated.
        assert reprocessed_post.event_id == original_id


class TestScenarioC:
    """Equivalent discount formats produce the same match score."""

    @pytest.mark.parametrize(
        "event_discount, extracted_discount",
        [
            ("50%", "50 %"),
            ("50%", "50 percent"),
            ("50%", "50% off"),
            ("$100", "100 USD"),
        ],
    )
    def test_equivalent_discounts_contribute_to_score(
        self, event_discount: str, extracted_discount: str
    ) -> None:
        cfg = settings.event_matcher
        e = _event(vendor="microsoft", discount=event_discount)
        x = _extracted(vendor="microsoft", discount=extracted_discount)
        score = _score_candidate(e, x)
        # Vendor (20) + discount (20) = 40
        assert score >= cfg.weight_vendor + cfg.weight_discount


class TestScenarioD:
    """Different promotions or significantly different discounts do NOT merge."""

    def test_different_promotions_do_not_merge_by_accident(self) -> None:
        cfg = settings.event_matcher
        e = _event(vendor="microsoft", promotion_name="AI Skills Fest", discount="50%")
        x = _extracted(
            vendor="oracle", promotion_name="Oracle Cloud Conference", discount="80%"
        )
        score = _score_candidate(e, x)
        # Different vendor, different name, different discount.
        # Only date_overlap (10) may contribute if both dates are absent.
        assert score < cfg.possible_match_threshold

    def test_same_vendor_different_discount_prevents_auto_merge(self) -> None:
        """Same vendor + similar name but very different discount should stay
        below auto_merge to avoid incorrect merges."""
        cfg = settings.event_matcher
        e = _event(
            vendor="microsoft",
            promotion_name="50% Off Azure Exam",
            discount="50%",
        )
        x = _extracted(
            vendor="microsoft",
            promotion_name="80% Off Azure Exam",
            discount="80%",
        )
        score = _score_candidate(e, x)
        # vendor (20) + name_similarity (25) + dates (10) ≈ 55
        # Discount mismatch: 50% vs 80% → 0 points for discount
        assert score >= cfg.possible_match_threshold  # should be possible match
        assert score < cfg.auto_merge_threshold  # should NOT auto-merge

    def test_different_vendors_same_name_does_not_reach_auto_merge(self) -> None:
        """Same promotion name but different vendors should not auto-merge."""
        cfg = settings.event_matcher
        e = _event(vendor="microsoft", promotion_name="Virtual Training Days")
        x = _extracted(vendor="google", promotion_name="Virtual Training Days")
        score = _score_candidate(e, x)
        # name (25) + dates (10) = 35 — below possible_match
        # No vendor match (different vendors)
        assert score < cfg.possible_match_threshold

    def test_generic_names_same_vendor_no_false_merge(self) -> None:
        """Generic names like 'Student Discount' vs 'Certification Discount'
        should NOT merge even when they share the same vendor, because the
        name similarity (~0.58) is below the 0.60 threshold."""
        cfg = settings.event_matcher
        e = _event(
            vendor="microsoft",
            promotion_name="Student Discount",
            discount="50%",
        )
        x = _extracted(
            vendor="microsoft",
            promotion_name="Certification Discount",
            discount="80%",
        )
        score = _score_candidate(e, x)
        # vendor (20) + dates (10) = 30 — below possible_match
        # Name similarity ≈ 0.58 < 0.60 → no name bonus
        # Discount mismatch (50% vs 80%) → no discount bonus
        assert score < cfg.possible_match_threshold

    def test_no_shared_fields_stays_below_threshold(self) -> None:
        """When the extracted event has no meaningful fields (no vendor, name,
        discount, URL, code, type, or certs), it cannot accidentally merge."""
        cfg = settings.event_matcher
        e = _event(
            vendor="microsoft",
            promotion_name="Virtual Training Days",
            discount="50%",
            promotion_type="discount",
        )
        x = _extracted()  # only is_voucher=True, confidence=0.9
        score = _score_candidate(e, x)
        # Only dates_absent (10) contributes.
        assert score == cfg.weight_date_overlap


# ---------------------------------------------------------------------------
# AI-backed matching (qwen decides whether two promotions are the same)
# ---------------------------------------------------------------------------


class TestAIMatching:
    """Stage 3 AI path: the qwen decision replaces the weighted score."""

    async def _match(
        self,
        matcher: EventMatcherCls,
        candidates: list[Event],
        extracted: ExtractedEvent,
        decision: EventMatchDecision | None,
    ) -> tuple[Event, MatchConfidence, AsyncMock, AsyncMock]:
        db = _db_mock()
        post = MagicMock()
        post.id = 7
        post.event_id = None
        with (
            patch.object(matcher, "_find_candidates", return_value=candidates),
            patch(
                "voucherbot.services.ingestion.event_matcher.compare_candidate",
                new=AsyncMock(return_value=decision),
            ) as compare,
            patch.object(settings, "groq_api_key", "gsk_test"),
        ):
            event, confidence = await matcher.match_or_create(
                db, extracted, post, SourceType.BLOG
            )
        return event, confidence, db, compare

    @pytest.mark.asyncio
    async def test_ai_high_confidence_match_auto_merges(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(id=1, vendor="microsoft", discount="50%")
        extracted = _extracted(vendor="microsoft", discount="50%")
        decision = EventMatchDecision(
            is_same_promotion=True, confidence=0.95, reason="same promo"
        )

        event, confidence, db, compare = await self._match(
            matcher, [candidate], extracted, decision
        )

        assert confidence == MatchConfidence.AUTO_MERGED
        assert event.id == candidate.id
        merge_log = cast(list[Any], event.merge_log or [])
        assert merge_log and merge_log[-1]["reason"] == "same promo"
        compare.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ai_low_confidence_match_flags_possible(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(id=1, vendor="microsoft", discount="50%")
        extracted = _extracted(vendor="microsoft", discount="50%")
        decision = EventMatchDecision(is_same_promotion=True, confidence=0.6)

        event, confidence, _, compare = await self._match(
            matcher, [candidate], extracted, decision
        )

        assert confidence == MatchConfidence.POSSIBLE_MATCH
        assert event.id == candidate.id

    @pytest.mark.asyncio
    async def test_ai_uncertain_match_creates_new_event(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(id=1, vendor="microsoft", discount="50%")
        extracted = _extracted(vendor="microsoft", discount="50%")
        decision = EventMatchDecision(is_same_promotion=True, confidence=0.3)

        event, confidence, _, compare = await self._match(
            matcher, [candidate], extracted, decision
        )

        assert confidence == MatchConfidence.NEW
        assert event.id != candidate.id

    @pytest.mark.asyncio
    async def test_ai_confident_no_match_creates_new_event(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(id=1, vendor="microsoft", discount="50%")
        extracted = _extracted(vendor="microsoft", discount="50%")
        decision = EventMatchDecision(is_same_promotion=False, confidence=0.9)

        event, confidence, _, compare = await self._match(
            matcher, [candidate], extracted, decision
        )

        assert confidence == MatchConfidence.NEW
        assert event.id != candidate.id

    @pytest.mark.asyncio
    async def test_ai_unavailable_falls_back_to_scoring(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(
            id=1,
            registration_url="https://ms.com/promo",
            voucher_code="AZURE50",
        )
        extracted = _extracted(
            registration_url="https://ms.com/promo",
            voucher_code="AZURE50",
        )

        event, confidence, _, compare = await self._match(
            matcher, [candidate], extracted, None
        )

        assert compare.await_count == 1  # attempted, but the model failed
        assert confidence == MatchConfidence.AUTO_MERGED
        assert event.id == candidate.id
        merge_log = cast(list[Any], event.merge_log or [])
        assert merge_log and "reason" not in (merge_log[-1] or {})

    @pytest.mark.asyncio
    async def test_ai_unavailable_fallback_does_not_merge_sparse(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(id=1, vendor="microsoft")
        extracted = _extracted(vendor="amazon")

        event, confidence, _, _ = await self._match(
            matcher, [candidate], extracted, None
        )

        assert confidence == MatchConfidence.NEW
        assert event.id != candidate.id

    @pytest.mark.asyncio
    async def test_ai_skips_candidates_below_deterministic_threshold(
        self, matcher: EventMatcherCls
    ) -> None:
        candidate = _event(id=1, vendor="microsoft")
        extracted = _extracted(vendor="amazon")  # deterministic score = 10 < 45

        event, confidence, _, compare = await self._match(
            matcher,
            [candidate],
            extracted,
            EventMatchDecision(is_same_promotion=True, confidence=0.95),
        )

        assert confidence == MatchConfidence.NEW
        compare.assert_not_awaited()  # gate keeps the model call bounded

    @pytest.mark.asyncio
    async def test_no_candidates_skips_ai(self, matcher: EventMatcherCls) -> None:
        extracted = _extracted(vendor="microsoft")

        event, confidence, _, compare = await self._match(
            matcher,
            [],
            extracted,
            EventMatchDecision(is_same_promotion=True, confidence=0.95),
        )

        assert confidence == MatchConfidence.NEW
        compare.assert_not_awaited()
