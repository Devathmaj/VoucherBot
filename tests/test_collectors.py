"""Unit tests for collectors — patch polite_get to avoid live network / robots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from voucherbot.database.bootstrap import SOURCE_DEFINITIONS
from voucherbot.providers.base import NormalizedPost
from voucherbot.providers.http_policy import clear_policy_caches, scraper_user_agent
from voucherbot.providers.rss.collector import (
    RssCollector,
    _feed_content_type_is_plausible,
    _looks_like_html,
    _normalize_feed_url,
)
from voucherbot.providers.website.collector import WebsiteCollector

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Free exam voucher announcement</title>
      <link>https://example.com/voucher</link>
      <guid>voucher-1</guid>
      <description>100% off certification exam</description>
    </item>
  </channel>
</rss>
"""

SAMPLE_JSON = b"""{
  "items": [
    {
      "id": "news-1",
      "title": "Certification offer",
      "url": "https://example.com/offer",
      "summary": "Limited time voucher",
      "publishedDate": "Mon, 01 Jan 2024 12:00:00 GMT"
    }
  ]
}"""

SAMPLE_HTML = """<!doctype html><html><body>
<main><li><a href="/blog/post">Blog category</a></li></main>
<section><h2>Event headline</h2><a href="/details">Details</a></section>
</body></html>"""


def _mock_response(
    url: str,
    *,
    content: bytes | None = None,
    text: str | None = None,
    content_type: str | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", url)
    headers = {"content-type": content_type} if content_type else None
    return httpx.Response(
        200, request=request, content=content, text=text, headers=headers
    )


def _source_by_name(name_suffix: str) -> dict[str, Any]:
    for source in SOURCE_DEFINITIONS:
        if source["name"] == name_suffix or source["name"].endswith(name_suffix):
            return source
    raise KeyError(name_suffix)


MODIFIED_SOURCES: list[str] = [
    "blog:microsoft_learn_blog",
    "blog:microsoft_events",
    "blog:google_cloud_blog",
    "blog:oracle_university_blog",
    "forum:microsoft_learn_qanda_voucher_search",
    "forum:google_cloud_training_group",
    "rss:tutorials_dojo",
    "rss:cloud_academy_blog",
    "rss:microsoft_blog",
    "rss:cisco_newsroom",
    "rss:cisco_newsroom_security",
    "rss:cisco_newsroom_press",
    "event:aws_reinvent",
    "website:comptia_offers",
    "website:red_hat_training_specials",
]

POLICY_BLOCKED: set[str] = {
    "rss:cloud_academy_blog",
    "event:aws_events",
    "event:aws_reinvent",
    "event:cisco_live",
    "website:isc2_blog",
    "website:red_hat_training_specials",
}


@pytest.fixture(autouse=True)
def _reset_policy_state(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    clear_policy_caches()
    monkeypatch.setattr(
        "voucherbot.providers.http_policy.settings.scraper_respect_robots",
        False,
    )
    monkeypatch.setattr(
        "voucherbot.providers.http_policy.settings.scraper_min_delay_seconds",
        0.0,
    )
    yield
    clear_policy_caches()


@pytest.mark.parametrize("source_name", MODIFIED_SOURCES)
def test_modified_source_definition_exists(source_name: str) -> None:
    source: dict[str, Any] = _source_by_name(source_name)
    assert source["name"] == source_name


@pytest.mark.parametrize("source_name", sorted(POLICY_BLOCKED))
def test_policy_blocked_sources_are_unsupported(source_name: str) -> None:
    source: dict[str, Any] = _source_by_name(source_name)
    assert source["config"].get("unsupported") is True
    assert source.get("enabled") is False
    assert source["config"].get("unsupported_reason")


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        (
            "https://techcommunity.microsoft.com/t5/microsoft-learn-blog/bg-p/MicrosoftLearnBlog/rss",
            "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Community"
            "?interaction.style=blog&labels=Microsoft+Learn+Blog",
        ),
        (
            "https://cloud.google.com/blog/rss",
            "https://cloudblog.withgoogle.com/rss/",
        ),
        (
            "https://example.com/feed.xml",
            "https://example.com/feed.xml",
        ),
    ],
)
def test_normalize_feed_url(raw_url: str, expected: str) -> None:
    assert _normalize_feed_url(raw_url) == expected


def test_looks_like_html() -> None:
    assert _looks_like_html(b"<!doctype html><html></html>") is True
    assert _looks_like_html(SAMPLE_RSS) is False


def test_feed_content_type_is_plausible() -> None:
    assert _feed_content_type_is_plausible(None) is True
    assert _feed_content_type_is_plausible("") is True
    assert _feed_content_type_is_plausible("application/rss+xml") is True
    assert _feed_content_type_is_plausible("application/atom+xml") is True
    assert _feed_content_type_is_plausible("application/xml; charset=utf-8") is True
    assert _feed_content_type_is_plausible("text/xml") is True
    assert _feed_content_type_is_plausible("application/json") is True
    assert _feed_content_type_is_plausible("text/html") is True
    assert _feed_content_type_is_plausible("text/plain") is True
    assert _feed_content_type_is_plausible("application/octet-stream") is True


def test_feed_content_type_rejects_binary_payloads() -> None:
    assert _feed_content_type_is_plausible("image/png") is False
    assert _feed_content_type_is_plausible("image/jpeg") is False
    assert _feed_content_type_is_plausible("audio/mpeg") is False
    assert _feed_content_type_is_plausible("video/mp4") is False
    assert _feed_content_type_is_plausible("multipart/form-data") is False
    assert _feed_content_type_is_plausible("application/pdf") is False
    assert _feed_content_type_is_plausible("application/zip") is False
    assert _feed_content_type_is_plausible("application/x-tar") is False
    assert _feed_content_type_is_plausible("application/msword") is False
    assert _feed_content_type_is_plausible("application/vnd.ms-excel") is False


def test_scraper_user_agent_is_identifying() -> None:
    ua: str = scraper_user_agent()
    assert "VoucherBot" in ua
    assert "Mozilla/5.0" not in ua


@pytest.mark.asyncio
async def test_rss_collector_parses_xml_feed() -> None:
    collector = RssCollector()
    response: httpx.Response = _mock_response(
        "https://example.com/feed.xml", content=SAMPLE_RSS
    )

    with patch(
        "voucherbot.providers.rss.collector.polite_get",
        new=AsyncMock(return_value=response),
    ):
        posts: list[NormalizedPost] = await collector.collect(
            {"feed_url": "https://example.com/feed.xml"}, limit=5
        )

    assert len(posts) == 1
    assert posts[0].title == "Free exam voucher announcement"
    assert posts[0].url == "https://example.com/voucher"


@pytest.mark.asyncio
async def test_rss_collector_parses_json_feed() -> None:
    collector = RssCollector()
    response: httpx.Response = _mock_response(
        "https://newsroom.example.com/rssfeed.json", content=SAMPLE_JSON
    )

    with patch(
        "voucherbot.providers.rss.collector.polite_get",
        new=AsyncMock(return_value=response),
    ):
        posts: list[NormalizedPost] = await collector.collect(
            {"feed_url": "https://newsroom.example.com/rssfeed.json"},
            limit=5,
        )

    assert len(posts) == 1
    assert posts[0].title == "Certification offer"
    assert posts[0].published_at == datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_rss_collector_rejects_html_response() -> None:
    collector = RssCollector()
    response: httpx.Response = _mock_response(
        "https://cloud.google.com/blog/rss",
        content=b"<!doctype html><html></html>",
    )

    with patch(
        "voucherbot.providers.rss.collector.polite_get",
        new=AsyncMock(return_value=response),
    ):
        posts: list[NormalizedPost] = await collector.collect(
            {"feed_url": "https://cloud.google.com/blog/rss"}, limit=5
        )

    assert posts == []


@pytest.mark.asyncio
async def test_rss_collector_rejects_binary_content_type() -> None:
    collector = RssCollector()
    response: httpx.Response = _mock_response(
        "https://example.com/feed.xml",
        content=SAMPLE_RSS,
        content_type="image/png",
    )

    with patch(
        "voucherbot.providers.rss.collector.polite_get",
        new=AsyncMock(return_value=response),
    ):
        posts: list[NormalizedPost] = await collector.collect(
            {"feed_url": "https://example.com/feed.xml"}, limit=5
        )

    assert posts == []


@pytest.mark.asyncio
async def test_rss_collector_parses_feed_with_missing_content_type() -> None:
    collector = RssCollector()
    response: httpx.Response = _mock_response(
        "https://example.com/feed.xml", content=SAMPLE_RSS
    )

    with patch(
        "voucherbot.providers.rss.collector.polite_get",
        new=AsyncMock(return_value=response),
    ):
        posts: list[NormalizedPost] = await collector.collect(
            {"feed_url": "https://example.com/feed.xml"}, limit=5
        )

    assert len(posts) == 1
    assert posts[0].url == "https://example.com/voucher"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name", MODIFIED_SOURCES)
async def test_modified_rss_sources_use_headers_or_are_unsupported(
    source_name: str,
) -> None:
    source: dict[str, Any] = _source_by_name(source_name)
    config = source["config"]

    if "feed_url" not in config:
        pytest.skip("website source")

    collector = RssCollector()
    if config.get("unsupported"):
        unsupported_posts: list[NormalizedPost] = await collector.collect(
            config, limit=3
        )
        assert unsupported_posts == []
        return

    response: httpx.Response = _mock_response(config["feed_url"], content=SAMPLE_RSS)
    mock_get = AsyncMock(return_value=response)

    with patch("voucherbot.providers.rss.collector.polite_get", new=mock_get):
        posts: list[NormalizedPost] = await collector.collect(config, limit=3)

    assert len(posts) == 1
    mock_get.assert_awaited_once()
    assert "VoucherBot" in scraper_user_agent()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name", MODIFIED_SOURCES)
async def test_modified_website_sources_scrape_or_are_unsupported(
    source_name: str,
) -> None:
    source: dict[str, Any] = _source_by_name(source_name)
    config = source["config"]

    if "url" not in config:
        pytest.skip("rss source")

    collector = WebsiteCollector()
    if config.get("unsupported"):
        unsupported_posts: list[NormalizedPost] = await collector.collect(
            config, limit=3
        )
        assert unsupported_posts == []
        return

    response: httpx.Response = _mock_response(config["url"], text=SAMPLE_HTML)
    mock_get = AsyncMock(return_value=response)

    with patch("voucherbot.providers.website.collector.polite_get", new=mock_get):
        website_posts: list[NormalizedPost] = await collector.collect(config, limit=3)

    assert len(website_posts) >= 1
    assert website_posts[0].title


@pytest.mark.asyncio
async def test_website_collector_uses_article_text_when_title_selector_misses() -> None:
    collector = WebsiteCollector()
    html = "<html><body><h2>Standalone heading</h2></body></html>"
    response: httpx.Response = _mock_response("https://example.com/events/", text=html)

    with patch(
        "voucherbot.providers.website.collector.polite_get",
        new=AsyncMock(return_value=response),
    ):
        posts: list[NormalizedPost] = await collector.collect(
            {
                "url": "https://example.com/events/",
                "article_selector": "h2",
                "title_selector": "h2",
                "link_selector": "a",
            },
            limit=3,
        )

    assert len(posts) == 1
    assert posts[0].title == "Standalone heading"
