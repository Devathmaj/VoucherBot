"""Unit tests for startup bootstrap seed data and resilience helpers.

Covers source/keyword definition builders, selector validation, transient-
error classification, the exponential-backoff retry wrapper, and the
advisory-lock behaviour of ``bootstrap_data``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg.exceptions import (  # type: ignore[import-untyped]
    ConnectionDoesNotExistError,
    InterfaceError,
)
from sqlalchemy.exc import DBAPIError, IntegrityError

from voucherbot.database import bootstrap
from voucherbot.models.source import SourceType


# ---------------------------------------------------------------------------
# Reddit tier / source naming
# ---------------------------------------------------------------------------


class TestRedditTier:
    def test_tier_a_subreddits(self) -> None:
        assert bootstrap._reddit_tier("AWSCertifications") == "A"

    def test_other_subreddits_are_tier_b(self) -> None:
        assert bootstrap._reddit_tier("CompTIA") == "B"


class TestSourceName:
    def test_slugs_label(self) -> None:
        assert bootstrap._source_name(SourceType.RSS, "Tutorials Dojo") == (
            "rss:tutorials_dojo"
        )

    def test_normalises_ampersand_and_spaces(self) -> None:
        assert bootstrap._source_name(SourceType.WEBSITE, "Red Hat & Linux") == (
            "website:red_hat_and_linux"
        )


# ---------------------------------------------------------------------------
# _feed / _page builders
# ---------------------------------------------------------------------------


class TestFeedBuilder:
    def test_basic_feed(self) -> None:
        feed = bootstrap._feed(
            "AWS Blog", "https://aws.com/feed", SourceType.BLOG, vendor="AWS"
        )
        assert feed["name"] == "blog:aws_blog"
        assert feed["type"] == SourceType.BLOG
        assert feed["base_url"] == "https://aws.com/feed"
        assert feed["enabled"] is True
        assert feed["config"]["feed_url"] == "https://aws.com/feed"
        assert feed["config"]["vendor"] == "AWS"
        assert feed["config"]["query_terms"] == bootstrap.DEFAULT_QUERY_TERMS
        assert (
            feed["config"]["poll_interval_minutes"]
            == bootstrap._TIER_CADENCE_MINUTES["B"]
        )

    def test_unsupported_feed_is_disabled(self) -> None:
        feed = bootstrap._feed(
            "Reg",
            "https://reg.com/feed",
            SourceType.RSS,
            unsupported=True,
            unsupported_reason="Anti-bot",
        )
        assert feed["enabled"] is False
        assert feed["config"]["unsupported"] is True
        assert feed["config"]["unsupported_reason"] == "Anti-bot"

    def test_cadence_override(self) -> None:
        feed = bootstrap._feed(
            "X", "https://x.com/feed", SourceType.RSS, cadence_minutes=5
        )
        assert feed["config"]["poll_interval_minutes"] == 5


class TestPageBuilder:
    def test_basic_page(self) -> None:
        page = bootstrap._page(
            "Cisco Live", "https://cisco.com/live", SourceType.EVENT, vendor="Cisco"
        )
        assert page["name"] == "event:cisco_live"
        assert page["type"] == SourceType.EVENT
        assert page["enabled"] is True
        assert page["config"]["url"] == "https://cisco.com/live"
        assert page["config"]["article_selector"]
        assert (
            page["config"]["poll_interval_minutes"]
            == bootstrap._TIER_CADENCE_MINUTES["D"]
        )

    def test_note_selector_in_config(self) -> None:
        page = bootstrap._page(
            "MSFTHub",
            "https://msfthub.com/",
            SourceType.WEBSITE,
            note_selector=".badge",
        )
        assert page["config"]["note_selector"] == ".badge"


# ---------------------------------------------------------------------------
# _warn_on_invalid_selectors
# ---------------------------------------------------------------------------


class TestWarnOnInvalidSelectors:
    def test_valid_selectors_do_not_warn(self) -> None:
        with patch("voucherbot.database.bootstrap.logger.warning") as warn:
            bootstrap._warn_on_invalid_selectors(
                {"article_selector": "main li", "title_selector": "h2"}, "s1"
            )
        warn.assert_not_called()

    def test_malformed_selector_warns(self) -> None:
        with patch("voucherbot.database.bootstrap.logger.warning") as warn:
            bootstrap._warn_on_invalid_selectors(
                {"article_selector": "[[unclosed", "title_selector": "h2"}, "s1"
            )
        warn.assert_called_once()
        args, kwargs = warn.call_args
        assert kwargs.get("source") == "s1"
        assert kwargs.get("selector") == "[[unclosed"

    def test_non_string_and_self_skipped(self) -> None:
        with patch("voucherbot.database.bootstrap.logger.warning") as warn:
            bootstrap._warn_on_invalid_selectors(
                {"article_selector": None, "link_selector": "self"}, "s1"
            )
        warn.assert_not_called()


# ---------------------------------------------------------------------------
# _is_transient
# ---------------------------------------------------------------------------


class TestIsTransient:
    def test_integrity_error_never_transient(self) -> None:
        exc = IntegrityError("stmt", {}, Exception("dup"))
        assert bootstrap._is_transient(exc) is False

    def test_non_dbapi_error_not_transient(self) -> None:
        assert bootstrap._is_transient(RuntimeError("boom")) is False

    def test_dbapi_error_with_transient_cause(self) -> None:
        exc = DBAPIError("stmt", {}, Exception("boom"))
        exc.__cause__ = ConnectionDoesNotExistError("connection dropped")
        assert bootstrap._is_transient(exc) is True

    def test_dbapi_error_with_interface_error_cause(self) -> None:
        exc = DBAPIError("stmt", {}, Exception("boom"))
        exc.__cause__ = InterfaceError("interface error")
        assert bootstrap._is_transient(exc) is True

    def test_dbapi_error_without_cause_assumed_transient(self) -> None:
        exc = DBAPIError("stmt", {}, Exception("boom"))
        assert bootstrap._is_transient(exc) is True


# ---------------------------------------------------------------------------
# _run_with_retry
# ---------------------------------------------------------------------------


class TestRunWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self) -> None:
        calls: list[int] = []

        async def fn() -> str:
            calls.append(1)
            return "ok"

        assert await bootstrap._run_with_retry(fn) == "ok"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_recovers_from_transient_error(self) -> None:
        exc = DBAPIError("stmt", {}, Exception("boom"))
        calls: list[int] = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise exc
            return "ok"

        with patch("voucherbot.database.bootstrap.asyncio.sleep", new=AsyncMock()):
            result = await bootstrap._run_with_retry(fn)

        assert result == "ok"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_never_retries_integrity_error(self) -> None:
        async def fn() -> Any:
            raise IntegrityError("stmt", {}, Exception("dup"))

        with (
            patch(
                "voucherbot.database.bootstrap.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
            pytest.raises(IntegrityError),
        ):
            await bootstrap._run_with_retry(fn)

        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self) -> None:
        exc = DBAPIError("stmt", {}, Exception("boom"))
        calls: list[int] = []

        async def fn() -> Any:
            calls.append(1)
            raise exc

        with (
            patch("voucherbot.database.bootstrap.asyncio.sleep", new=AsyncMock()),
            pytest.raises(DBAPIError),
        ):
            await bootstrap._run_with_retry(fn)

        assert len(calls) == bootstrap._MAX_RETRIES


# ---------------------------------------------------------------------------
# Seed batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_keywords_upserts_all_and_commits() -> None:
    db = AsyncMock()
    await bootstrap._seed_keywords(db)
    assert db.execute.await_count == len(bootstrap.KEYWORDS)
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# bootstrap_data
# ---------------------------------------------------------------------------


class _FakeLockSession:
    def __init__(self, acquired: bool) -> None:
        self._acquired = acquired
        self.executed: list[str] = []

    async def __aenter__(self) -> "_FakeLockSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append(str(stmt))
        result = MagicMock()
        result.scalar_one.return_value = self._acquired
        return result


@pytest.mark.asyncio
async def test_bootstrap_data_runs_all_batches() -> None:
    lock = _FakeLockSession(acquired=True)
    with (
        patch("voucherbot.database.bootstrap.AsyncSessionLocal", return_value=lock),
        patch(
            "voucherbot.database.bootstrap._run_batch",
            new=AsyncMock(),
        ) as run_batch,
    ):
        await bootstrap.bootstrap_data()

    assert run_batch.await_count == 5
    labels = [call.args[0] for call in run_batch.await_args_list]
    assert labels == [
        "keywords",
        "reddit_sources",
        "sources",
        "vendor_mappings",
        "disable_sources",
    ]
    assert any("pg_advisory_unlock" in s for s in lock.executed)


@pytest.mark.asyncio
async def test_bootstrap_data_skips_when_lock_held() -> None:
    lock = _FakeLockSession(acquired=False)
    with (
        patch("voucherbot.database.bootstrap.AsyncSessionLocal", return_value=lock),
        patch(
            "voucherbot.database.bootstrap._run_batch",
            new=AsyncMock(),
        ) as run_batch,
    ):
        await bootstrap.bootstrap_data()

    run_batch.assert_not_awaited()
    assert not any("pg_advisory_unlock" in s for s in lock.executed)
