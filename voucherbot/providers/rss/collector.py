from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
import asyncio
import json
import re
import structlog
from lxml import etree
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.providers.http_policy import (
    default_headers,
    polite_get,
    RobotsDisallowedError,
)

logger = structlog.get_logger(__name__)

_FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml, "
    "application/json, text/xml, */*"
)

# Legacy Tech Community blog URLs redirect to login; map them to working syndication endpoints.
_TECHCOMMUNITY_FEED_REWRITES: dict[str, str] = {
    "https://techcommunity.microsoft.com/t5/microsoft-learn-blog/bg-p/MicrosoftLearnBlog/rss": (
        "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Community"
        "?interaction.style=blog&labels=Microsoft+Learn+Blog"
    ),
}

# cloud.google.com/blog/rss now serves HTML; the Atom feed moved to cloudblog.withgoogle.com.
_GOOGLE_CLOUD_BLOG_RE = re.compile(
    r"^https://cloud\.google\.com/blog/rss/?$",
    re.IGNORECASE,
)


def _normalize_feed_url(feed_url: str) -> str:
    """Rewrite known-broken feed URLs while leaving custom configs untouched."""
    if feed_url in _TECHCOMMUNITY_FEED_REWRITES:
        return _TECHCOMMUNITY_FEED_REWRITES[feed_url]

    if _GOOGLE_CLOUD_BLOG_RE.match(feed_url):
        return "https://cloudblog.withgoogle.com/rss/"

    parsed = urlparse(feed_url)
    if (
        parsed.hostname == "techcommunity.microsoft.com"
        and parsed.path.endswith("/rss")
        and not parsed.path.startswith("/t5/s/")
        and parsed.path.startswith("/t5/")
        and "gxcuf89792" in parsed.path
    ):
        rewritten_path = parsed.path.replace("/t5/", "/t5/s/", 1)
        return parsed._replace(path=rewritten_path).geturl()

    return feed_url


def _looks_like_html(content: bytes) -> bool:
    start = content.lstrip()[:256].lower()
    return start.startswith(b"<!doctype html") or start.startswith(b"<html")


# Content-Types that can never carry an RSS/Atom/JSON feed. Anything else is
# allowed through so real feeds with missing/loose headers keep working.
_NON_FEED_CONTENT_TYPE_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "multipart/",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-gzip",
    "application/x-tar",
    "application/msword",
    "application/vnd.",
)


def _feed_content_type_is_plausible(content_type: str | None) -> bool:
    """True when the response Content-Type is (or could be) a feed.

    Missing/unknown types pass through unchanged; only payloads that can
    never be a feed are rejected.
    """
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type:
        return True
    return not media_type.startswith(_NON_FEED_CONTENT_TYPE_PREFIXES)


def _clean_html(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return BeautifulSoup(value, "lxml").get_text(separator=" ", strip=True) or None


async def _clean_html_async(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return await asyncio.to_thread(_clean_html, value)


def _parse_date(entry: Any) -> datetime | None:
    """Parse a date from an RSS entry, trying multiple fields."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                dt = datetime(*val[:6])
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _parse_json_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


class RssCollector(BaseCollector):
    """Collects items from an RSS/Atom feed (preferred over HTML scraping)."""

    async def collect(
        self, source_config: dict[str, Any], limit: int = 50
    ) -> list[NormalizedPost]:
        if source_config.get("unsupported"):
            logger.info(
                "RssCollector: source marked unsupported",
                reason=source_config.get("unsupported_reason"),
            )
            return []

        feed_url = _normalize_feed_url(source_config.get("feed_url", ""))
        if not feed_url:
            logger.warning("RssCollector: no feed_url in config", config=source_config)
            return []

        timeout = float(source_config.get("timeout_seconds", 15))
        logger.info("RssCollector: fetching", feed_url=feed_url)

        try:
            response = await polite_get(feed_url, accept=_FEED_ACCEPT, timeout=timeout)
            content = response.content
            content_type = response.headers.get("content-type")
        except RobotsDisallowedError:
            logger.info("RssCollector: skipped (robots.txt)", feed_url=feed_url)
            return []
        except Exception as e:
            # Fallback for flaky hosts that block httpx but allow urllib with same UA.
            logger.warning(
                "RssCollector: httpx failed, trying urllib fallback",
                feed_url=feed_url,
                error=str(e)[:160],
            )

            def _fetch() -> tuple[bytes, str | None]:
                import urllib.request

                req = urllib.request.Request(
                    feed_url, headers=default_headers(accept=_FEED_ACCEPT)
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read(), resp.headers.get("Content-Type")

            try:
                content, content_type = await asyncio.to_thread(_fetch)
            except Exception as fb_err:
                logger.error(
                    "RssCollector: fetch failed",
                    feed_url=feed_url,
                    error=str(fb_err),
                )
                return []

        if not _feed_content_type_is_plausible(content_type):
            logger.error(
                "RssCollector: feed URL returned an unsupported Content-Type",
                feed_url=feed_url,
                content_type=content_type,
            )
            return []

        if _looks_like_html(content):
            logger.error(
                "RssCollector: feed URL returned HTML instead of XML/JSON",
                feed_url=feed_url,
            )
            return []

        json_results = self._parse_json_feed(content, source_config, limit)
        if json_results:
            logger.info(
                "RssCollector: collected JSON feed",
                feed_url=feed_url,
                count=len(json_results),
            )
            return json_results

        feed = await asyncio.to_thread(feedparser.parse, content)

        if feed.bozo and not feed.entries:
            logger.warning(
                "RssCollector: standard parse failed, attempting lxml recovery",
                feed_url=feed_url,
            )
            try:

                def _recover() -> str:
                    parser = etree.XMLParser(recover=True)
                    root = etree.fromstring(content, parser)
                    if root is None:
                        raise ValueError("XML parser returned no root element")
                    return etree.tostring(root, encoding="unicode")

                repaired_xml = await asyncio.to_thread(_recover)
                feed = await asyncio.to_thread(feedparser.parse, repaired_xml)
            except Exception as e:
                logger.error(
                    "RssCollector: recovery failed", feed_url=feed_url, error=str(e)
                )
                return []

        if feed.bozo and not feed.entries:
            logger.error(
                "RssCollector: failed to parse feed even after recovery",
                feed_url=feed_url,
                error=str(feed.bozo_exception),
            )
            return []

        results: list[NormalizedPost] = []
        for entry in feed.entries[:limit]:
            url = entry.get("link", "")
            raw_summary = entry.get("summary") or entry.get("description")
            content_text = await _clean_html_async(raw_summary)

            # If the feed provides no summary, fetch the article page and
            # extract its text so the keyword filter has something to work with.
            if not content_text and url:
                try:
                    article_resp = await polite_get(url, timeout=10.0)

                    def _extract_text() -> str:
                        return BeautifulSoup(article_resp.text, "lxml").get_text(
                            separator=" ", strip=True
                        )

                    article_text = await asyncio.to_thread(_extract_text)
                    content_text = article_text[:2000] or None
                except Exception:
                    pass

            results.append(
                NormalizedPost(
                    url=url,
                    title=entry.get("title", "(no title)"),
                    content=content_text,
                    summary=None,
                    author=entry.get("author"),
                    published_at=_parse_date(entry),
                    raw_data={
                        "feed_url": feed_url,
                        "vendor": source_config.get("vendor"),
                        "tags": [t.term for t in entry.get("tags", [])],
                    },
                )
            )

        logger.info("RssCollector: collected", feed_url=feed_url, count=len(results))
        return results

    def _parse_json_feed(
        self,
        content: bytes,
        source_config: dict[str, Any],
        limit: int,
    ) -> list[NormalizedPost]:
        try:
            payload = json.loads(content)
        except Exception:
            return []

        feed_url = source_config.get("feed_url", "")
        items = (
            payload.get("items") or payload.get("articles") or payload.get("data") or []
        )
        if not isinstance(items, list):
            return []

        results: list[NormalizedPost] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue

            url = item.get("url") or item.get("link") or item.get("canonicalUrl") or ""
            title = (
                item.get("title")
                or item.get("headline")
                or item.get("name")
                or "(no title)"
            )
            # _clean_html is synchronous but cheap for JSON feed snippets;
            # full async offload happens in the RSS path via _clean_html_async.
            summary = _clean_html(item.get("summary"))
            content_text = _clean_html(
                item.get("summary") or item.get("description") or item.get("body")
            )
            published_at = _parse_json_date(
                item.get("publishedDate") or item.get("pubDate") or item.get("date")
            )

            results.append(
                NormalizedPost(
                    url=url or feed_url,
                    title=title,
                    content=content_text,
                    summary=summary,
                    author=item.get("author"),
                    published_at=published_at,
                    raw_data={
                        "feed_url": feed_url,
                        "vendor": source_config.get("vendor"),
                        "format": "json",
                    },
                )
            )

        return results
