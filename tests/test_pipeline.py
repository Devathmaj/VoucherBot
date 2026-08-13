"""Unit tests for the shared ingestion pipeline.

Covers the pure helper functions (URL normalisation, vendor resolution,
collector resolution, fetch-limit selection, AI content trimming) and the
DB-backed orchestration paths in ``_process_one_source`` /
``run_pipeline_for_source`` with a mocked async session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voucherbot.models.event import MatchConfidence
from voucherbot.models.post import PostStatus
from voucherbot.models.source import SourceType
from voucherbot.providers.base import NormalizedPost
from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.ingestion import pipeline


def _source(**overrides: object) -> SimpleNamespace:
    base = dict(
        id=1,
        name="rss:test",
        type=SourceType.RSS,
        config={},
        base_url=None,
        last_checked_utc=None,
        error_count=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _post(url: str, title: str, content: str | None = None) -> NormalizedPost:
    return NormalizedPost(url=url, title=title, content=content)


# ---------------------------------------------------------------------------
# _normalise_url_for_match
# ---------------------------------------------------------------------------


class TestNormaliseUrlForMatch:
    def test_strips_trailing_slash(self) -> None:
        assert (
            pipeline._normalise_url_for_match("https://example.com/path/")
            == "https://example.com/path"
        )

    def test_strips_query_params(self) -> None:
        assert (
            pipeline._normalise_url_for_match("https://example.com/path?q=1&x=2")
            == "https://example.com/path"
        )

    def test_root_path_is_preserved(self) -> None:
        assert pipeline._normalise_url_for_match("https://example.com/") == (
            "https://example.com/"
        )


# ---------------------------------------------------------------------------
# _resolve_vendor
# ---------------------------------------------------------------------------


class TestResolveVendor:
    def test_url_pattern_wins_over_name_pattern(self) -> None:
        mappings = [
            {
                "url_pattern": "https://aws.amazon.com/",
                "source_name_pattern": None,
                "vendor": "aws",
            },
            {
                "url_pattern": None,
                "source_name_pattern": "amazon",
                "vendor": "amazon-name",
            },
        ]
        assert (
            pipeline._resolve_vendor(
                "AWS Blogs", "https://aws.amazon.com/blogs/", mappings
            )
            == "aws"
        )

    def test_name_pattern_is_lowercase_substring(self) -> None:
        mappings = [
            {
                "url_pattern": None,
                "source_name_pattern": "microsoft",
                "vendor": "microsoft",
            }
        ]
        assert (
            pipeline._resolve_vendor("Microsoft Learn", None, mappings) == "microsoft"
        )

    def test_returns_none_when_no_match(self) -> None:
        mappings: list[dict[str, str | None]] = []
        assert pipeline._resolve_vendor("Unknown", None, mappings) is None

    def test_base_url_matching_ignores_trailing_slash_on_pattern(self) -> None:
        mappings = [
            {
                "url_pattern": "https://www.redhat.com/",
                "source_name_pattern": None,
                "vendor": "red hat",
            }
        ]
        assert (
            pipeline._resolve_vendor(
                "Red Hat", "https://www.redhat.com/en/blog/", mappings
            )
            == "red hat"
        )


# ---------------------------------------------------------------------------
# _resolve_collector
# ---------------------------------------------------------------------------


class TestResolveCollector:
    def _collectors(self) -> dict[str, object]:
        return {
            "reddit": object(),
            "rss": object(),
            "web": object(),
            "pearsonvue": object(),
            "training_provider": object(),
        }

    def test_reddit_source(self) -> None:
        collectors = self._collectors()
        source = _source(type=SourceType.REDDIT, config={})
        assert pipeline._resolve_collector(source, collectors) is collectors["reddit"]

    def test_feed_url_uses_rss(self) -> None:
        collectors = self._collectors()
        source = _source(type=SourceType.BLOG, config={"feed_url": "https://x/feed"})
        assert pipeline._resolve_collector(source, collectors) is collectors["rss"]

    def test_article_selector_uses_web(self) -> None:
        collectors = self._collectors()
        source = _source(
            type=SourceType.WEBSITE, config={"article_selector": "article"}
        )
        assert pipeline._resolve_collector(source, collectors) is collectors["web"]

    def test_pearsonvue_source(self) -> None:
        collectors = self._collectors()
        source = _source(type=SourceType.PEARSONVUE, config={})
        assert (
            pipeline._resolve_collector(source, collectors) is collectors["pearsonvue"]
        )

    def test_training_provider_source(self) -> None:
        collectors = self._collectors()
        source = _source(type=SourceType.TRAINING_PROVIDER, config={})
        assert (
            pipeline._resolve_collector(source, collectors)
            is collectors["training_provider"]
        )

    def test_unknown_source_returns_none(self) -> None:
        source = _source(type=SourceType.EVENT, config={})
        assert pipeline._resolve_collector(source, {}) is None


# ---------------------------------------------------------------------------
# _fetch_limit_for_source
# ---------------------------------------------------------------------------


class TestFetchLimitForSource:
    def test_explicit_limit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = _source(type=SourceType.RSS, config={})
        assert pipeline._fetch_limit_for_source(source, fetch_limit=7) == 7

    def test_reddit_uses_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pipeline.settings, "reddit_fetch_limit", 42)
        source = _source(type=SourceType.REDDIT, config={})
        assert pipeline._fetch_limit_for_source(source) == 42

    def test_note_selector_uses_50(self) -> None:
        source = _source(type=SourceType.WEBSITE, config={"note_selector": ".badge"})
        assert pipeline._fetch_limit_for_source(source) == 50

    def test_defaults_to_10(self) -> None:
        source = _source(type=SourceType.RSS, config={})
        assert pipeline._fetch_limit_for_source(source) == 10


# ---------------------------------------------------------------------------
# _ai_content
# ---------------------------------------------------------------------------


class TestAiContent:
    def test_empty_content_returns_none(self) -> None:
        assert pipeline._ai_content(None) is None
        assert pipeline._ai_content("") is None

    def test_note_line_is_kept_whole(self) -> None:
        assert (
            pipeline._ai_content("Note: free exam\nmore details") == "Note: free exam"
        )

    def test_long_content_is_trimmed(self) -> None:
        long = "x" * 1000
        assert pipeline._ai_content(long) == "x" * pipeline._AI_CONTENT_LIMIT

    def test_short_content_is_preserved(self) -> None:
        assert pipeline._ai_content("short text") == "short text"


# ---------------------------------------------------------------------------
# _process_one_source
# ---------------------------------------------------------------------------


class _NestedCtx:
    async def __aenter__(self) -> "_NestedCtx":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _fake_db(
    keywords: list[object],
    db_post: object | None,
    *,
    upsert_rowcount: int = 1,
    inserted_pk: tuple[int, ...] | None = (99,),
) -> AsyncMock:
    db = AsyncMock()

    def fake_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        s = str(stmt)
        if "FROM keywords" in s:
            result = MagicMock()
            result.scalars.return_value.all.return_value = keywords
            return result
        if "FROM vendor_mappings" in s:
            result = MagicMock()
            result.all.return_value = []
            return result
        if s.startswith("INSERT INTO posts"):
            result = MagicMock()
            result.rowcount = upsert_rowcount
            result.inserted_primary_key = inserted_pk
            return result
        if "event_id IS NULL" in s:
            result = MagicMock()
            result.scalars.return_value.first.return_value = None
            return result
        if "FROM posts" in s:
            result = MagicMock()
            result.scalars.return_value.first.return_value = db_post
            return result
        raise AssertionError(f"unexpected statement: {s}")

    db.execute.side_effect = fake_execute
    db.begin_nested = MagicMock(return_value=_NestedCtx())
    return db


@pytest.mark.asyncio
async def test_process_one_source_filters_all_posts() -> None:
    source = _source()
    collector = AsyncMock()
    collector.collect.return_value = [_post("https://example.com/a", "unrelated post")]
    db = AsyncMock()

    stats = await pipeline._process_one_source(db, source, collector, [], 10)

    assert stats["fetched"] == 1
    assert stats["keyword_filtered"] == 1
    assert stats["new_posts"] == 0
    assert source.last_checked_utc is not None
    assert source.error_count == 0
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_one_source_new_voucher_full_pipeline() -> None:
    source = _source()
    post = _post(
        "https://example.com/voucher",
        "Free exam voucher 50% off",
        "Promo details",
    )
    collector = AsyncMock()
    collector.collect.return_value = [post]
    keywords = [SimpleNamespace(keyword="voucher", score=5)]
    db_post = SimpleNamespace(id=99, event_id=None, ai_result=None, status=None)
    db = _fake_db(keywords, db_post)
    extracted = ExtractedEvent(is_voucher=True, confidence=0.9, vendor="aws")

    matcher = MagicMock()
    matcher.match_or_create = AsyncMock(return_value=(None, MatchConfidence.NEW))

    with (
        patch.object(pipeline, "_event_matcher", matcher),
        patch(
            "voucherbot.services.ingestion.pipeline.analyze_post_batch",
            new=AsyncMock(return_value=[extracted]),
        ),
        patch(
            "voucherbot.services.ingestion.pipeline.stage_voucher_notification",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "voucherbot.services.ingestion.pipeline.deliver_pending_notifications",
            new=AsyncMock(return_value=1),
        ) as deliver,
    ):
        stats = await pipeline._process_one_source(db, source, collector, keywords, 10)

    assert stats["fetched"] == 1
    assert stats["keyword_filtered"] == 0
    assert stats["new_posts"] == 1
    assert stats["ai_analyzed"] == 1
    assert stats["events_created"] == 1
    assert stats["notified"] == 1
    assert db_post.ai_result == extracted.model_dump()
    assert db_post.status == PostStatus.PROCESSED
    assert source.error_count == 0
    db.commit.assert_awaited()
    deliver.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_one_source_ai_filters_non_voucher() -> None:
    source = _source()
    post = _post("https://example.com/voucher", "Free exam voucher", "promo")
    collector = AsyncMock()
    collector.collect.return_value = [post]
    keywords = [SimpleNamespace(keyword="voucher", score=5)]
    db_post = SimpleNamespace(id=99, event_id=None, ai_result=None, status=None)
    db = _fake_db(keywords, db_post)
    extracted = ExtractedEvent(is_voucher=False, confidence=0.2)

    matcher = MagicMock()
    matcher.match_or_create = AsyncMock()

    with (
        patch.object(pipeline, "_event_matcher", matcher),
        patch(
            "voucherbot.services.ingestion.pipeline.analyze_post_batch",
            new=AsyncMock(return_value=[extracted]),
        ),
        patch(
            "voucherbot.services.ingestion.pipeline.stage_voucher_notification",
            new=AsyncMock(),
        ) as stage,
    ):
        stats = await pipeline._process_one_source(db, source, collector, keywords, 10)

    assert stats["ai_analyzed"] == 1
    assert stats["events_created"] == 0
    assert stats["notified"] == 0
    assert db_post.status == PostStatus.FILTERED
    matcher.match_or_create.assert_not_awaited()
    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_source_skips_ai_result_none() -> None:
    source = _source()
    post = _post("https://example.com/voucher", "Free exam voucher", "promo")
    collector = AsyncMock()
    collector.collect.return_value = [post]
    keywords = [SimpleNamespace(keyword="voucher", score=5)]
    db_post = SimpleNamespace(id=99, event_id=None, ai_result=None, status=None)
    db = _fake_db(keywords, db_post)

    with (
        patch.object(pipeline, "_event_matcher", MagicMock()),
        patch(
            "voucherbot.services.ingestion.pipeline.analyze_post_batch",
            new=AsyncMock(return_value=[None]),
        ),
        patch(
            "voucherbot.services.ingestion.pipeline.stage_voucher_notification",
            new=AsyncMock(),
        ) as stage,
    ):
        stats = await pipeline._process_one_source(db, source, collector, keywords, 10)

    assert stats["ai_analyzed"] == 0
    assert stats["new_posts"] == 1
    stage.assert_not_awaited()
    assert db_post.status is None


@pytest.mark.asyncio
async def test_process_one_source_marks_existing_post_unchanged() -> None:
    source = _source()
    post = _post("https://example.com/voucher", "Free exam voucher", "promo")
    collector = AsyncMock()
    collector.collect.return_value = [post]
    keywords = [SimpleNamespace(keyword="voucher", score=5)]
    db = _fake_db(keywords, None, upsert_rowcount=0)

    stats = await pipeline._process_one_source(db, source, collector, keywords, 10)

    assert stats["fetched"] == 1
    assert stats["unchanged"] == 1
    assert stats["new_posts"] == 0
    assert source.error_count == 0


# ---------------------------------------------------------------------------
# run_pipeline_for_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pipeline_for_source_no_collector() -> None:
    source = _source(type=SourceType.EVENT, config={})
    result = await pipeline.run_pipeline_for_source(AsyncMock(), source, {})
    assert result == {"errors": 1}


@pytest.mark.asyncio
async def test_run_pipeline_for_source_returns_process_stats() -> None:
    source = _source()
    collector = AsyncMock()
    expected = {"fetched": 3, "new_posts": 1}

    with (
        patch(
            "voucherbot.services.ingestion.pipeline._resolve_collector",
            return_value=collector,
        ),
        patch(
            "voucherbot.services.ingestion.pipeline._process_one_source",
            new=AsyncMock(return_value=expected),
        ) as process,
    ):
        db = AsyncMock()
        kw_result = MagicMock()
        kw_result.scalars.return_value.all.return_value = []
        db.execute.return_value = kw_result
        result = await pipeline.run_pipeline_for_source(
            db, source, {"rss": collector}, fetch_limit=3
        )

    assert result == expected
    process.assert_awaited_once()
    assert process.await_args.args[4] == 3


@pytest.mark.asyncio
async def test_run_pipeline_for_source_uses_default_limit() -> None:
    source = _source()
    collector = AsyncMock()

    with (
        patch(
            "voucherbot.services.ingestion.pipeline._resolve_collector",
            return_value=collector,
        ),
        patch(
            "voucherbot.services.ingestion.pipeline._process_one_source",
            new=AsyncMock(return_value={}),
        ) as process,
    ):
        db = AsyncMock()
        kw_result = MagicMock()
        kw_result.scalars.return_value.all.return_value = []
        db.execute.return_value = kw_result
        await pipeline.run_pipeline_for_source(db, source, {"rss": collector})

    assert process.await_args.args[4] == 10
