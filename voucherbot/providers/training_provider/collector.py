from typing import Any, Callable
import asyncio
import httpx
import structlog
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.providers.http_policy import polite_get, RobotsDisallowedError

logger = structlog.get_logger(__name__)

# ── Per-site extractors ──────────────────────────────────────────────────


def _extract_gk(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    main = soup.find("main") or soup.find("body")
    for a in main.find_all("a", href=True) if main else []:
        href = a["href"]
        if any(
            skip in href
            for skip in [
                "/training/courses",
                "/certifications",
                "/contact",
                "/company",
                "/solutions",
                "/legal",
                "/account",
            ]
        ):
            continue
        text = a.get_text(separator=" ", strip=True)
        if len(text) < 20:
            continue
        items.append({"title": text[:120], "url": href, "description": text})
    return items


def _extract_ascendient(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for h4 in soup.find_all("h4"):
        title = h4.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        desc_el = h4.find_next_sibling("p")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        link_el = h4.find_parent("a") or h4.find_next("a")
        url = link_el["href"] if link_el and link_el.get("href") else ""
        items.append({"title": title, "description": desc, "url": url})
    for a in soup.select("ul a[href]"):
        text = a.get_text(strip=True)
        if text and len(text) > 8:
            items.append({"title": text, "description": "", "url": a["href"]})
    return items


def _extract_generic_links(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tag in soup.select("nav, footer, header, script, style"):
        tag.decompose()
    main = soup.find("main") or soup.find("body")
    for a in main.find_all("a", href=True) if main else []:
        text = a.get_text(separator=" ", strip=True)
        if len(text) < 15:
            continue
        parent = a.parent
        desc_el = parent.find("p") if parent else None
        desc = desc_el.get_text(strip=True) if desc_el else ""
        items.append({"title": text[:120], "description": desc, "url": a["href"]})
    return items


def _extract_generic_cards(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tag in soup.select("nav, footer, header, script, style"):
        tag.decompose()

    card_selectors = [
        "article",
        ".card",
        ".offer",
        ".promo",
        ".promotion",
        "[class*='card']",
        "[class*='offer']",
        "[class*='promo']",
    ]
    cards: list[Any] = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    if not cards:
        for h in soup.find_all(["h2", "h3", "h4"]):
            title = h.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            desc_el = h.find_next_sibling("p")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            link_el = h.find("a") or h.find_next("a")
            url = link_el["href"] if link_el and link_el.get("href") else ""
            items.append({"title": title, "description": desc, "url": url})
        return items

    for card in cards:
        h = card.find(["h2", "h3", "h4", "h5"])
        p = card.find("p")
        a = card.find("a", href=True)
        title = h.get_text(strip=True) if h else ""
        desc = p.get_text(strip=True) if p else ""
        url = a["href"] if a else ""
        if title and len(title) > 4:
            items.append({"title": title, "description": desc, "url": url})

    return items


EXTRACTORS: dict[str, Callable[[BeautifulSoup], list[dict[str, Any]]]] = {
    "gk": _extract_gk,
    "ascendient": _extract_ascendient,
    "generic_links": _extract_generic_links,
    "generic_cards": _extract_generic_cards,
}


class TrainingProviderCollector(BaseCollector):
    """Scrapes training provider promotion/voucher pages.

    Each provider site uses a configured extractor key in the source config
    that routes to the appropriate extraction function. Returns one
    NormalizedPost per promotion item found on the page.
    """

    async def collect(
        self, source_config: dict[str, Any], limit: int = 50
    ) -> list[NormalizedPost]:
        if source_config.get("unsupported"):
            logger.info(
                "TrainingProviderCollector: source marked unsupported",
                reason=source_config.get("unsupported_reason"),
            )
            return []

        url = source_config.get("url", "")
        provider = source_config.get("provider", "")
        extractor_key = source_config.get("extractor", "")

        if not url:
            logger.warning(
                "TrainingProviderCollector: no url in config", config=source_config
            )
            return []

        extractor_fn = EXTRACTORS.get(extractor_key)
        if not extractor_fn:
            logger.warning(
                "TrainingProviderCollector: unknown extractor",
                extractor=extractor_key,
                provider=provider,
            )
            return []

        timeout = float(source_config.get("timeout_seconds", 15))
        logger.info(
            "TrainingProviderCollector: fetching",
            url=url,
            provider=provider,
            extractor=extractor_key,
        )

        try:
            response = await polite_get(
                url,
                accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                timeout=timeout,
            )
        except RobotsDisallowedError:
            logger.info("TrainingProviderCollector: skipped (robots.txt)", url=url)
            return []
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning(
                    "TrainingProviderCollector: blocked by auth",
                    url=url,
                    provider=provider,
                )
                return []
            raise
        except (httpx.TimeoutException, httpx.ConnectError):
            logger.warning(
                "TrainingProviderCollector: transient error",
                url=url,
                provider=provider,
            )
            return []
        except Exception:
            logger.exception(
                "TrainingProviderCollector: unexpected error",
                url=url,
                provider=provider,
            )
            raise

        soup = await asyncio.to_thread(BeautifulSoup, response.text, "html.parser")
        items = await asyncio.to_thread(extractor_fn, soup)

        results: list[NormalizedPost] = []
        for item in items[:limit]:
            title = item.get("title", "")
            description = item.get("description", "")
            item_url = item.get("url", "")

            if not title and not description:
                continue

            if item_url and not item_url.startswith("http"):
                item_url = urljoin(url, item_url)

            content = description if description else title

            results.append(
                NormalizedPost(
                    url=item_url or url,
                    title=title,
                    content=content,
                    summary=description or None,
                    raw_data={
                        "scraped_from": url,
                        "provider": provider,
                        "extractor": extractor_key,
                    },
                )
            )

        logger.info(
            "TrainingProviderCollector: collected",
            url=url,
            provider=provider,
            count=len(results),
        )
        return results
