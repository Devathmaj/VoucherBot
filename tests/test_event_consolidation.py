"""Unit tests for the periodic event-consolidation sweep.

Covers candidate-pair discovery, merge decisioning (AI-confirmed, deterministic
fallback, and budget cap), field folding + post repointing, and the entry-point
guards.  No database is used; sessions and provider calls are mocked.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.config.settings import EventConsolidationConfig
from voucherbot.models.event import Event, EventStatus, MatchConfidence
from voucherbot.models.source import SourceType
from voucherbot.services import event_consolidation
from voucherbot.services.ai.event_matcher_ai import EventMatchDecision
from voucherbot.services.event_consolidation import (
    _apply_merge,
    _candidate_pairs,
    _choose_survivor,
    _discover_merges,
    _event_to_extracted,
    _origin_source,
    _pair_score,
    consolidate_events,
)


def _event(**kwargs: object) -> Event:
    defaults: dict[str, object] = dict(
        id=1,
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


def _code_pair(a_id: int, b_id: int) -> tuple[Event, Event]:
    """Two Events that deterministically score high (code + vendor + discount)."""
    return (
        _event(
            id=a_id,
            vendor="microsoft",
            voucher_code="AZ900-50",
            discount="50%",
        ),
        _event(
            id=b_id,
            vendor="microsoft",
            voucher_code="AZ900-50",
            discount="50%",
        ),
    )


def _same(conf: float = 0.9, reason: str = "same vendor") -> EventMatchDecision:
    return EventMatchDecision(is_same_promotion=True, confidence=conf, reason=reason)


# ---------------------------------------------------------------------------
# _candidate_pairs
# ---------------------------------------------------------------------------


class TestCandidatePairs:
    def test_groups_by_normalised_url(self) -> None:
        a = _event(id=1, registration_url="https://microsoft.com/voucher?utm_source=x")
        b = _event(id=2, registration_url="http://MICROSOFT.com/voucher")
        assert (a, b) in _candidate_pairs([a, b])

    def test_groups_by_code_case_insensitive(self) -> None:
        a = _event(id=1, voucher_code="AZ900-50")
        b = _event(id=2, voucher_code="az900-50")
        assert (a, b) in _candidate_pairs([a, b])

    def test_groups_by_vendor(self) -> None:
        a = _event(id=1, vendor="Microsoft")
        b = _event(id=2, vendor="microsoft")
        assert (a, b) in _candidate_pairs([a, b])

    def test_dedupes_cross_signal_pairs(self) -> None:
        a = _event(
            id=1,
            vendor="microsoft",
            voucher_code="AZ900-50",
            registration_url="https://microsoft.com/voucher",
        )
        b = _event(
            id=2,
            vendor="microsoft",
            voucher_code="AZ900-50",
            registration_url="https://microsoft.com/voucher",
        )
        assert _candidate_pairs([a, b]) == [(a, b)]

    def test_ignores_na_voucher_codes(self) -> None:
        a = _event(id=1, voucher_code="N/A")
        b = _event(id=2, voucher_code="N/A")
        assert _candidate_pairs([a, b]) == []

    def test_no_pairs_without_shared_signal(self) -> None:
        a = _event(id=1, vendor="microsoft")
        b = _event(id=2, vendor="amazon")
        assert _candidate_pairs([a, b]) == []

    def test_bucket_sample_cap(self) -> None:
        events = [_event(id=i, vendor="microsoft") for i in range(1, 6)]
        with patch.object(event_consolidation, "_MAX_BUCKET_SAMPLE", 3):
            pairs = _candidate_pairs(events)
        assert len(pairs) == 3  # C(3, 2) from the first three events


# ---------------------------------------------------------------------------
# _event_to_extracted / _origin_source
# ---------------------------------------------------------------------------


class TestEventProjection:
    def test_projects_iso_dates(self) -> None:
        from datetime import datetime, timezone as tz

        event = _event(
            id=1,
            vendor="microsoft",
            start_date=datetime(2026, 8, 1, tzinfo=tz.utc),
            regions=["US"],
        )
        ex = _event_to_extracted(event)
        assert ex.is_voucher is True
        assert ex.confidence == 1.0
        assert ex.vendor == "microsoft"
        assert ex.start_date == "2026-08-01T00:00:00+00:00"
        assert ex.end_date is None
        assert ex.regions == ["US"]


class TestOriginSource:
    def test_uses_creation_source(self) -> None:
        event = _event(
            merge_log=[
                {"source_type": "BLOG", "fields_updated": ["vendor"]},
                {"source_type": "WEBSITE", "fields_updated": ["end_date"]},
            ]
        )
        assert _origin_source(event) == SourceType.BLOG

    def test_skips_unreadable_entries(self) -> None:
        event = _event(
            merge_log=[
                {"source_type": "UNKNOWN", "fields_updated": []},
                {"source_type": "RSS", "fields_updated": []},
            ]
        )
        assert _origin_source(event) == SourceType.RSS

    def test_falls_back_to_rss(self) -> None:
        assert _origin_source(_event()) == SourceType.RSS


# ---------------------------------------------------------------------------
# _choose_survivor
# ---------------------------------------------------------------------------


class TestChooseSurvivor:
    def test_more_posts_wins(self) -> None:
        a = _event(id=1)
        b = _event(id=2)
        survivor, loser = _choose_survivor(a, b, {1: 1, 2: 5})
        assert survivor is b
        assert loser is a

    def test_tie_keeps_older_event(self) -> None:
        a = _event(id=1)
        b = _event(id=2)
        survivor, loser = _choose_survivor(a, b, {1: 2, 2: 2})
        assert survivor is a
        assert loser is b


# ---------------------------------------------------------------------------
# _discover_merges
# ---------------------------------------------------------------------------


class TestDiscoverMerges:
    @pytest.mark.asyncio
    async def test_ai_confirms_merges_preferring_high_post_count(self) -> None:
        a, b = _code_pair(1, 2)
        compare = AsyncMock(return_value=_same())
        merges, ai_calls, pairs, gated = await _discover_merges(
            [a, b], {1: 2, 2: 1}, compare
        )
        assert ai_calls == 1
        assert pairs == 1
        assert gated == 1
        assert merges == [(a, b, 90, "same vendor")]

    @pytest.mark.asyncio
    async def test_ai_says_different_skips(self) -> None:
        a, b = _code_pair(1, 2)
        compare = AsyncMock(
            return_value=EventMatchDecision(
                is_same_promotion=False, confidence=0.9, reason="different certs"
            )
        )
        merges, ai_calls, *_ = await _discover_merges([a, b], {}, compare)
        assert merges == []
        assert ai_calls == 1

    @pytest.mark.asyncio
    async def test_ai_same_but_low_confidence_skips(self) -> None:
        a, b = _code_pair(1, 2)
        compare = AsyncMock(return_value=_same(conf=0.4))
        merges, ai_calls, *_ = await _discover_merges([a, b], {}, compare)
        assert merges == []
        assert ai_calls == 1

    @pytest.mark.asyncio
    async def test_model_unavailable_falls_back_deterministically(self) -> None:
        a, b = _code_pair(1, 2)
        compare = AsyncMock(return_value=None)
        merges, ai_calls, *_ = await _discover_merges([a, b], {1: 1}, compare)
        assert ai_calls == 1
        # code 40 + vendor 20 + discount 20 + absent-dates overlap 10 = 90.
        assert merges == [(a, b, 90, None)]

    @pytest.mark.asyncio
    async def test_model_unavailable_below_deterministic_floor_skips(self) -> None:
        a = _event(id=1, vendor="microsoft", promotion_name="AI Skills Fest")
        b = _event(id=2, vendor="microsoft", promotion_name="AI Skills Fest")
        compare = AsyncMock(return_value=None)
        merges, ai_calls, *_ = await _discover_merges([a, b], {}, compare)
        assert merges == []
        assert ai_calls == 1

    @pytest.mark.asyncio
    async def test_ai_budget_cap_limits_calls(self) -> None:
        events = [_code_pair(i, i + 1)[0] for i in range(1, 4)]
        compare = AsyncMock(return_value=_same())
        cfg = EventConsolidationConfig(max_ai_calls_per_sweep=1)
        with patch.object(event_consolidation.settings, "consolidation", cfg):
            merges, ai_calls, *_ = await _discover_merges(events, {}, compare)
        assert ai_calls == 1
        assert len(merges) == 1

    @pytest.mark.asyncio
    async def test_absorbed_events_not_double_merged(self) -> None:
        events = [
            _event(id=i, vendor="microsoft", voucher_code="AZ900-50", discount="50%")
            for i in range(1, 5)
        ]
        compare = AsyncMock(return_value=_same())
        merges, *_ = await _discover_merges(events, {}, compare)
        # With equal scores the sweep merges (1,2) and (3,4); absorbed events
        # are never folded into a second target.
        assert len(merges) == 2
        involved: set[int] = set()
        for survivor, loser, *_ in merges:
            assert loser.id not in involved
            involved.add(survivor.id)
            involved.add(loser.id)
        assert {loser.id for _, loser, *_ in merges} == {2, 4}


# ---------------------------------------------------------------------------
# _apply_merge
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, rowcount: int = 3) -> None:
        self.rowcount = rowcount
        self.execute_calls: list[tuple[Any, Any]] = []

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.execute_calls.append((stmt, params))
        return SimpleNamespace(rowcount=self.rowcount)


class TestApplyMerge:
    @pytest.mark.asyncio
    async def test_merges_fields_repoints_posts_and_archives(self) -> None:
        survivor = _event(
            id=1,
            vendor="microsoft",
            merge_log=[{"source_type": "RSS", "fields_updated": ["vendor"]}],
        )
        loser = _event(
            id=2,
            vendor="microsoft",
            voucher_code="AZ900-50",
            discount="50%",
            merge_log=[{"source_type": "BLOG", "fields_updated": ["discount"]}],
        )
        session = _FakeSession(rowcount=3)

        rowcount = await _apply_merge(
            cast(AsyncSession, session), survivor, loser, 90, "same vendor"
        )

        assert rowcount == 3
        assert len(session.execute_calls) == 1
        stmt, params = session.execute_calls[0]
        assert (
            str(stmt)
            == "UPDATE posts SET event_id = :survivor_id WHERE event_id = :loser_id"
        )
        assert params == {"survivor_id": 1, "loser_id": 2}

        assert loser.status == EventStatus.ARCHIVED
        survivor_log = survivor.merge_log
        assert survivor_log is not None
        survivor_entry = survivor_log[-1]
        assert survivor_entry["source_type"] == "BLOG"
        assert survivor_entry["post_id"] == 2
        assert survivor_entry["match_score"] == 90
        assert survivor_entry["match_confidence"] == MatchConfidence.AUTO_MERGED.value
        assert survivor_entry["reason"] == "same vendor"
        assert survivor.voucher_code == "AZ900-50"

        loser_log = loser.merge_log
        assert loser_log is not None
        assert loser_log[-1]["reason"] == "consolidated into event 1"


# ---------------------------------------------------------------------------
# consolidate_events entry-point guards
# ---------------------------------------------------------------------------


class TestConsolidateEvents:
    @pytest.mark.asyncio
    async def test_disabled_returns_zeros(self) -> None:
        cfg = EventConsolidationConfig(enabled=False)
        with patch.object(event_consolidation.settings, "consolidation", cfg):
            stats = await consolidate_events()
        assert stats == {
            "candidate_pairs": 0,
            "gated_pairs": 0,
            "ai_calls": 0,
            "merged": 0,
            "posts_repointed": 0,
        }

    @pytest.mark.asyncio
    async def test_throttled_returns_zeros(self) -> None:
        with patch.object(event_consolidation, "_last_merge_ts", time.monotonic()):
            stats = await consolidate_events()
        assert stats["merged"] == 0


# ---------------------------------------------------------------------------
# _pair_score sanity
# ---------------------------------------------------------------------------


class TestPairScore:
    def test_matches_within_deterministic_bands(self) -> None:
        a, b = _code_pair(1, 2)
        # code 40 + vendor 20 + discount 20 + absent-dates overlap 10 = 90.
        assert _pair_score(a, b) == 90
        assert _pair_score(b, a) == 90
