"""Unit tests for the Pearson VUE vendor page collector.

Pure HTML-extraction helpers are tested directly; ``collect`` is tested
with ``polite_get`` mocked so no live network or robots behaviour is hit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from voucherbot.providers.http_policy import RobotsDisallowedError
from voucherbot.providers.pearsonvue.collector import (
    PearsonVUECollector,
    _element_has_promo_text,
    _extract_last_updated,
    _extract_promo_cards,
    _find_promo_card_parent,
)

BASE_URL = "https://www.pearsonvue.com/us/en/aws.html"
CONFIG = {"url": BASE_URL, "vendor": "AWS"}

SLIDE_HTML = """<html><body><main>
<div data-slide-url-title="AWS 50% Off Promo">text</div>
<div data-slide-url="/news">Plain link</div>
</main></body></html>"""

CARD_HTML = """<html><body><main>
<div class="promo-card">
  <h2>Special Offer: Free Exam</h2>
  <p>Get a free exam voucher</p>
  <a href="/register">Register</a>
</div>
</main></body></html>"""

OVERVIEW_HTML = """<html><body><main>
<h1>AWS Certification Programs</h1>
<p>Overview of AWS certification exams</p>
<p>Last updated: Jan 2026</p>
</main></body></html>"""


def _response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", BASE_URL), text=text)


# ---------------------------------------------------------------------------
# Pure HTML helpers
# ---------------------------------------------------------------------------


class TestElementHasPromoText:
    def test_promo_text_detected(self) -> None:
        soup = BeautifulSoup("<p>Free exam offer</p>", "html.parser")
        assert _element_has_promo_text(soup.find("p")) is True

    def test_non_promo_text_ignored(self) -> None:
        soup = BeautifulSoup(
            "<p>Comprehensive study guides for certification prep</p>", "html.parser"
        )
        assert _element_has_promo_text(soup.find("p")) is False

    def test_empty_text_returns_false(self) -> None:
        soup = BeautifulSoup("<p>   </p>", "html.parser")
        assert _element_has_promo_text(soup.find("p")) is False


class TestFindPromoCardParent:
    def test_finds_card_container(self) -> None:
        soup = BeautifulSoup(
            '<html><body><div class="card"><section><p>Free exam voucher</p></section></div></body></html>',
            "html.parser",
        )
        card = _find_promo_card_parent(soup.find("p"))
        assert card is not None
        assert "card" in card["class"]

    def test_returns_none_when_no_card(self) -> None:
        soup = BeautifulSoup(
            "<html><body><main><p>Free exam</p></main></body></html>", "html.parser"
        )
        assert _find_promo_card_parent(soup.find("p")) is None


class TestExtractPromoCards:
    def test_known_card_selector_path(self) -> None:
        soup = BeautifulSoup(CARD_HTML, "html.parser")
        items = _extract_promo_cards(soup, BASE_URL)
        assert len(items) == 1
        assert items[0]["title"] == "Special Offer: Free Exam"
        assert items[0]["url"] == "/register"

    def test_dedupes_repeated_text(self) -> None:
        html = (
            '<main><div class="card"><h2>Deal one</h2><p>Deal one details</p></div>'
            '<div class="card"><h2>Deal one</h2><p>Deal one details</p></div></main>'
        )
        soup = BeautifulSoup(html, "html.parser")
        assert len(_extract_promo_cards(soup, BASE_URL)) == 1


class TestExtractLastUpdated:
    def test_finds_last_updated_paragraph(self) -> None:
        soup = BeautifulSoup(
            "<p>Random</p><p>Last updated: Jan 5 2026</p>", "html.parser"
        )
        assert _extract_last_updated(soup) == "Last updated: Jan 5 2026"

    def test_returns_none_when_absent(self) -> None:
        soup = BeautifulSoup("<p>Only text</p>", "html.parser")
        assert _extract_last_updated(soup) is None


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_source_returns_empty() -> None:
    collector = PearsonVUECollector()
    config = {"url": BASE_URL, "unsupported": True, "unsupported_reason": "policy"}
    with patch("voucherbot.providers.pearsonvue.collector.polite_get") as get:
        posts = await collector.collect(config)
    assert posts == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_url_returns_empty() -> None:
    collector = PearsonVUECollector()
    with patch("voucherbot.providers.pearsonvue.collector.polite_get") as get:
        posts = await collector.collect({})
    assert posts == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_robots_disallowed_returns_empty() -> None:
    collector = PearsonVUECollector()
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(side_effect=RobotsDisallowedError(BASE_URL)),
    ):
        posts = await collector.collect(CONFIG)
    assert posts == []


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_auth_blocks_return_empty(status: int) -> None:
    collector = PearsonVUECollector()
    resp = httpx.Response(status, request=httpx.Request("GET", BASE_URL))
    error = httpx.HTTPStatusError("blocked", request=resp.request, response=resp)
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(side_effect=error),
    ):
        posts = await collector.collect(CONFIG)
    assert posts == []


@pytest.mark.asyncio
async def test_other_http_status_raises() -> None:
    collector = PearsonVUECollector()
    resp = httpx.Response(429, request=httpx.Request("GET", BASE_URL))
    error = httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(side_effect=error),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await collector.collect(CONFIG)


@pytest.mark.asyncio
async def test_transient_network_errors_return_empty() -> None:
    collector = PearsonVUECollector()
    for exc in (
        httpx.TimeoutException("timeout"),
        httpx.ConnectError("connect"),
    ):
        with patch(
            "voucherbot.providers.pearsonvue.collector.polite_get",
            new=AsyncMock(side_effect=exc),
        ):
            posts = await collector.collect(CONFIG)
        assert posts == []


@pytest.mark.asyncio
async def test_extracts_slide_promotions() -> None:
    collector = PearsonVUECollector()
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(return_value=_response(SLIDE_HTML)),
    ):
        posts = await collector.collect(CONFIG)

    assert len(posts) == 2
    slide = next(
        p for p in posts if p.raw_data and p.raw_data.get("type") == "slide_promo"
    )
    assert slide.title == "AWS 50% Off Promo"
    assert slide.url == BASE_URL
    link = next(
        p for p in posts if p.raw_data and p.raw_data.get("type") == "slide_link"
    )
    assert link.title == "Plain link"


@pytest.mark.asyncio
async def test_extracts_promo_cards() -> None:
    collector = PearsonVUECollector()
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(return_value=_response(CARD_HTML)),
    ):
        posts = await collector.collect(CONFIG)

    assert len(posts) == 1
    post = posts[0]
    assert post.title == "Special Offer: Free Exam"
    assert post.url == "https://www.pearsonvue.com/register"
    assert post.raw_data["type"] == "promo_card"


@pytest.mark.asyncio
async def test_falls_back_to_page_overview() -> None:
    collector = PearsonVUECollector()
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(return_value=_response(OVERVIEW_HTML)),
    ):
        posts = await collector.collect(CONFIG)

    assert len(posts) == 1
    assert posts[0].title == "Pearson VUE — AWS Certification Programs"
    assert "Last updated: Jan 2026" in posts[0].content or ""
    assert posts[0].raw_data["type"] == "page_overview"


@pytest.mark.asyncio
async def test_limit_is_respected() -> None:
    collector = PearsonVUECollector()
    with patch(
        "voucherbot.providers.pearsonvue.collector.polite_get",
        new=AsyncMock(return_value=_response(SLIDE_HTML)),
    ):
        posts = await collector.collect(CONFIG, limit=1)
    assert len(posts) == 1
