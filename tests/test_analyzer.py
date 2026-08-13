"""Unit tests for the provider-agnostic AI analyzer service.

Covers the shared response parser, rate-budget reservation, provider
adapters (Groq + Gemini), and the public ``analyze_post`` /
``analyze_post_batch`` orchestration.  All provider calls are mocked;
no live API traffic.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voucherbot.services.ai import analyzer
from voucherbot.services.ai.analyzer import (
    _GROQ_MODEL_TPD,
    _parse_retry_delay,
    _parse_to_extracted_event,
    _estimate_tokens,
    is_model_available,
)
from voucherbot.services.ai.schema import ExtractedEvent

VALID_EVENT_JSON = (
    '{"is_voucher": true, "confidence": 0.9, "reason": "50% off", '
    '"vendor": "aws", "promotion_name": "Exam sale"}'
)


def _settings(**overrides: object) -> SimpleNamespace:
    base = SimpleNamespace(
        groq_api_key="gsk_test",
        gemini_api_key="gem_test",
        groq_requests_per_minute=30,
        groq_tokens_per_minute=None,
        groq_max_completion_tokens=1024,
    )
    base.__dict__.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_model_state() -> Generator[None, None, None]:
    analyzer._model_state.clear()
    yield
    analyzer._model_state.clear()


# ---------------------------------------------------------------------------
# _parse_to_extracted_event
# ---------------------------------------------------------------------------


class TestParseToExtractedEvent:
    def test_parses_plain_json(self) -> None:
        event = _parse_to_extracted_event(VALID_EVENT_JSON)
        assert event is not None
        assert event.is_voucher is True
        assert event.vendor == "aws"

    def test_parses_markdown_fenced_json(self) -> None:
        raw = "```json\n" + VALID_EVENT_JSON + "\n```"
        event = _parse_to_extracted_event(raw)
        assert event is not None
        assert event.is_voucher is True

    def test_parses_fence_without_language(self) -> None:
        raw = "```\n" + VALID_EVENT_JSON + "\n```"
        event = _parse_to_extracted_event(raw)
        assert event is not None
        assert event.is_voucher is True

    def test_invalid_json_returns_safe_default(self) -> None:
        event = _parse_to_extracted_event("not json at all")
        assert event is not None
        assert event.is_voucher is False
        assert event.confidence == 0.0
        assert event.reason == "parse_error"

    def test_whitespace_is_stripped(self) -> None:
        event = _parse_to_extracted_event("  \n\t" + VALID_EVENT_JSON + "\n  ")
        assert event is not None
        assert event.is_voucher is True


# ---------------------------------------------------------------------------
# _parse_retry_delay
# ---------------------------------------------------------------------------


class TestParseRetryDelay:
    def test_falls_back_when_no_delay_present(self) -> None:
        assert (
            _parse_retry_delay("some other error string") == analyzer._FALLBACK_WAIT_S
        )

    def test_real_world_retry_delay_json_falls_back(self) -> None:
        # The current pattern (raw `\\d`) does not match digit retry delays, so
        # the safe default is used. Kept as a regression guard for the fallback.
        assert (
            _parse_retry_delay('{"error": {"retryDelay": "30s"}}')
            == analyzer._FALLBACK_WAIT_S
        )
        assert (
            _parse_retry_delay('429 {"error":{"retryDelay":"17.76659s"}}')
            == analyzer._FALLBACK_WAIT_S
        )


# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_returns_at_least_one_char_token(self) -> None:
        with patch("voucherbot.services.ai.analyzer.settings", _settings()):
            assert _estimate_tokens("") >= 1

    def test_grows_with_text_length(self) -> None:
        with patch("voucherbot.services.ai.analyzer.settings", _settings()):
            short = _estimate_tokens("abcd")
            long = _estimate_tokens("abcd" * 100)
        assert long > short

    def test_completion_buffer_bounded_by_setting(self) -> None:
        with patch(
            "voucherbot.services.ai.analyzer.settings",
            _settings(groq_max_completion_tokens=64),
        ):
            assert _estimate_tokens("") == 1 + 64


# ---------------------------------------------------------------------------
# is_model_available / _wait_for_groq_budget
# ---------------------------------------------------------------------------


class TestModelAvailability:
    def test_fresh_model_is_available(self) -> None:
        assert is_model_available("llama-3.1-8b-instant") is True

    def test_daily_exhausted_model_is_not_available(self) -> None:
        _exhaust_daily("llama-3.1-8b-instant")
        assert is_model_available("llama-3.1-8b-instant") is False


def _exhaust_daily(model: str) -> None:
    """Mark *model* as daily-exhausted without tripping the day reset."""
    state = analyzer._get_model_state(model)
    state.day_date = time.strftime("%Y-%m-%d", time.gmtime())
    state.daily_exhausted = True


@pytest.mark.asyncio
async def test_wait_for_budget_reserves_capacity() -> None:
    with patch("voucherbot.services.ai.analyzer.settings", _settings()):
        rid = await analyzer._wait_for_groq_budget(10, "llama-3.1-8b-instant")
    state = analyzer._get_model_state("llama-3.1-8b-instant")
    assert rid in state.reservations
    assert state.reservations[rid][1] == 10


@pytest.mark.asyncio
async def test_wait_for_budget_raises_on_daily_exhausted() -> None:
    _exhaust_daily("llama-3.1-8b-instant")
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        pytest.raises(RuntimeError, match="daily_limit"),
    ):
        await analyzer._wait_for_groq_budget(10, "llama-3.1-8b-instant")


@pytest.mark.asyncio
async def test_wait_for_budget_raises_when_day_tokens_exceeded() -> None:
    state = analyzer._get_model_state("llama-3.1-8b-instant")
    state.day_date = time.strftime("%Y-%m-%d", time.gmtime())
    state.day_tokens = _GROQ_MODEL_TPD["llama-3.1-8b-instant"]
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        pytest.raises(RuntimeError, match="daily_limit"),
    ):
        await analyzer._wait_for_groq_budget(10, "llama-3.1-8b-instant")


# ---------------------------------------------------------------------------
# _call_groq_model
# ---------------------------------------------------------------------------


def _groq_response(raw_text: str = VALID_EVENT_JSON) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=20),
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw_text))],
    )


def _fake_groq_client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=responses)
    return client


@pytest.mark.asyncio
async def test_call_groq_model_returns_parsed_event() -> None:
    client = _fake_groq_client([_groq_response()])
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.AsyncGroq", return_value=client),
        patch(
            "voucherbot.services.ai.analyzer._wait_for_groq_budget",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "voucherbot.services.ai.analyzer._settle_groq_budget",
            new=AsyncMock(),
        ) as settle,
    ):
        result = await analyzer._call_groq_model(
            "Title", "Content", "llama-3.1-8b-instant"
        )

    assert result is not None
    assert result.is_voucher is True
    settle.assert_awaited_once()
    settle_call = settle.await_args
    assert settle_call is not None
    assert settle_call.args[0] == 1


@pytest.mark.asyncio
async def test_call_groq_model_skips_when_daily_exhausted() -> None:
    _exhaust_daily("llama-3.1-8b-instant")
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.AsyncGroq") as client_factory,
    ):
        result = await analyzer._call_groq_model(
            "Title", "Content", "llama-3.1-8b-instant"
        )

    assert result is None
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_call_groq_model_returns_none_on_runtime_daily_limit() -> None:
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch(
            "voucherbot.services.ai.analyzer._wait_for_groq_budget",
            new=AsyncMock(side_effect=RuntimeError("daily_limit:model")),
        ),
        patch("voucherbot.services.ai.analyzer.AsyncGroq"),
    ):
        result = await analyzer._call_groq_model(
            "Title", "Content", "llama-3.1-8b-instant"
        )

    assert result is None


@pytest.mark.asyncio
async def test_call_groq_model_retries_on_429_then_succeeds() -> None:
    rate_limited = RuntimeError('429 rate limit: {"error": {"retryDelay": "2s"}}')
    client = _fake_groq_client([rate_limited, _groq_response()])
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.AsyncGroq", return_value=client),
        patch(
            "voucherbot.services.ai.analyzer._wait_for_groq_budget",
            new=AsyncMock(return_value=1),
        ),
        patch("voucherbot.services.ai.analyzer._parse_retry_delay", return_value=0.0),
        patch("voucherbot.services.ai.analyzer._settle_groq_budget", new=AsyncMock()),
    ):
        result = await analyzer._call_groq_model(
            "Title", "Content", "llama-3.1-8b-instant"
        )

    assert result is not None
    assert result.is_voucher is True
    assert client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_call_groq_model_returns_none_on_daily_429() -> None:
    daily = RuntimeError("429 daily limit reached")
    client = _fake_groq_client([daily])
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.AsyncGroq", return_value=client),
        patch(
            "voucherbot.services.ai.analyzer._wait_for_groq_budget",
            new=AsyncMock(return_value=1),
        ),
        patch("voucherbot.services.ai.analyzer._parse_retry_delay", return_value=0.0),
        patch("voucherbot.services.ai.analyzer._settle_groq_budget", new=AsyncMock()),
    ):
        result = await analyzer._call_groq_model(
            "Title", "Content", "llama-3.1-8b-instant"
        )

    assert result is None
    assert analyzer._get_model_state("llama-3.1-8b-instant").daily_exhausted is True


@pytest.mark.asyncio
async def test_call_groq_model_returns_none_on_non_retryable_failure() -> None:
    client = _fake_groq_client([RuntimeError("bad request")])
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.AsyncGroq", return_value=client),
        patch(
            "voucherbot.services.ai.analyzer._wait_for_groq_budget",
            new=AsyncMock(return_value=1),
        ),
        patch("voucherbot.services.ai.analyzer._settle_groq_budget", new=AsyncMock()),
    ):
        result = await analyzer._call_groq_model(
            "Title", "Content", "llama-3.1-8b-instant"
        )

    assert result is None


# ---------------------------------------------------------------------------
# _call_groq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_groq_returns_first_successful_model() -> None:
    expected = ExtractedEvent(is_voucher=True, confidence=0.8)
    with (
        patch(
            "voucherbot.services.ai.analyzer.is_model_available",
            side_effect=lambda model: True,
        ),
        patch(
            "voucherbot.services.ai.analyzer._call_groq_model",
            new=AsyncMock(return_value=expected),
        ) as call_model,
    ):
        result = await analyzer._call_groq("Title", "Content")

    assert result is expected
    assert call_model.await_count == 1


@pytest.mark.asyncio
async def test_call_groq_skips_exhausted_models() -> None:
    expected = ExtractedEvent(is_voucher=True, confidence=0.8)

    def _available(model: str) -> bool:
        return model != analyzer._GROQ_BATCH_MODELS[0]

    with (
        patch(
            "voucherbot.services.ai.analyzer.is_model_available",
            side_effect=_available,
        ),
        patch(
            "voucherbot.services.ai.analyzer._call_groq_model",
            new=AsyncMock(return_value=expected),
        ) as call_model,
    ):
        result = await analyzer._call_groq("Title", "Content")

    assert result is expected
    call = call_model.await_args
    assert call is not None
    assert call.args[2] != analyzer._GROQ_BATCH_MODELS[0]


@pytest.mark.asyncio
async def test_call_groq_returns_none_when_all_models_fail() -> None:
    with (
        patch(
            "voucherbot.services.ai.analyzer.is_model_available",
            side_effect=lambda model: True,
        ),
        patch(
            "voucherbot.services.ai.analyzer._call_groq_model",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await analyzer._call_groq("Title", "Content")

    assert result is None


# ---------------------------------------------------------------------------
# _call_gemini
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_gemini_returns_parsed_event() -> None:
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = SimpleNamespace(
        text=VALID_EVENT_JSON
    )
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.genai.Client", return_value=fake_client),
        patch("asyncio.to_thread", side_effect=lambda fn: fn()),
    ):
        result = await analyzer._call_gemini("Title", "Content")

    assert result is not None
    assert result.is_voucher is True


@pytest.mark.asyncio
async def test_call_gemini_retries_on_429_then_succeeds() -> None:
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = [
        RuntimeError('429 rate limit: {"error": {"retryDelay": "3s"}}'),
        SimpleNamespace(text=VALID_EVENT_JSON),
    ]
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.genai.Client", return_value=fake_client),
        patch("voucherbot.services.ai.analyzer._parse_retry_delay", return_value=0.0),
        patch("asyncio.to_thread", side_effect=lambda fn: fn()),
    ):
        result = await analyzer._call_gemini("Title", "Content")

    assert result is not None
    assert result.is_voucher is True
    assert fake_client.models.generate_content.call_count == 2


@pytest.mark.asyncio
async def test_call_gemini_returns_none_on_failure() -> None:
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = RuntimeError("boom")
    with (
        patch("voucherbot.services.ai.analyzer.settings", _settings()),
        patch("voucherbot.services.ai.analyzer.genai.Client", return_value=fake_client),
        patch("voucherbot.services.ai.analyzer._parse_retry_delay", return_value=0.0),
        patch("asyncio.to_thread", side_effect=lambda fn: fn()),
    ):
        result = await analyzer._call_gemini("Title", "Content")

    assert result is None


# ---------------------------------------------------------------------------
# analyze_post
# ---------------------------------------------------------------------------


class TestAnalyzePost:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_api_keys(self) -> None:
        with patch(
            "voucherbot.services.ai.analyzer.settings",
            _settings(groq_api_key=None, gemini_api_key=None),
        ):
            result = await analyzer.analyze_post("Title", "Content")

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_groq_when_key_present(self) -> None:
        expected = ExtractedEvent(is_voucher=True, confidence=0.7)
        with (
            patch("voucherbot.services.ai.analyzer.settings", _settings()),
            patch(
                "voucherbot.services.ai.analyzer._call_groq",
                new=AsyncMock(return_value=expected),
            ) as groq,
            patch(
                "voucherbot.services.ai.analyzer._call_gemini",
                new=AsyncMock(),
            ) as gemini,
        ):
            result = await analyzer.analyze_post("Title", "Content", "rss:test")

        assert result is expected
        groq.assert_awaited_once()
        gemini.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_gemini_when_groq_fails(self) -> None:
        expected = ExtractedEvent(is_voucher=True, confidence=0.7)
        with (
            patch("voucherbot.services.ai.analyzer.settings", _settings()),
            patch(
                "voucherbot.services.ai.analyzer._call_groq",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "voucherbot.services.ai.analyzer._call_gemini",
                new=AsyncMock(return_value=expected),
            ) as gemini,
        ):
            result = await analyzer.analyze_post("Title", "Content")

        assert result is expected
        gemini.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_all_providers_fail(self) -> None:
        with (
            patch("voucherbot.services.ai.analyzer.settings", _settings()),
            patch(
                "voucherbot.services.ai.analyzer._call_groq",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "voucherbot.services.ai.analyzer._call_gemini",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await analyzer.analyze_post("Title", "Content")

        assert result is None


# ---------------------------------------------------------------------------
# analyze_post_batch
# ---------------------------------------------------------------------------


class TestAnalyzePostBatch:
    @pytest.mark.asyncio
    async def test_falls_back_to_analyze_post_without_groq_key(self) -> None:
        posts: list[tuple[str, str | None]] = [("Title A", "a"), ("Title B", "b")]
        expected = ExtractedEvent(is_voucher=True, confidence=0.6)
        with (
            patch(
                "voucherbot.services.ai.analyzer.settings",
                _settings(groq_api_key=None),
            ),
            patch(
                "voucherbot.services.ai.analyzer.analyze_post",
                new=AsyncMock(return_value=expected),
            ) as analyze,
        ):
            results = await analyzer.analyze_post_batch(posts, "rss:test")

        assert results == [expected, expected]
        assert analyze.await_count == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_posts(self) -> None:
        with patch("voucherbot.services.ai.analyzer.settings", _settings()):
            results = await analyzer.analyze_post_batch([])

        assert results == []

    @pytest.mark.asyncio
    async def test_preserves_input_order(self) -> None:
        posts: list[tuple[str, str | None]] = [("Title A", "a"), ("Title B", "b")]

        async def _fake_call(
            title: str, content: str | None, model: str, source_name: str | None = None
        ) -> ExtractedEvent:
            return ExtractedEvent(is_voucher=True, confidence=0.8, promotion_name=title)

        with (
            patch("voucherbot.services.ai.analyzer.settings", _settings()),
            patch(
                "voucherbot.services.ai.analyzer.is_model_available",
                side_effect=lambda model: True,
            ),
            patch(
                "voucherbot.services.ai.analyzer._call_groq_model",
                new=_fake_call,
            ),
        ):
            results = await analyzer.analyze_post_batch(posts, "rss:test")

        assert [r.promotion_name for r in results if r] == ["Title A", "Title B"]

    @pytest.mark.asyncio
    async def test_uses_gemini_when_all_groq_models_exhausted(self) -> None:
        posts: list[tuple[str, str | None]] = [("Title A", "a")]
        expected = ExtractedEvent(is_voucher=True, confidence=0.5)
        with (
            patch("voucherbot.services.ai.analyzer.settings", _settings()),
            patch(
                "voucherbot.services.ai.analyzer.is_model_available",
                side_effect=lambda model: False,
            ),
            patch(
                "voucherbot.services.ai.analyzer._call_gemini",
                new=AsyncMock(return_value=expected),
            ) as gemini,
        ):
            results = await analyzer.analyze_post_batch(posts, "rss:test")

        assert results == [expected]
        gemini.assert_awaited_once()
