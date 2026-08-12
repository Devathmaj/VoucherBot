from typing import Any
import asyncio
import structlog
from bs4 import BeautifulSoup, Tag

from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.providers.http_policy import polite_get, RobotsDisallowedError

logger = structlog.get_logger(__name__)

# Keywords that signal promotional content in Pearson VUE pages.
# Deliberately narrow: Pearson is a voucher vendor so generic terms like
# "voucher", "save", "credit" appear on nearly every page and are not
# reliable signals of actual promotions.
_PROMO_KEYWORDS: list[str] = [
    "free exam",
    "free certification",
    "% off",
    "special offer",
    "coupon",
    "complimentary",
    "no cost",
    "promo code",
    "exam discount",
    "limited time",
    "buy one get one",
    "bogo",
    "giveaway",
    "reduced price",
    "early bird",
    "earn",
    "reward",
    "off",
    "voucher",
]

# CSS selectors for promotion card containers on Pearson VUE pages.
_CARD_SELECTORS: list[str] = [
    ".promo-section",
    ".offer-section",
    ".special-offer",
    ".promo-card",
    ".offer-card",
    ".deal-card",
    ".promotion",
    ".offer",
    ".promo",
    ".card",
    "[class*='promo']",
    "[class*='offer']",
    "[class*='voucher']",
    "[class*='deal']",
    "article",
]


def _element_has_promo_text(el: Tag) -> bool:
    """Check if an element's visible text contains promotion keywords."""
    text = el.get_text(separator=" ", strip=True).lower()
    if not text:
        return False
    for kw in _PROMO_KEYWORDS:
        if kw in text:
            return True
    return False


def _find_promo_card_parent(el: Tag, max_depth: int = 5) -> Tag | None:
    """Walk up from *el* to find a card-like parent container."""
    depth = 0
    current: Tag | None = el
    while current and depth < max_depth:
        classes = " ".join(current.get_attribute_list("class"))
        eid = str(current.get("id", "") or "")
        tag = current.name or ""
        combined = f"{tag} {classes} {eid}"
        if any(
            kw in combined.lower()
            for kw in ["card", "promo", "offer", "deal", "voucher"]
        ):
            return current
        if tag in ("section", "article", "div", "li") and any(
            kw in combined.lower()
            for kw in ["promo", "offer", "deal", "voucher", "special", "saving"]
        ):
            return current
        current = current.parent
        depth += 1
    return None


def _extract_promo_cards(soup: BeautifulSoup, url: str) -> list[dict[str, Any]]:
    """Extract promotion items from Pearson VUE pages using keyword + card detection.

    Strategy:
    1. Try known CSS card selectors.
    2. Fall back to keyword text search on all elements, then walk up to
       find a card container.
    3. Extract heading, description, and link from each card.
    """
    for tag in soup.select("nav, footer, header, script, style, .cookie-banner"):
        tag.decompose()

    main = soup.find("main") or soup.find("body")
    if main is None:
        return []

    seen_texts: set[str] = set()
    items: list[dict[str, Any]] = []

    # Strategy 1: known card CSS selectors
    cards_found: list[Tag] = []
    for sel in _CARD_SELECTORS:
        cards_found = soup.select(sel)
        if cards_found:
            break

    if cards_found:
        for card in cards_found:
            h = card.find(["h2", "h3", "h4", "h5"])
            p = card.find("p")
            a = card.find("a", href=True)
            strong = card.find("strong")

            title = (
                h.get_text(strip=True)
                if h
                else (strong.get_text(strip=True) if strong else "")
            )
            desc = p.get_text(strip=True) if p else ""
            link = a["href"] if a else ""

            text_key = (title + desc).lower().strip()
            if text_key and text_key not in seen_texts and len(title) > 4:
                seen_texts.add(text_key)
                items.append({"title": title, "description": desc, "url": link})

    # Strategy 2: keyword text search, walking up to card parent
    if not items:
        for el in main.find_all(["h2", "h3", "h4", "h5", "p", "li", "strong", "a"]):
            if not _element_has_promo_text(el):
                continue
            card_parent = _find_promo_card_parent(el)
            if card_parent is None:
                card_parent = el

            h = card_parent.find(["h2", "h3", "h4", "h5"]) or card_parent.find("strong")
            p = card_parent.find("p")
            a = card_parent.find("a", href=True)

            title = h.get_text(strip=True) if h else el.get_text(strip=True)[:120]
            desc = p.get_text(strip=True) if p else ""
            link = str(a.get("href", "")) if a and a.get("href") else ""

            text_key = (title + desc).lower().strip()
            if text_key and text_key not in seen_texts:
                seen_texts.add(text_key)
                items.append({"title": title, "description": desc, "url": link})

    return items


def _extract_last_updated(soup: BeautifulSoup) -> str | None:
    for p in soup.find_all("p"):
        t = p.get_text(strip=True)
        if t.startswith("Last updated"):
            return t
    return None


class PearsonVUECollector(BaseCollector):
    """Scrapes Pearson VUE vendor program pages.

    Extracts promotions using three strategies:
    1. Slide attributes (``data-slide-url-title`` / ``data-slide-url``) — legacy.
    2. Keyword + card detection (``_extract_promo_cards``) — catches promotions
       embedded directly in the HTML that are not behind slide attributes.
    3. Generic page overview — fallback only when nothing else is found.
    """

    async def collect(
        self, source_config: dict[str, Any], limit: int = 50
    ) -> list[NormalizedPost]:
        if source_config.get("unsupported"):
            logger.info(
                "PearsonVUECollector: source marked unsupported",
                reason=source_config.get("unsupported_reason"),
            )
            return []

        url = source_config.get("url", "")
        vendor = source_config.get("vendor", "")

        if not url:
            logger.warning(
                "PearsonVUECollector: no url in config", config=source_config
            )
            return []

        timeout = float(source_config.get("timeout_seconds", 15))
        logger.info("PearsonVUECollector: fetching", url=url, vendor=vendor)

        try:
            response = await polite_get(
                url,
                accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                timeout=timeout,
            )
        except RobotsDisallowedError:
            logger.info("PearsonVUECollector: skipped (robots.txt)", url=url)
            return []
        except Exception as e:
            logger.error(
                "PearsonVUECollector: HTTP error", url=url, vendor=vendor, error=str(e)
            )
            return []

        soup = await asyncio.to_thread(BeautifulSoup, response.text, "html.parser")

        for tag in soup.select("nav, footer, .cookie-banner, script, style"):
            tag.decompose()

        main = soup.find("main") or soup.find("body")
        if main is None:
            logger.warning("PearsonVUECollector: no main content found", url=url)
            return []

        seen_urls: set[str] = set()
        results: list[NormalizedPost] = []

        # ── Strategy 1: slide-attribute promotions ─────────────────────────
        for el in soup.find_all(lambda t: t.has_attr("data-slide-url-title")):
            slide_title_raw = el["data-slide-url-title"]
            slide_url_raw = el.get("data-slide-url", "")

            slide_title = str(slide_title_raw) if slide_title_raw else ""
            if not slide_title:
                continue

            slide_url = str(slide_url_raw) if slide_url_raw else ""

            full_url = slide_url
            if full_url and not full_url.startswith("http"):
                from urllib.parse import urljoin

                full_url = urljoin(url, full_url)

            dedup_key = full_url or slide_title
            if dedup_key in seen_urls:
                continue
            seen_urls.add(dedup_key)

            slide_text = el.get_text(separator=" ", strip=True)

            content_parts: list[str] = [slide_title]
            if slide_text and slide_text != slide_title:
                content_parts.append(slide_text)

            results.append(
                NormalizedPost(
                    url=full_url or url,
                    title=slide_title,
                    content="\n".join(content_parts),
                    raw_data={
                        "scraped_from": url,
                        "vendor": vendor,
                        "type": "slide_promo",
                    },
                )
            )

        for el in soup.find_all(lambda t: t.has_attr("data-slide-url")):
            slide_url_raw = el["data-slide-url"]
            slide_text = el.get_text(separator=" ", strip=True)

            slide_url = str(slide_url_raw) if slide_url_raw else ""
            if slide_url and slide_url not in seen_urls:
                seen_urls.add(slide_url)

                full_url = slide_url
                if not full_url.startswith("http"):
                    from urllib.parse import urljoin

                    full_url = urljoin(url, full_url)

                results.append(
                    NormalizedPost(
                        url=full_url,
                        title=slide_text or f"Promotion from {vendor}",
                        content=slide_text or "",
                        raw_data={
                            "scraped_from": url,
                            "vendor": vendor,
                            "type": "slide_link",
                        },
                    )
                )

        # ── Strategy 2: keyword + card promotion extraction ────────────────
        promo_items = await asyncio.to_thread(_extract_promo_cards, soup, url)

        for item in promo_items:
            title = item.get("title", "")
            desc = item.get("description", "")
            item_url = item.get("url", "")

            if not title and not desc:
                continue

            if item_url in seen_urls:
                continue
            if item_url:
                seen_urls.add(item_url)

            if item_url and not item_url.startswith("http"):
                from urllib.parse import urljoin

                item_url = urljoin(url, item_url)

            content = desc if desc else title

            results.append(
                NormalizedPost(
                    url=item_url or url,
                    title=title,
                    content=content,
                    summary=desc or None,
                    raw_data={
                        "scraped_from": url,
                        "vendor": vendor,
                        "type": "promo_card",
                    },
                )
            )

        # ── Strategy 3: generic page overview (fallback) ───────────────────
        if not results:
            sections = []
            current_heading = None

            for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "a"]):
                tag_name = el.name or ""
                text = el.get_text(separator=" ", strip=True)
                if not text or len(text) < 5:
                    continue
                if tag_name in ("h1", "h2", "h3", "h4"):
                    current_heading = text
                else:
                    entry: dict[str, Any] = {
                        "heading": current_heading,
                        "type": tag_name,
                        "text": text,
                    }
                    if tag_name == "a" and el.get("href"):
                        entry["href"] = str(el.get("href"))
                    sections.append(entry)

            last_updated = _extract_last_updated(soup)

            content_parts = [f"Last updated: {last_updated}"] if last_updated else []
            for sec in sections:
                line = sec["text"]
                if sec.get("heading"):
                    line = f"[{sec['heading']}] {line}"
                content_parts.append(line)

            results.append(
                NormalizedPost(
                    url=url,
                    title=f"Pearson VUE — {vendor} Certification Programs",
                    content="\n".join(content_parts),
                    summary=f"{len(sections)} sections across {vendor} certification programs on Pearson VUE",
                    raw_data={
                        "scraped_from": url,
                        "vendor": vendor,
                        "type": "page_overview",
                    },
                )
            )

        logger.info(
            "PearsonVUECollector: collected",
            url=url,
            vendor=vendor,
            slide_promos=sum(
                1
                for r in results
                if r.raw_data
                and r.raw_data.get("type") in ("slide_promo", "slide_link")
            ),
            promo_cards=sum(
                1
                for r in results
                if r.raw_data and r.raw_data.get("type") == "promo_card"
            ),
            total=len(results),
        )
        return results[:limit]
