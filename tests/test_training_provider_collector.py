"""Unit tests for the training provider promotion collector.

Per-site extractors are tested directly against representative HTML;
``collect`` is tested with ``polite_get`` mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from voucherbot.providers.http_policy import RobotsDisallowedError
from voucherbot.providers.training_provider.collector import (
    EXTRACTORS,
    TrainingProviderCollector,
    _extract_ascendient,
    _extract_generic_cards,
    _extract_generic_links,
    _extract_gk,
)

BASE_URL = "https://www.globalknowledge.com/en-gb/training/special-offers/promotions"


def _response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", BASE_URL), text=text)


# ---------------------------------------------------------------------------
# Per-site extractors
# ---------------------------------------------------------------------------


class TestExtractGk:
    def test_skips_navigation_links_and_short_text(self) -> None:
        html = """
        <main>
          <a href="/training/courses/course-x">This is a long course page title</a>
          <a href="/solutions">Corporate solutions</a>
          <a href="/promotions/promo-1">Special offer: Save 50% on AWS certification training this month only</a>
          <a href="/contact">Contact us</a>
        </main>
        """
        items = _extract_gk(BeautifulSoup(html, "html.parser"))
        assert len(items) == 1
        assert items[0]["url"] == "/promotions/promo-1"

    def test_truncates_long_titles_to_120_chars(self) -> None:
        html = '<main><a href="/x">' + "word " * 50 + "</a></main>"
        items = _extract_gk(BeautifulSoup(html, "html.parser"))
        assert len(items) == 1
        assert len(items[0]["title"]) == 120


class TestExtractAscendient:
    def test_h4_blocks_with_sibling_description_and_link(self) -> None:
        html = """
        <div>
          <h4>Save on CCNA</h4>
          <p>Save 30% on CCNA bootcamps</p>
          <a href="/ccna">Details</a>
        </div>
        """
        items = _extract_ascendient(BeautifulSoup(html, "html.parser"))
        assert any(item["title"] == "Save on CCNA" for item in items)

    def test_list_links_are_included(self) -> None:
        html = """
        <ul>
          <li><a href="/promo">Discounted exam voucher bundle</a></li>
        </ul>
        """
        items = _extract_ascendient(BeautifulSoup(html, "html.parser"))
        assert any("exam voucher" in item["title"] for item in items)


class TestExtractGenericLinks:
    def test_requires_minimum_text_length(self) -> None:
        html = '<main><a href="/short">Too short</a><a href="/long">A sufficiently long offer headline</a></main>'
        items = _extract_generic_links(BeautifulSoup(html, "html.parser"))
        assert len(items) == 1
        assert items[0]["url"] == "/long"

    def test_pulls_sibling_paragraph_as_description(self) -> None:
        html = (
            '<main><div><a href="/deal">Deal of the month for Azure admins</a>'
            "<p>Limited time discount</p></div></main>"
        )
        items = _extract_generic_links(BeautifulSoup(html, "html.parser"))
        assert items[0]["description"] == "Limited time discount"


class TestExtractGenericCards:
    def test_extracts_from_card_selectors(self) -> None:
        html = """
        <main>
          <div class="card">
            <h3>Free exam retake</h3>
            <p>Get one free retake</p>
            <a href="/retake">More</a>
          </div>
        </main>
        """
        items = _extract_generic_cards(BeautifulSoup(html, "html.parser"))
        assert len(items) == 1
        assert items[0]["title"] == "Free exam retake"
        assert items[0]["url"] == "/retake"

    def test_falls_back_to_headings(self) -> None:
        html = "<main><h2>Save on Security+</h2><p>Limited time offer</p></main>"
        items = _extract_generic_cards(BeautifulSoup(html, "html.parser"))
        assert len(items) == 1
        assert items[0]["title"] == "Save on Security+"

    def test_skips_short_titles(self) -> None:
        html = '<main><div class="card"><h3>Hi</h3><p>Nothing</p></div></main>'
        items = _extract_generic_cards(BeautifulSoup(html, "html.parser"))
        assert items == []


def test_all_known_extractor_keys_registered() -> None:
    for key in ("gk", "ascendient", "generic_links", "generic_cards"):
        assert key in EXTRACTORS


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_source_returns_empty() -> None:
    collector = TrainingProviderCollector()
    config = {"url": BASE_URL, "unsupported": True, "unsupported_reason": "policy"}
    with patch("voucherbot.providers.training_provider.collector.polite_get") as get:
        posts = await collector.collect(config)
    assert posts == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_url_returns_empty() -> None:
    collector = TrainingProviderCollector()
    with patch("voucherbot.providers.training_provider.collector.polite_get") as get:
        posts = await collector.collect({"extractor": "gk"})
    assert posts == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_extractor_returns_empty() -> None:
    collector = TrainingProviderCollector()
    with patch("voucherbot.providers.training_provider.collector.polite_get") as get:
        posts = await collector.collect({"url": BASE_URL, "extractor": "nope"})
    assert posts == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_robots_disallowed_returns_empty() -> None:
    collector = TrainingProviderCollector()
    with patch(
        "voucherbot.providers.training_provider.collector.polite_get",
        new=AsyncMock(side_effect=RobotsDisallowedError(BASE_URL)),
    ):
        posts = await collector.collect({"url": BASE_URL, "extractor": "gk"})
    assert posts == []


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_auth_blocks_return_empty(status: int) -> None:
    collector = TrainingProviderCollector()
    resp = httpx.Response(status, request=httpx.Request("GET", BASE_URL))
    error = httpx.HTTPStatusError("blocked", request=resp.request, response=resp)
    with patch(
        "voucherbot.providers.training_provider.collector.polite_get",
        new=AsyncMock(side_effect=error),
    ):
        posts = await collector.collect({"url": BASE_URL, "extractor": "gk"})
    assert posts == []


@pytest.mark.asyncio
async def test_collect_resolves_relative_urls() -> None:
    html = (
        "<main>"
        '<a href="/promotions/promo-1">Special offer: Save 50% on AWS certification training</a>'
        "</main>"
    )
    collector = TrainingProviderCollector()
    with patch(
        "voucherbot.providers.training_provider.collector.polite_get",
        new=AsyncMock(return_value=_response(html)),
    ):
        posts = await collector.collect(
            {"url": BASE_URL, "provider": "Global Knowledge", "extractor": "gk"}
        )

    assert len(posts) == 1
    post = posts[0]
    assert post.url == "https://www.globalknowledge.com/promotions/promo-1"
    assert post.raw_data is not None
    assert post.raw_data["extractor"] == "gk"
    assert post.summary == post.content


@pytest.mark.asyncio
async def test_collect_honours_limit() -> None:
    html = (
        "<main>"
        + "".join(
            f'<a href="/p{i}">A sufficiently long promotion headline number {i}</a>'
            for i in range(5)
        )
        + "</main>"
    )
    collector = TrainingProviderCollector()
    with patch(
        "voucherbot.providers.training_provider.collector.polite_get",
        new=AsyncMock(return_value=_response(html)),
    ):
        posts = await collector.collect(
            {"url": BASE_URL, "provider": "GK", "extractor": "generic_links"}, limit=2
        )
    assert len(posts) == 2
