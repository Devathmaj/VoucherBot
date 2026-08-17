"""Unit tests for the qwen-backed event matcher (event_matcher_ai).

Covers serialization, decision parsing, and the compare_candidate Groq call.
All provider calls are mocked; no live API traffic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from voucherbot.models.event import Event, EventStatus
from voucherbot.services.ai import event_matcher_ai
from voucherbot.services.ai.analyzer import _GROQ_REASONER_MODEL
from voucherbot.services.ai.event_matcher_ai import (
    compare_candidate,
    compare_events,
    _event_to_dict,
    _extracted_to_dict,
    _parse_decision,
)
from voucherbot.services.ai.schema import ExtractedEvent


VALID_DECISION_JSON = (
    '{"is_same_promotion": true, "confidence": 0.9, "reason": "same vendor"}'
)


def _settings(**overrides: object) -> SimpleNamespace:
    base = SimpleNamespace(groq_api_key="gsk_test")
    base.__dict__.update(overrides)
    return base


def _event(**kwargs: object) -> Event:
    defaults: dict[str, object] = dict(
        vendor="microsoft",
        promotion_name=None,
        promotion_type=None,
        certifications=None,
        voucher_code=None,
        discount="50%",
        registration_url=None,
        start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
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
        vendor="microsoft",
        discount="50%",
    )
    defaults.update(kwargs)
    return ExtractedEvent(**defaults)


# ---------------------------------------------------------------------------
# _event_to_dict / _extracted_to_dict
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_event_dates_become_iso(self) -> None:
        d = _event_to_dict(_event())
        assert d["vendor"] == "microsoft"
        assert d["start_date"] == "2026-08-01T00:00:00+00:00"
        assert d["end_date"] is None

    def test_extracted_passes_fields_through(self) -> None:
        ex = _extracted(vendor="microsoft", discount="50%")
        assert _extracted_to_dict(ex)["vendor"] == "microsoft"
        assert _extracted_to_dict(ex)["registration_url"] is None


# ---------------------------------------------------------------------------
# _parse_decision
# ---------------------------------------------------------------------------


class TestParseDecision:
    def test_parses_plain_json(self) -> None:
        decision = _parse_decision(VALID_DECISION_JSON)
        assert decision is not None
        assert decision.is_same_promotion is True
        assert decision.confidence == 0.9
        assert decision.reason == "same vendor"

    def test_parses_markdown_fenced_json(self) -> None:
        decision = _parse_decision("```json\n" + VALID_DECISION_JSON + "\n```")
        assert decision is not None
        assert decision.is_same_promotion is True

    def test_returns_none_on_invalid_json(self) -> None:
        assert _parse_decision("not json at all") is None

    def test_returns_none_on_wrong_schema(self) -> None:
        assert _parse_decision('{"nope": 1}') is None

    def test_clamps_confidence_on_parse(self) -> None:
        decision = _parse_decision('{"is_same_promotion": false, "confidence": 1.5}')
        assert decision is not None
        assert decision.confidence == 1.0
        decision = _parse_decision('{"is_same_promotion": false, "confidence": -0.2}')
        assert decision is not None
        assert decision.confidence == 0.0


# ---------------------------------------------------------------------------
# compare_candidate
# ---------------------------------------------------------------------------


class TestCompareCandidate:
    @pytest.mark.asyncio
    async def test_returns_none_without_groq_key(self) -> None:
        with (
            patch(
                "voucherbot.services.ai.event_matcher_ai.settings",
                _settings(groq_api_key=None),
            ),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=AsyncMock(),
            ) as call_raw,
        ):
            result = await compare_candidate(_event(), _extracted())

        assert result is None
        call_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_calls_qwen_with_both_records(self) -> None:
        candidate = _event(vendor="microsoft", discount="50%")
        extracted = _extracted(vendor="microsoft", discount="50%")
        raw = AsyncMock(return_value=VALID_DECISION_JSON)
        with (
            patch("voucherbot.services.ai.event_matcher_ai.settings", _settings()),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=raw,
            ) as call_raw,
        ):
            result = await compare_candidate(candidate, extracted)

        assert result is not None
        assert result.is_same_promotion is True
        call_raw.assert_awaited_once()
        call = call_raw.await_args
        assert call is not None
        messages = call.args[0]
        assert call.args[1] == _GROQ_REASONER_MODEL
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == event_matcher_ai._MATCH_SYSTEM_PROMPT
        assert "INCOMING (newly detected) promotion" in messages[1]["content"]
        assert '"microsoft"' in messages[1]["content"]
        assert '"50%"' in messages[1]["content"]

    @pytest.mark.asyncio
    async def test_returns_none_when_model_fails(self) -> None:
        with (
            patch("voucherbot.services.ai.event_matcher_ai.settings", _settings()),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await compare_candidate(_event(), _extracted())

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unparseable_response(self) -> None:
        with (
            patch("voucherbot.services.ai.event_matcher_ai.settings", _settings()),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=AsyncMock(return_value="garbage"),
            ),
        ):
            result = await compare_candidate(_event(), _extracted())

        assert result is None


# ---------------------------------------------------------------------------
# compare_events
# ---------------------------------------------------------------------------


class TestCompareEvents:
    @pytest.mark.asyncio
    async def test_returns_none_without_groq_key(self) -> None:
        with (
            patch(
                "voucherbot.services.ai.event_matcher_ai.settings",
                _settings(groq_api_key=None),
            ),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=AsyncMock(),
            ) as call_raw,
        ):
            result = await compare_events(_event(), _event())

        assert result is None
        call_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_calls_qwen_with_both_events(self) -> None:
        existing = _event(vendor="microsoft", discount="50%")
        incoming = _event(vendor="microsoft", discount="50%")
        raw = AsyncMock(return_value=VALID_DECISION_JSON)
        with (
            patch("voucherbot.services.ai.event_matcher_ai.settings", _settings()),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=raw,
            ) as call_raw,
        ):
            result = await compare_events(existing, incoming)

        assert result is not None
        assert result.is_same_promotion is True
        call_raw.assert_awaited_once()
        call = call_raw.await_args
        assert call is not None
        messages = call.args[0]
        assert call.args[1] == _GROQ_REASONER_MODEL
        assert messages[0]["role"] == "system"
        assert "INCOMING (newly detected) promotion" in messages[1]["content"]
        assert '"microsoft"' in messages[1]["content"]

    @pytest.mark.asyncio
    async def test_returns_none_when_model_fails(self) -> None:
        with (
            patch("voucherbot.services.ai.event_matcher_ai.settings", _settings()),
            patch(
                "voucherbot.services.ai.event_matcher_ai._call_groq_raw",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await compare_events(_event(), _event())

        assert result is None
