"""Tests for DB-driven scheduler dispatcher logic."""

from __future__ import annotations

import typing
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voucherbot.models.source import Source, SourceType
import voucherbot.services.dispatcher as dispatcher
from voucherbot.services.dispatcher import (
    compute_backoff_minutes,
    dispatch_tick,
    poll_interval_minutes,
    rolling_avg_runtime_ms,
    set_process_boot_at,
)


def _source(**kwargs: typing.Any) -> Source:
    defaults: dict[str, typing.Any] = dict(
        id=1,
        name="rss:test",
        type=SourceType.RSS,
        base_url="https://example.com/feed",
        enabled=True,
        priority=1,
        error_count=0,
        config={"poll_interval_minutes": 60},
        priority_tier="B",
        consecutive_failures=0,
        backoff_until=None,
        next_due_at=None,
        avg_runtime_ms=None,
    )
    defaults.update(kwargs)
    source = Source()
    for key, value in defaults.items():
        setattr(source, key, value)
    return source


class TestBackoffFormula:
    def test_first_failure(self) -> None:
        assert compute_backoff_minutes(1) == 5

    def test_exponential_growth(self) -> None:
        assert compute_backoff_minutes(2) == 10
        assert compute_backoff_minutes(3) == 20
        assert compute_backoff_minutes(4) == 40

    def test_capped_at_max(self) -> None:
        assert compute_backoff_minutes(10) == 360

    def test_zero_failures(self) -> None:
        assert compute_backoff_minutes(0) == 0


class TestPollInterval:
    def test_reads_config_interval(self) -> None:
        source = _source(config={"poll_interval_minutes": 30})
        assert poll_interval_minutes(source) == 30

    def test_falls_back_to_tier_default(self) -> None:
        source = _source(config={}, priority_tier="A")
        assert poll_interval_minutes(source) == 15

    def test_invalid_config_uses_tier(self) -> None:
        source = _source(config={"poll_interval_minutes": "bad"}, priority_tier="D")
        assert poll_interval_minutes(source) == 720

    def test_clamps_interval_below_min(self) -> None:
        source = _source(config={"poll_interval_minutes": 0})
        assert poll_interval_minutes(source) == 1

    def test_clamps_interval_below_min_negative(self) -> None:
        source = _source(config={"poll_interval_minutes": -10})
        assert poll_interval_minutes(source) == 1

    def test_clamps_interval_above_max(self) -> None:
        source = _source(config={"poll_interval_minutes": 10**15})
        assert poll_interval_minutes(source) == 43200


class TestBootAt:
    def test_set_process_boot_at_updates_value(self) -> None:
        previous = dispatcher.PROCESS_BOOT_AT
        set_process_boot_at()
        assert dispatcher.PROCESS_BOOT_AT >= previous

    def test_set_process_boot_at_accepts_explicit_value(self) -> None:
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc)
        set_process_boot_at(expected)
        assert dispatcher.PROCESS_BOOT_AT == expected


class TestRollingAverage:
    def test_first_sample(self) -> None:
        assert rolling_avg_runtime_ms(None, 1000) == 1000

    def test_averages_with_previous(self) -> None:
        assert rolling_avg_runtime_ms(800, 1200) == 1000


@pytest.mark.asyncio
async def test_dispatch_tick_returns_busy_when_lease_not_acquired() -> None:
    session = AsyncMock()
    with patch(
        "voucherbot.services.dispatcher._acquire_lease",
        new=AsyncMock(return_value=False),
    ):
        result = await dispatch_tick(session, {}, "holder-1")

    assert result == {"status": "busy"}


@pytest.mark.asyncio
async def test_dispatch_tick_returns_idle_when_no_source_due() -> None:
    session = AsyncMock()
    with (
        patch(
            "voucherbot.services.dispatcher._acquire_lease",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "voucherbot.services.dispatcher._pick_due_source",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "voucherbot.services.dispatcher._release_lease",
            new=AsyncMock(),
        ) as release,
    ):
        result = await dispatch_tick(session, {}, "holder-1")

    assert result == {"status": "idle"}
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_tick_runs_pipeline_and_marks_success() -> None:
    session = AsyncMock()
    source = _source(name="rss:example")
    pipeline_stats = {"fetched": 3, "new_posts": 1}

    with (
        patch(
            "voucherbot.services.dispatcher._acquire_lease",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "voucherbot.services.dispatcher._pick_due_source",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "voucherbot.services.dispatcher.run_pipeline_for_source",
            new=AsyncMock(return_value=pipeline_stats),
        ),
        patch(
            "voucherbot.services.dispatcher._mark_success",
            new=AsyncMock(),
        ) as mark_success,
        patch(
            "voucherbot.services.dispatcher._release_lease",
            new=AsyncMock(),
        ) as release,
    ):
        result = await dispatch_tick(session, {}, "holder-1")

    assert result["status"] == "ran"
    assert result["source"] == "rss:example"
    assert result["fetched"] == 3
    mark_success.assert_awaited_once()
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_tick_marks_failure_on_pipeline_error() -> None:
    session = AsyncMock()
    source = _source(name="rss:broken")

    with (
        patch(
            "voucherbot.services.dispatcher._acquire_lease",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "voucherbot.services.dispatcher._pick_due_source",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "voucherbot.services.dispatcher.run_pipeline_for_source",
            new=AsyncMock(side_effect=RuntimeError("collect failed")),
        ),
        patch(
            "voucherbot.services.dispatcher._mark_failure",
            new=AsyncMock(),
        ) as mark_failure,
        patch(
            "voucherbot.services.dispatcher._release_lease",
            new=AsyncMock(),
        ) as release,
    ):
        result = await dispatch_tick(session, {}, "holder-1")

    assert result["status"] == "failed"
    assert result["source"] == "rss:broken"
    assert "collect failed" in result["error"]
    mark_failure.assert_awaited_once()
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_tick_disables_unrecoverable_source() -> None:
    session = AsyncMock()
    source = _source(id=7, name="rss:gone")

    with (
        patch(
            "voucherbot.services.dispatcher._acquire_lease",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "voucherbot.services.dispatcher._pick_due_source",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "voucherbot.services.dispatcher.run_pipeline_for_source",
            new=AsyncMock(side_effect=RuntimeError("404 not found")),
        ),
        patch(
            "voucherbot.services.dispatcher._mark_unrecoverable",
            new=AsyncMock(),
        ) as mark_unrecoverable,
        patch(
            "voucherbot.services.dispatcher._release_lease",
            new=AsyncMock(),
        ) as release,
    ):
        result = await dispatch_tick(session, {}, "holder-1")

    assert result["status"] == "failed"
    assert "404 not found" in result["error"]
    # Regression guard: must pass pre-captured scalars (the ORM instance is
    # expired after rollback — reading source.id/source.name then would raise
    # "greenlet_spawn has not been called" under async SQLAlchemy).
    mark_unrecoverable.assert_awaited_once_with(session, 7, "rss:gone", "404 not found")
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_success_resets_failures_and_sets_next_due() -> None:
    from voucherbot.services.dispatcher import _mark_success

    session = AsyncMock()
    source = _source(
        consecutive_failures=2,
        backoff_until=datetime.now(timezone.utc),
        avg_runtime_ms=500,
    )

    await _mark_success(session, source, elapsed_ms=900)

    assert source.consecutive_failures == 0
    assert source.backoff_until is None
    assert source.avg_runtime_ms == 700
    assert source.next_due_at is not None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_failure_applies_backoff() -> None:
    from voucherbot.services.dispatcher import _mark_failure

    session = AsyncMock()
    source = _source(consecutive_failures=1, error_count=0)
    before = datetime.now(timezone.utc)

    await _mark_failure(session, source, elapsed_ms=1000)

    assert source.consecutive_failures == 2
    assert source.error_count == 1
    assert source.backoff_until is not None
    assert source.next_due_at == source.backoff_until
    assert source.backoff_until >= before + timedelta(minutes=10)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_pick_due_source_includes_reddit_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REDDIT_INGESTION_ENABLED=false must not stop Reddit sources from being
    picked — it only gates the PRAW API path in the collector (RSS still runs)."""
    from voucherbot.services.dispatcher import _pick_due_source

    monkeypatch.setattr(
        "voucherbot.services.dispatcher.settings.reddit_ingestion_enabled", False
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    await _pick_due_source(session)

    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "REDDIT" not in compiled.upper()
