from typing import Any
import asyncio
import structlog
from bs4 import BeautifulSoup

from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.providers.http_policy import polite_get, RobotsDisallowedError

logger = structlog.get_logger(__name__)


class WebsiteCollector(BaseCollector):
    """Scrapes an HTML page using configurable CSS selectors from source config.

    Expected source config shape:
    {
        "article_selector": ".news-card",   # wraps each article
        "title_selector": "h2",             # inside the article
        "link_selector": "a"                # inside the article, for href
    }

    Obeys robots.txt and per-host crawl delays via ``http_policy``. Sources
    marked ``unsupported`` (ToS / policy block) are skipped entirely.
    """

    async def collect(
        self, source_config: dict[str, Any], limit: int = 50
    ) -> list[NormalizedPost]:
        if source_config.get("unsupported"):
            logger.info(
                "WebsiteCollector: source marked unsupported",
                reason=source_config.get("unsupported_reason"),
            )
            return []

        url = source_config.get("url", "")
        article_selector = source_config.get("article_selector", "article")
        title_selector = source_config.get("title_selector", "h2")
        link_selector = source_config.get("link_selector", "a")
        note_selector = source_config.get("note_selector")

        if not url:
            logger.warning("WebsiteCollector: no url in config", config=source_config)
            return []

        timeout = float(source_config.get("timeout_seconds", 15))
        logger.info("WebsiteCollector: fetching", url=url)
        try:
            response = await polite_get(
                url,
                accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                timeout=timeout,
            )
        except RobotsDisallowedError:
            logger.info("WebsiteCollector: skipped (robots.txt)", url=url)
            return []
        except ConnectionRefusedError:
            logger.warning(
                "WebsiteCollector: local server not running",
                url=url,
            )
            return []
        except Exception as e:
            logger.error("WebsiteCollector: HTTP error", url=url, error=str(e))
            return []

        soup = await asyncio.to_thread(BeautifulSoup, response.text, "lxml")
        articles = soup.select(article_selector)[:limit]
        if not articles:
            articles = [soup]

        results: list[NormalizedPost] = []
        for article in articles:
            title_el = article.select_one(title_selector)
            link_el = (
                article
                if link_selector == "self"
                else article.select_one(link_selector)
            )

            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                title = article.get_text(separator=" ", strip=True)
            raw_href = link_el.get("href", "") if link_el else ""
            href = str(raw_href[0]) if isinstance(raw_href, list) else str(raw_href)

            if not title and not href:
                continue

            if href and not href.startswith("http"):
                from urllib.parse import urljoin

                href = urljoin(url, href)

            raw_content = article.get_text(separator=" ", strip=True) or None
            if note_selector:
                note_el = article.select_one(note_selector)
                note_text = (
                    note_el.get_text(separator=" ", strip=True) if note_el else None
                )
                content = (
                    f"Note: {note_text}\n{raw_content}" if note_text else raw_content
                )
            else:
                content = raw_content

            results.append(
                NormalizedPost(
                    url=href or url,
                    title=title or href,
                    content=content,
                    summary=None,
                    author=None,
                    published_at=None,
                    raw_data={
                        "scraped_from": url,
                        "article_selector": article_selector,
                        "vendor": source_config.get("vendor"),
                    },
                )
            )

        logger.info("WebsiteCollector: collected", url=url, count=len(results))
        return results
