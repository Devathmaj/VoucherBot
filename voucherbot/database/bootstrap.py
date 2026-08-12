"""
Startup bootstrap for source and keyword seed data.

The source catalog is intentionally config-driven: collectors read the JSONB
config, so adding feeds/pages does not require a schema migration.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import structlog
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.config.settings import settings
from voucherbot.database.connection import AsyncSessionLocal, session_scope
from voucherbot.models.keyword import Keyword
from voucherbot.models.source import Source, SourceType
from voucherbot.models.vendor_mapping import VendorMapping

logger = structlog.get_logger(__name__)

# Selector keys in source config that WebsiteCollector passes to BeautifulSoup.
# "self" is a sentinel for "use the article element itself", not a CSS selector.
_SELECTOR_KEYS = (
    "article_selector",
    "title_selector",
    "link_selector",
    "note_selector",
)


def _warn_on_invalid_selectors(config: dict[str, Any], source_name: str) -> None:
    """Log a warning for any malformed CSS selector in a source config.

    Log-only by design: never rejects or mutates the config, so ingestion
    behavior is unchanged. Collectors themselves still fail gracefully with
    backoff if a bad selector slips through.
    """
    import soupsieve

    for key in _SELECTOR_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or value == "self":
            continue
        try:
            soupsieve.compile(value)
        except Exception as exc:  # SelectorSyntaxError -> malformed CSS
            logger.warning(
                "bootstrap: source config has invalid CSS selector",
                source=source_name,
                key=key,
                selector=value,
                error=str(exc),
            )


DEFAULT_QUERY_TERMS = [
    "voucher",
    "coupon",
    "promo code",
    "free exam",
    "exam voucher",
    "discount",
    "100% off",
    "50% off",
    "redeem",
]

KEYWORDS = [
    {"keyword": "voucher", "score": 5},
    {"keyword": "coupon", "score": 5},
    {"keyword": "100% off", "score": 5},
    {"keyword": "promo code", "score": 5},
    {"keyword": "free exam", "score": 5},
    {"keyword": "exam voucher", "score": 5},
    {"keyword": "certification voucher", "score": 5},
    {"keyword": "free certification", "score": 4},
    {"keyword": "free access", "score": 4},
    {"keyword": "discount", "score": 4},
    {"keyword": "redeem", "score": 4},
    {"keyword": "50% off", "score": 4},
    {"keyword": "retake", "score": 3},
    {"keyword": "safeguard", "score": 3},
    {"keyword": "limited time", "score": 3},
    {"keyword": "beta access", "score": 3},
    {"keyword": "free tier", "score": 3},
    {"keyword": "register now", "score": 1},
    {"keyword": "webinar", "score": 1},
    {"keyword": "virtual event", "score": 1},
    {"keyword": "virtual training", "score": 2},
    {"keyword": "free training", "score": 2},
    {"keyword": "live session", "score": 1},
    {"keyword": "certification", "score": 1},
    {"keyword": "exam", "score": 1},
    {"keyword": "pearsonvue", "score": 2},
    # ── New keywords from training provider scrapers ──────────────────────
    {"keyword": "beta exam", "score": 4},
    {"keyword": "exam credit", "score": 5},
    {"keyword": "free trial", "score": 3},
    {"keyword": "sponsored", "score": 2},
    {"keyword": "waived", "score": 4},
    {"keyword": "no cost", "score": 4},
    {"keyword": "complimentary", "score": 4},
    {"keyword": "free badge", "score": 3},
]

HIGH_SIGNAL_REDDIT_SUBREDDITS = [
    "AWSCertifications",
    "AzureCertification",
    "MicrosoftLearn",
    "CompTIA",
    "ccna",
    "cissp",
    "isc2",
    "redhat",
    "LinuxCertifications",
    "kubernetes",
    "googlecloud",
    "OracleCloud",
    "eFreebies",
    "FREE",
    "Udemy",
    "FreeUdemyCoupons",
]

# Removed from catalog — too noisy for cert-voucher signal.
DISABLED_REDDIT_SUBREDDITS = {"deals", "freebies"}

TIER_A_REDDIT_SUBS = {
    "AWSCertifications",
    "AzureCertification",
    "eFreebies",
    "FREE",
    "Udemy",
    "FreeUdemyCoupons",
}

_TIER_CADENCE_MINUTES = {
    "A": 15,
    "B": 60,
    "C": 240,
    "D": 720,
}


def _reddit_tier(sub: str) -> str:
    return "A" if sub in TIER_A_REDDIT_SUBS else "B"


def _source_name(source_type: SourceType, label: str) -> str:
    slug = (
        label.lower()
        .replace("&", "and")
        .replace("/", "_")
        .replace(" ", "_")
        .replace(":", "")
    )
    return f"{source_type.value.lower()}:{slug}"


def _feed(
    label: str,
    feed_url: str,
    source_type: SourceType = SourceType.RSS,
    *,
    vendor: str | None = None,
    priority_tier: str = "B",
    cadence_minutes: int | None = None,
    priority: int = 1,
    query_terms: list[str] | None = None,
    note: str | None = None,
    unsupported: bool = False,
    unsupported_reason: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    interval = (
        cadence_minutes
        if cadence_minutes is not None
        else _TIER_CADENCE_MINUTES[priority_tier]
    )
    config: dict[str, Any] = {
        "feed_url": feed_url,
        "vendor": vendor,
        "query_terms": query_terms or DEFAULT_QUERY_TERMS,
        "poll_interval_minutes": interval,
    }
    if note:
        config["note"] = note
    if unsupported:
        config["unsupported"] = True
        config["unsupported_reason"] = unsupported_reason or "Blocked by site policy"
    return {
        "name": _source_name(source_type, label),
        "type": source_type,
        "base_url": feed_url,
        "priority": priority,
        "priority_tier": priority_tier,
        "enabled": False if unsupported else (True if enabled is None else enabled),
        "config": config,
    }


def _page(
    label: str,
    url: str,
    source_type: SourceType,
    *,
    vendor: str | None = None,
    article_selector: str = "article, main li, .card, .event-card",
    title_selector: str = "h1, h2, h3, a",
    link_selector: str = "a",
    note_selector: str | None = None,
    priority_tier: str = "D",
    cadence_minutes: int | None = None,
    priority: int = 1,
    query_terms: list[str] | None = None,
    note: str | None = None,
    unsupported: bool = False,
    unsupported_reason: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    interval = (
        cadence_minutes
        if cadence_minutes is not None
        else _TIER_CADENCE_MINUTES[priority_tier]
    )
    config: dict[str, Any] = {
        "url": url,
        "vendor": vendor,
        "article_selector": article_selector,
        "title_selector": title_selector,
        "link_selector": link_selector,
        "query_terms": query_terms or DEFAULT_QUERY_TERMS,
        "poll_interval_minutes": interval,
    }
    if note_selector:
        config["note_selector"] = note_selector
    if note:
        config["note"] = note
    if unsupported:
        config["unsupported"] = True
        config["unsupported_reason"] = unsupported_reason or "Blocked by site policy"
    return {
        "name": _source_name(source_type, label),
        "type": source_type,
        "base_url": url,
        "priority": priority,
        "priority_tier": priority_tier,
        "enabled": False if unsupported else (True if enabled is None else enabled),
        "config": config,
    }


SOURCE_DEFINITIONS: list[dict[str, Any]] = [
    # Official vendor RSS/blog feeds (Tier B).
    _feed(
        "AWS Training and Certification Blog",
        "https://aws.amazon.com/blogs/training-and-certification/feed/",
        SourceType.BLOG,
        vendor="AWS",
    ),
    _feed(
        "AWS Training Announcements",
        "https://aws.amazon.com/blogs/training-and-certification/category/post-types/announcements/feed/",
        SourceType.BLOG,
        vendor="AWS",
        priority=2,
    ),
    _feed(
        "AWS Builder",
        "https://builder.aws.com/rss.xml",
        SourceType.RSS,
        vendor="AWS",
        priority=2,
    ),
    _feed(
        "Microsoft Learn Blog",
        (
            "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Community"
            "?interaction.style=blog&labels=Microsoft+Learn+Blog"
        ),
        SourceType.BLOG,
        vendor="Microsoft",
    ),
    _feed(
        "Google Cloud Blog",
        "https://cloudblog.withgoogle.com/rss/",
        SourceType.BLOG,
        vendor="Google Cloud",
    ),
    _feed(
        "Cisco Newsroom",
        "https://newsroom.cisco.com/c/services/i/servlets/newsroom/rssfeed.json",
        SourceType.BLOG,
        vendor="Cisco",
    ),
    _feed(
        "Red Hat Blog",
        "https://www.redhat.com/en/rss/blog",
        SourceType.BLOG,
        vendor="Red Hat",
    ),
    _feed(
        "Linux Foundation Blog",
        "https://www.linuxfoundation.org/blog/rss.xml",
        SourceType.BLOG,
        vendor="Linux Foundation",
    ),
    _feed(
        "Linux.com",
        "https://www.linux.com/feed/",
        SourceType.RSS,
        vendor="Linux Foundation",
    ),
    # Community/forum RSS feeds (Tier C).
    _feed(
        "Microsoft Learn Q&A Voucher Search",
        "https://learn.microsoft.com/api/search/rss?search=voucher+certification+exam&locale=en-us",
        SourceType.FORUM,
        vendor="Microsoft",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "Google Cloud Training Group",
        "https://discuss.google.dev/c/google-cloud/cloud-announcements/172.rss",
        SourceType.FORUM,
        vendor="Google Cloud",
        priority_tier="C",
        note=(
            "Migrated from Google Groups to discuss.google.dev. "
            "Category 172 = Cloud Announcements."
        ),
    ),
    _feed(
        "Microsoft Events",
        "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/board?board.id=azure-events",
        SourceType.BLOG,
        vendor="Microsoft",
        priority=2,
    ),
    _feed(
        "Microsoft Azure Blog",
        "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/board?board.id=Azure",
        SourceType.BLOG,
        vendor="Microsoft",
        priority=2,
    ),
    _feed(
        "Microsoft Security Blog",
        "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/board?board.id=microsoft-security-blog",
        SourceType.BLOG,
        vendor="Microsoft",
        priority=2,
    ),
    _feed(
        "Microsoft AI Platform Blog",
        "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Community?interaction.style=blog",
        SourceType.BLOG,
        vendor="Microsoft",
        priority=2,
        note="board.id=AIPlatformBlog is broken (returns 'Resource Not Found'). Using Community endpoint as fallback.",
    ),
    # Aggregator blogs (Tier C).
    _feed(
        "Tutorials Dojo",
        "https://tutorialsdojo.com/feed/",
        SourceType.RSS,
        vendor="Tutorials Dojo",
        priority_tier="C",
    ),
    _feed(
        "Packet Pilot",
        "https://packetpilot.com/feed/",
        SourceType.RSS,
        vendor="Packet Pilot",
        priority_tier="C",
    ),
    {
        "name": "rss:cloud_academy_blog",
        "type": SourceType.WEBSITE,
        "base_url": "https://www.pluralsight.com/resources/blog",
        "priority": 1,
        "priority_tier": "C",
        "enabled": False,
        "config": {
            "collector": "website",
            "url": "https://www.pluralsight.com/resources/blog",
            "article_selector": "a[href*='/resources/blog/']",
            "title_selector": "p",
            "link_selector": "self",
            "vendor": "Pluralsight",
            "unsupported": True,
            "unsupported_reason": (
                "Pluralsight Enterprise ToS forbid robots/crawlers/data-mining tools "
                "not provided by Pluralsight. Prefer official feeds/APIs only."
            ),
            "query_terms": DEFAULT_QUERY_TERMS,
            "poll_interval_minutes": 240,
        },
    },
    _feed(
        "Microsoft Blog",
        "https://blogs.microsoft.com/feed",
        SourceType.RSS,
        vendor="Microsoft",
    ),
    _feed(
        "Cisco Newsroom",
        "https://newsroom.cisco.com/c/services/i/servlets/newsroom/rssfeed.json",
        SourceType.RSS,
        vendor="Cisco",
    ),
    _feed(
        "Cisco Newsroom Security",
        "https://newsroom.cisco.com/c/services/i/servlets/newsroom/rssfeed.json?feed=security",
        SourceType.RSS,
        vendor="Cisco",
    ),
    _feed(
        "Cisco Newsroom Press",
        "https://newsroom.cisco.com/c/services/i/servlets/newsroom/rssfeed.json?feed=press-releases",
        SourceType.RSS,
        vendor="Cisco",
    ),
    # Official vendor/event pages (Tier D).
    _page(
        "Microsoft Cloud Skills Challenge",
        "https://learn.microsoft.com/training/challenges",
        SourceType.EVENT,
        vendor="Microsoft",
        article_selector="article, .card, li, main section",
    ),
    _page(
        "AWS Events",
        "https://aws.amazon.com/events/",
        SourceType.EVENT,
        vendor="AWS",
        article_selector=".lb-content-item, article, .card",
        title_selector="h2, h3, a",
        unsupported=True,
        unsupported_reason=(
            "AWS robots.txt / Customer Agreement discourage automated access to "
            "event pages. Use AWS Training & Certification RSS feeds instead."
        ),
    ),
    _page(
        "AWS reInvent",
        "https://aws.amazon.com/events/reinvent/",
        SourceType.EVENT,
        vendor="AWS",
        article_selector="main section, main",
        title_selector="h2, h3",
        unsupported=True,
        unsupported_reason=(
            "AWS event HTML scraping is high-risk per site policy. Prefer official "
            "AWS blog/announcement RSS."
        ),
    ),
    _page(
        "Google Cloud Events",
        "https://cloud.google.com/events",
        SourceType.EVENT,
        vendor="Google Cloud",
        article_selector="article, .event-item, .card",
        note="Conditional HTML — robots-aware, slow poll. Prefer Google Cloud blog RSS.",
    ),
    _page(
        "Google Cloud Next",
        "https://cloud.withgoogle.com/next",
        SourceType.EVENT,
        vendor="Google Cloud",
        article_selector="article, .card, main section",
        note="Conditional HTML — robots-aware, slow poll.",
    ),
    _page(
        "Cisco Live",
        "https://www.ciscolive.com/global.html",
        SourceType.EVENT,
        vendor="Cisco",
        article_selector=".cmp-teaser, article, .card",
        title_selector="h2, h3, a",
        unsupported=True,
        unsupported_reason=(
            "Cisco Terms forbid crawling/bots/scripts. Use Cisco Newsroom RSS only."
        ),
    ),
    _page(
        "CompTIA Offers",
        "https://www.comptia.org/en-us/blog/",
        SourceType.WEBSITE,
        vendor="CompTIA",
        article_selector="main li",
        title_selector="a",
        link_selector="a",
        note="Conditional HTML — robots-aware, ≤0.5 req/s via global scrape policy.",
    ),
    _page(
        "ISC2 Blog",
        "https://www.isc2.org/Insights",
        SourceType.WEBSITE,
        vendor="ISC2",
        unsupported=True,
        unsupported_reason=(
            "ISC2 Site Use Policy forbids bots/scrapers without permission. "
            "Use official APIs/feeds only."
        ),
    ),
    _feed(
        "Oracle University Blog",
        "https://feeds.libsyn.com/459162/rss",
        SourceType.BLOG,
        vendor="Oracle",
        priority_tier="D",
        note=(
            "Podcast RSS - blog /rss is 403. Podcast actively covers Race to "
            "Certification and free exam promos."
        ),
    ),
    _page(
        "Red Hat Training Specials",
        "https://www.redhat.com/en/services/training/specials",
        SourceType.WEBSITE,
        vendor="Red Hat",
        article_selector="article, .card, main section, main li",
        title_selector="h2, h3, a",
        link_selector="a",
        unsupported=True,
        unsupported_reason=(
            "Red Hat site TOS forbid robot/spider retrieval apps; robots.txt "
            "sets Crawl-delay 10. Use Red Hat Blog RSS only."
        ),
    ),
    # Aggregators without reliable known feeds (Tier C).
    _page(
        "MSFTHub Vouchers",
        "https://msfthub.com/vouchers/",
        SourceType.WEBSITE,
        vendor="MSFTHub",
        article_selector="div.sl-link-card",
        title_selector="span.title",
        link_selector="a",
        priority_tier="C",
        priority=2,
        note_selector=".sl-badge",
    ),
    _page(
        "VladTalksTech",
        "https://vladtalkstech.com/",
        SourceType.WEBSITE,
        vendor="VladTalksTech",
        article_selector=".post, article, .entry",
        title_selector=".entry-title, h2, h1",
        link_selector=".entry-title a, h2 a, h1 a",
        priority_tier="C",
        note="Requires browser-like User-Agent + Accept headers (403 without them).",
    ),
    # ── RSS/news feeds (from parser.py) ───────────────────────────────────
    _feed(
        "Petri IT",
        "https://petri.com/feed/",
        SourceType.RSS,
        vendor="Petri IT",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "InfoQ",
        "https://feed.infoq.com",
        SourceType.RSS,
        vendor="InfoQ",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "The Register",
        "https://www.theregister.com/headlines.rss",
        SourceType.RSS,
        vendor="The Register",
        priority_tier="C",
        priority=2,
        unsupported=True,
        unsupported_reason='Serves Proof-of-Work challenge ("Are we human?") instead of RSS XML; anti-bot PoW cannot be solved without JavaScript.',
    ),
    _feed(
        "TechTarget AWS",
        "https://www.techtarget.com/searchaws/rss/News-on-amazon-web-services-trends-and-technology.xml",
        SourceType.RSS,
        vendor="AWS",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "TechTarget Cloud",
        "https://www.techtarget.com/searchcloudcomputing/rss/Cloud-computing-news-and-technical-advice.xml",
        SourceType.RSS,
        vendor="TechTarget",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "Certiport Exam Updates",
        "https://certiport.pearsonvue.com/Support/Exam-content-updates.aspx?rss=exam-content-updates",
        SourceType.RSS,
        vendor="Certiport",
        priority_tier="B",
        priority=2,
    ),
    # ── New vendor feeds (Databricks, HashiCorp, LF) ─────────────────────
    _feed(
        "Databricks Learning Events",
        "https://community.databricks.com/rss/board?board.id=databricks-community-events",
        SourceType.RSS,
        vendor="Databricks",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "HashiCorp Blog",
        "https://www.hashicorp.com/blog/feed.xml",
        SourceType.BLOG,
        vendor="HashiCorp",
        priority_tier="C",
        priority=2,
    ),
    _feed(
        "Linux Foundation Training Blog",
        "https://training.linuxfoundation.org/feed/",
        SourceType.BLOG,
        vendor="Linux Foundation",
        priority_tier="B",
        priority=2,
    ),
    _page(
        "Linux Foundation Promotions",
        "https://training.linuxfoundation.org/?section=promotions",
        SourceType.WEBSITE,
        vendor="Linux Foundation",
        article_selector="main .promotion, article, .card",
        priority_tier="B",
        priority=2,
    ),
    # ── New vendor feeds ──────────────────────────────────────────────────
    _feed(
        "CNCF Blog",
        "https://www.cncf.io/blog/feed/",
        SourceType.BLOG,
        vendor="Linux Foundation",
        priority=2,
    ),
    _feed(
        "LF Events",
        "https://events.linuxfoundation.org/feed/",
        SourceType.RSS,
        vendor="Linux Foundation",
        priority=2,
    ),
    _feed(
        "VMware VMTN Blog",
        "https://blogs.vmware.com/vmtn/feed/",
        SourceType.BLOG,
        vendor="VMware",
    ),
    _feed(
        "Elastic Blog",
        "https://www.elastic.co/blog/feed",
        SourceType.BLOG,
        vendor="Elastic",
    ),
    _feed(
        "SUSE C Blog",
        "https://www.suse.com/c/feed/",
        SourceType.BLOG,
        vendor="SUSE",
    ),
    _feed(
        "Canonical Blog",
        "https://canonical.com/blog/feed",
        SourceType.BLOG,
        vendor="Canonical",
    ),
    _feed(
        "Ubuntu Blog",
        "https://ubuntu.com/blog/feed",
        SourceType.BLOG,
        vendor="Canonical",
        priority=2,
    ),
    _feed(
        "SAS Training Blog",
        "https://blogs.sas.com/content/sastraining/feed/",
        SourceType.BLOG,
        vendor="SAS",
    ),
    _feed(
        "Docker Blog",
        "https://www.docker.com/blog/feed/",
        SourceType.BLOG,
        vendor="Docker",
        priority_tier="C",
    ),
    _feed(
        "Neo4j Blog",
        "https://neo4j.com/blog/feed/",
        SourceType.BLOG,
        vendor="Neo4j",
        priority_tier="C",
    ),
    _feed(
        "Confluent Blog",
        "https://www.confluent.io/feed/",
        SourceType.RSS,
        vendor="Confluent",
        priority_tier="C",
    ),
    # ── Pearson VUE vendor pages ──────────────────────────────────────────
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE AWS"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/aws.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/aws.html",
            "vendor": "AWS",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE Microsoft"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/microsoft.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/microsoft.html",
            "vendor": "Microsoft",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE Cisco"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/cisco.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/cisco.html",
            "vendor": "Cisco",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE CompTIA"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/comptia.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/comptia.html",
            "vendor": "CompTIA",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE VMware Broadcom"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/broadcom.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/broadcom.html",
            "vendor": "VMware",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE Fortinet"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/fortinet.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/fortinet.html",
            "vendor": "Fortinet",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE Palo Alto Networks"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/paloaltonetworks.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/paloaltonetworks.html",
            "vendor": "Palo Alto Networks",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE Salesforce"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/salesforce.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/salesforce.html",
            "vendor": "Salesforce",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE ServiceNow"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/servicenow.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/servicenow.html",
            "vendor": "ServiceNow",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.PEARSONVUE, "Pearson VUE Splunk"),
        "type": SourceType.PEARSONVUE,
        "base_url": "https://www.pearsonvue.com/us/en/splunk.html",
        "priority": 1,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.pearsonvue.com/us/en/splunk.html",
            "vendor": "Splunk",
            "provider": "Pearson VUE",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    # ── Training provider promotion pages ─────────────────────────────────
    {
        "name": _source_name(
            SourceType.TRAINING_PROVIDER, "Global Knowledge Promotions"
        ),
        "type": SourceType.TRAINING_PROVIDER,
        "base_url": "https://www.globalknowledge.com/en-gb/training/special-offers/promotions",
        "priority": 2,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.globalknowledge.com/en-gb/training/special-offers/promotions",
            "provider": "Global Knowledge",
            "extractor": "gk",
            "vendor": "Global Knowledge",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(
            SourceType.TRAINING_PROVIDER, "Ascendient Learning Savings"
        ),
        "type": SourceType.TRAINING_PROVIDER,
        "base_url": "https://www.ascendientlearning.com/it-training/savings",
        "priority": 2,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.ascendientlearning.com/it-training/savings",
            "provider": "Ascendient Learning",
            "extractor": "ascendient",
            "vendor": "Ascendient Learning",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.TRAINING_PROVIDER, "Fast Lane Special Offers"),
        "type": SourceType.TRAINING_PROVIDER,
        "base_url": "https://www.fastlaneus.com/specialoffers",
        "priority": 2,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.fastlaneus.com/specialoffers",
            "provider": "Fast Lane",
            "extractor": "generic_links",
            "vendor": "Fast Lane",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.TRAINING_PROVIDER, "QA Training News"),
        "type": SourceType.TRAINING_PROVIDER,
        "base_url": "https://www.qa.com/en-us/about/news/",
        "priority": 2,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.qa.com/en-us/about/news/",
            "provider": "QA",
            "extractor": "generic_cards",
            "vendor": "QA",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(SourceType.TRAINING_PROVIDER, "Firebrand Training Blog"),
        "type": SourceType.TRAINING_PROVIDER,
        "base_url": "https://firebrand.training/en/blog",
        "priority": 2,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://firebrand.training/en/blog",
            "provider": "Firebrand Training",
            "extractor": "generic_cards",
            "vendor": "Firebrand Training",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
    {
        "name": _source_name(
            SourceType.TRAINING_PROVIDER, "Learning Tree Training Offers"
        ),
        "type": SourceType.TRAINING_PROVIDER,
        "base_url": "https://www.learningtree.com/training-offers/",
        "priority": 2,
        "priority_tier": "C",
        "enabled": True,
        "config": {
            "url": "https://www.learningtree.com/training-offers/",
            "provider": "Learning Tree",
            "extractor": "generic_cards",
            "vendor": "Learning Tree",
            "poll_interval_minutes": _TIER_CADENCE_MINUTES["C"],
        },
    },
]


# ── Local test source (IS_TEST=true only) ─────────────────────────────────────
def _test_source() -> dict[str, Any]:
    """Return the test source definition."""
    return {
        "name": "website:local_test",
        "type": SourceType.WEBSITE,
        "base_url": "http://localhost:35926/",
        "priority": 1,
        "priority_tier": "Z",
        "enabled": settings.is_test,
        "config": {
            "url": "http://localhost:35926/",
            "vendor": "local_test",
            "article_selector": ".item",
            "title_selector": "h2",
            "link_selector": "self",
            "query_terms": DEFAULT_QUERY_TERMS,
            "poll_interval_minutes": 5,
        },
    }


# Vendor mappings: URL patterns (checked first) and source name patterns
# (fallback).  Maps known official sources to their canonical vendor name.
# Sources not listed here (aggregators, unknown) rely on AI to guess.
VENDOR_MAPPINGS: list[dict[str, str | None]] = [
    # ── URL-pattern entries (checked first, matched via startswith) ──────
    {
        "url_pattern": "https://aws.amazon.com/blogs/training-and-certification/",
        "source_name_pattern": None,
        "vendor": "aws",
    },
    {
        "url_pattern": "https://builder.aws.com/",
        "source_name_pattern": None,
        "vendor": "aws",
    },
    {
        "url_pattern": "https://aws.amazon.com/events/",
        "source_name_pattern": None,
        "vendor": "aws",
    },
    {
        "url_pattern": "https://aws.amazon.com/",
        "source_name_pattern": None,
        "vendor": "aws",
    },
    {
        "url_pattern": "https://techcommunity.microsoft.com/",
        "source_name_pattern": None,
        "vendor": "microsoft",
    },
    {
        "url_pattern": "https://learn.microsoft.com/",
        "source_name_pattern": None,
        "vendor": "microsoft",
    },
    {
        "url_pattern": "https://blogs.microsoft.com/",
        "source_name_pattern": None,
        "vendor": "microsoft",
    },
    {
        "url_pattern": "https://cloudblog.withgoogle.com/",
        "source_name_pattern": None,
        "vendor": "google cloud",
    },
    {
        "url_pattern": "https://cloud.google.com/",
        "source_name_pattern": None,
        "vendor": "google cloud",
    },
    {
        "url_pattern": "https://discuss.google.dev/",
        "source_name_pattern": None,
        "vendor": "google cloud",
    },
    {
        "url_pattern": "https://newsroom.cisco.com/",
        "source_name_pattern": None,
        "vendor": "cisco",
    },
    {
        "url_pattern": "https://www.ciscolive.com/",
        "source_name_pattern": None,
        "vendor": "cisco",
    },
    {
        "url_pattern": "https://www.redhat.com/",
        "source_name_pattern": None,
        "vendor": "red hat",
    },
    {
        "url_pattern": "https://www.linuxfoundation.org/",
        "source_name_pattern": None,
        "vendor": "linux foundation",
    },
    {
        "url_pattern": "https://www.linux.com/",
        "source_name_pattern": None,
        "vendor": "linux foundation",
    },
    {
        "url_pattern": "https://www.comptia.org/",
        "source_name_pattern": None,
        "vendor": "comptia",
    },
    {
        "url_pattern": "https://www.isc2.org/",
        "source_name_pattern": None,
        "vendor": "isc2",
    },
    {
        "url_pattern": "https://feeds.libsyn.com/459162/",
        "source_name_pattern": None,
        "vendor": "oracle",
    },
    {
        "url_pattern": "https://oracleuniversitypodcast.libsyn.com/",
        "source_name_pattern": None,
        "vendor": "oracle",
    },
    {
        "url_pattern": "https://msfthub.com/",
        "source_name_pattern": None,
        "vendor": "microsoft",
    },
    {
        "url_pattern": "https://packetpilot.com/",
        "source_name_pattern": None,
        "vendor": "packet pilot",
    },
    {
        "url_pattern": "https://www.pluralsight.com/",
        "source_name_pattern": None,
        "vendor": "pluralsight",
    },
    # ── Source-name-pattern entries (fallback) ───────────────────────────
    {"url_pattern": None, "source_name_pattern": "aws training", "vendor": "aws"},
    {"url_pattern": None, "source_name_pattern": "aws builder", "vendor": "aws"},
    {"url_pattern": None, "source_name_pattern": "aws events", "vendor": "aws"},
    {"url_pattern": None, "source_name_pattern": "aws reinvent", "vendor": "aws"},
    {
        "url_pattern": None,
        "source_name_pattern": "microsoft learn",
        "vendor": "microsoft",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "microsoft blog",
        "vendor": "microsoft",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "microsoft events",
        "vendor": "microsoft",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "microsoft azure blog",
        "vendor": "microsoft",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "microsoft security blog",
        "vendor": "microsoft",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "microsoft ai platform blog",
        "vendor": "microsoft",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "google cloud",
        "vendor": "google cloud",
    },
    {"url_pattern": None, "source_name_pattern": "cisco newsroom", "vendor": "cisco"},
    {"url_pattern": None, "source_name_pattern": "cisco live", "vendor": "cisco"},
    {"url_pattern": None, "source_name_pattern": "cisco", "vendor": "cisco"},
    {"url_pattern": None, "source_name_pattern": "red hat", "vendor": "red hat"},
    {
        "url_pattern": None,
        "source_name_pattern": "linux foundation",
        "vendor": "linux foundation",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "linux.com",
        "vendor": "linux foundation",
    },
    {"url_pattern": None, "source_name_pattern": "comptia", "vendor": "comptia"},
    {"url_pattern": None, "source_name_pattern": "isc2", "vendor": "isc2"},
    {
        "url_pattern": None,
        "source_name_pattern": "oracle university",
        "vendor": "oracle",
    },
    {"url_pattern": None, "source_name_pattern": "msfthub", "vendor": "microsoft"},
    {
        "url_pattern": None,
        "source_name_pattern": "packet pilot",
        "vendor": "packet pilot",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "cloud academy",
        "vendor": "pluralsight",
    },
    {"url_pattern": None, "source_name_pattern": "pearsonvue", "vendor": "pearson vue"},
    # ── Pearson VUE vendor page mappings ──────────────────────────────────
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/aws.html",
        "source_name_pattern": None,
        "vendor": "aws",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/microsoft.html",
        "source_name_pattern": None,
        "vendor": "microsoft",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/cisco.html",
        "source_name_pattern": None,
        "vendor": "cisco",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/comptia.html",
        "source_name_pattern": None,
        "vendor": "comptia",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/broadcom.html",
        "source_name_pattern": None,
        "vendor": "vmware",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/fortinet.html",
        "source_name_pattern": None,
        "vendor": "fortinet",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/paloaltonetworks.html",
        "source_name_pattern": None,
        "vendor": "palo alto networks",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/salesforce.html",
        "source_name_pattern": None,
        "vendor": "salesforce",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/servicenow.html",
        "source_name_pattern": None,
        "vendor": "servicenow",
    },
    {
        "url_pattern": "https://www.pearsonvue.com/us/en/splunk.html",
        "source_name_pattern": None,
        "vendor": "splunk",
    },
    # ── Training provider mappings ────────────────────────────────────────
    {
        "url_pattern": "https://www.globalknowledge.com/",
        "source_name_pattern": None,
        "vendor": "global knowledge",
    },
    {
        "url_pattern": "https://www.ascendientlearning.com/",
        "source_name_pattern": None,
        "vendor": "ascendient learning",
    },
    {
        "url_pattern": "https://www.fastlaneus.com/",
        "source_name_pattern": None,
        "vendor": "fast lane",
    },
    {
        "url_pattern": "https://www.qa.com/",
        "source_name_pattern": None,
        "vendor": "qa",
    },
    {
        "url_pattern": "https://firebrand.training/",
        "source_name_pattern": None,
        "vendor": "firebrand training",
    },
    {
        "url_pattern": "https://www.learningtree.com/",
        "source_name_pattern": None,
        "vendor": "learning tree",
    },
    # ── New RSS feed mappings ─────────────────────────────────────────────
    {
        "url_pattern": "https://petri.com/",
        "source_name_pattern": None,
        "vendor": "petri it",
    },
    {
        "url_pattern": "https://feed.infoq.com",
        "source_name_pattern": None,
        "vendor": "infoq",
    },
    {
        "url_pattern": "https://www.theregister.com/",
        "source_name_pattern": None,
        "vendor": "the register",
    },
    {
        "url_pattern": "https://www.techtarget.com/searchaws/",
        "source_name_pattern": None,
        "vendor": "aws",
    },
    {
        "url_pattern": "https://www.techtarget.com/searchcloudcomputing/",
        "source_name_pattern": None,
        "vendor": "techtarget",
    },
    {
        "url_pattern": "https://certiport.pearsonvue.com/",
        "source_name_pattern": None,
        "vendor": "certiport",
    },
    # ── New vendor mappings (Databricks, HashiCorp, LF) ──────────────────
    {
        "url_pattern": "https://community.databricks.com/",
        "source_name_pattern": None,
        "vendor": "databricks",
    },
    {
        "url_pattern": "https://www.hashicorp.com/",
        "source_name_pattern": None,
        "vendor": "hashicorp",
    },
    {
        "url_pattern": "https://training.linuxfoundation.org/",
        "source_name_pattern": None,
        "vendor": "linux foundation",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "databricks",
        "vendor": "databricks",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "hashicorp",
        "vendor": "hashicorp",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "linux foundation training",
        "vendor": "linux foundation",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "linux foundation promotions",
        "vendor": "linux foundation",
    },
    # ── New vendor mappings (CNCF, VMware, Elastic, SUSE, Canonical, SAS, Docker, Neo4j, Confluent) ──
    {
        "url_pattern": "https://www.cncf.io/",
        "source_name_pattern": None,
        "vendor": "linux foundation",
    },
    {
        "url_pattern": "https://events.linuxfoundation.org/",
        "source_name_pattern": None,
        "vendor": "linux foundation",
    },
    {
        "url_pattern": "https://blogs.vmware.com/",
        "source_name_pattern": None,
        "vendor": "vmware",
    },
    {
        "url_pattern": "https://www.elastic.co/",
        "source_name_pattern": None,
        "vendor": "elastic",
    },
    {
        "url_pattern": "https://www.suse.com/",
        "source_name_pattern": None,
        "vendor": "suse",
    },
    {
        "url_pattern": "https://canonical.com/",
        "source_name_pattern": None,
        "vendor": "canonical",
    },
    {
        "url_pattern": "https://ubuntu.com/",
        "source_name_pattern": None,
        "vendor": "canonical",
    },
    {
        "url_pattern": "https://blogs.sas.com/",
        "source_name_pattern": None,
        "vendor": "sas",
    },
    {
        "url_pattern": "https://www.docker.com/",
        "source_name_pattern": None,
        "vendor": "docker",
    },
    {
        "url_pattern": "https://neo4j.com/",
        "source_name_pattern": None,
        "vendor": "neo4j",
    },
    {
        "url_pattern": "https://www.confluent.io/",
        "source_name_pattern": None,
        "vendor": "confluent",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "cncf",
        "vendor": "linux foundation",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "lf events",
        "vendor": "linux foundation",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "vmware vmtn",
        "vendor": "vmware",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "elastic",
        "vendor": "elastic",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "suse c",
        "vendor": "suse",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "canonical",
        "vendor": "canonical",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "ubuntu",
        "vendor": "canonical",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "sas training",
        "vendor": "sas",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "docker",
        "vendor": "docker",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "neo4j",
        "vendor": "neo4j",
    },
    {
        "url_pattern": None,
        "source_name_pattern": "confluent",
        "vendor": "confluent",
    },
]


# ── Retry helpers ──────────────────────────────────────────────────────────────

# Advisory lock ID (arbitrary unique integer for pg_try_advisory_lock).
_BOOTSTRAP_LOCK_ID = 0xB0075D1A
# Maximum transient-DB-error retry attempts.
_MAX_RETRIES = 5
# Base delay in seconds for exponential backoff (1, 2, 4, 8, 16).
_BASE_DELAY_S = 1.0
# Commit after this many source upserts to keep individual transactions small.
_BATCH_SIZE = 25


def _is_transient(error: Exception) -> bool:
    """Return True when *error* is a transient DB failure worth retrying.

    Never retry IntegrityError (constraint violation == idempotency bug).
    For DBAPIError, walk the full ``__cause__`` chain looking for known
    transient asyncpg exception types:
      - ConnectionDoesNotExistError  (connection dropped mid-operation)
      - QueryCanceledError           (statement timeout — retry with backoff)
      - InterfaceError               (client-side connection issue)

    The cause chain from SQLAlchemy is::

        DBAPIError
          └─ __cause__ → AsyncAdapt_asyncpg_dbapi.Error
                           └─ __cause__ → asyncpg.exceptions.*Error

    """
    if isinstance(error, IntegrityError):
        return False
    if not isinstance(error, DBAPIError):
        return False
    from asyncpg.exceptions import (  # type: ignore[import-untyped]
        ConnectionDoesNotExistError as _CDNE,
        InterfaceError as _IE,
        QueryCanceledError as _QCE,
    )

    _TRANSIENT = (_CDNE, _QCE, _IE)
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, _TRANSIENT):
            return True
        cause = cause.__cause__
    return True  # non-IntegrityError with no known cause is assumed transient


async def _run_with_retry(fn: Callable[[], Awaitable[Any]]) -> Any:
    """Execute *fn* with exponential-backoff retry for transient DB errors.

    Retries up to ``_MAX_RETRIES`` times with delays: 1, 2, 4, 8, 16 s.
    IntegrityError is never retried.  All other exceptions are raised
    immediately on the final attempt.
    """
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await fn()
        except IntegrityError:
            raise
        except DBAPIError as e:
            if not _is_transient(e) or attempt == _MAX_RETRIES:
                raise
            last_exc = e
            delay = _BASE_DELAY_S * (2 ** (attempt - 1))
            logger.warning(
                "bootstrap: transient DB error, retrying",
                attempt=attempt,
                max_retries=_MAX_RETRIES,
                delay_seconds=round(delay, 1),
                error=str(e)[:200],
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def _run_batch(
    label: str, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
) -> None:
    """Execute a bootstrap batch inside its own session, with retry.

    Each batch gets a fresh connection so no aborted transaction can leak
    between logical groups.  Transient errors are retried; all others
    propagate up and fail the bootstrap.
    """
    logger.info("bootstrap: batch starting", batch=label)

    async def _execute() -> None:
        async with session_scope() as session:
            await fn(session, *args, **kwargs)

    await _run_with_retry(_execute)
    logger.info("bootstrap: batch complete", batch=label)


# ── Batch seed functions ──────────────────────────────────────────────────────


async def _seed_keywords(db: AsyncSession) -> None:
    """Upsert keyword-scoring rows."""
    for kw in KEYWORDS:
        await db.execute(
            insert(Keyword)
            .values(
                keyword=str(kw["keyword"]).lower(),
                score=kw["score"],
                enabled=True,
            )
            .on_conflict_do_nothing(index_elements=["keyword"]),
        )
    await db.commit()


async def _seed_reddit_sources(db: AsyncSession) -> None:
    """Upsert Reddit subreddit sources."""
    for sub in HIGH_SIGNAL_REDDIT_SUBREDDITS:
        tier = _reddit_tier(sub)
        cadence = _TIER_CADENCE_MINUTES[tier]
        await db.execute(
            insert(Source)
            .values(
                name=f"reddit:{sub.lower()}",
                type=SourceType.REDDIT,
                base_url=f"https://www.reddit.com/r/{sub}",
                enabled=True,
                priority=1 if tier == "A" else 2,
                priority_tier=tier,
                config={
                    "subreddit": sub,
                    "query_terms": DEFAULT_QUERY_TERMS,
                    "poll_interval_minutes": cadence,
                    "auth_mode": "praw_or_rss",
                },
            )
            .on_conflict_do_nothing(index_elements=["name"]),
        )
    await db.commit()


async def _seed_sources(db: AsyncSession) -> None:
    """Upsert all non-Reddit sources, committing every _BATCH_SIZE.

    The sub-batch commit keeps each individual transaction small enough to
    avoid PostgreSQL ``statement_timeout`` even when the catalog grows.
    """
    test_source = _test_source()
    sources_to_seed = [*SOURCE_DEFINITIONS, test_source]

    total = len(sources_to_seed)
    for i, source in enumerate(sources_to_seed):
        enabled = source.get("enabled", True)
        _warn_on_invalid_selectors(source["config"], source["name"])
        logger.info(
            "bootstrap: upserting source",
            source=source["name"],
            index=i + 1,
            total=total,
            enabled=enabled,
        )
        await db.execute(
            insert(Source)
            .values(
                name=source["name"],
                type=source["type"],
                base_url=source["base_url"],
                enabled=enabled,
                priority=source.get("priority", 1),
                priority_tier=source.get("priority_tier", "C"),
                config=source["config"],
            )
            .on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "type": source["type"],
                    "base_url": source["base_url"],
                    "enabled": enabled,
                    "priority": source.get("priority", 1),
                    "priority_tier": source.get("priority_tier", "C"),
                    "config": source["config"],
                },
            ),
        )

        if (i + 1) % _BATCH_SIZE == 0:
            logger.info(
                "bootstrap: committing source sub-batch",
                count=i + 1,
                total=total,
            )
            await db.commit()
            await db.execute(text("SET statement_timeout = '120000'"))

    await db.commit()


async def _seed_vendor_mappings(db: AsyncSession) -> None:
    """Upsert vendor-mapping rows (URL prefix then source-name fallback)."""
    for mapping in VENDOR_MAPPINGS:
        if mapping.get("url_pattern"):
            stmt = (
                insert(VendorMapping)
                .values(
                    url_pattern=mapping["url_pattern"],
                    source_name_pattern=None,
                    vendor=mapping["vendor"],
                )
                .on_conflict_do_update(
                    index_elements=["url_pattern"],
                    set_={"vendor": mapping["vendor"]},
                )
            )
        else:
            stmt = (
                insert(VendorMapping)
                .values(
                    url_pattern=None,
                    source_name_pattern=mapping["source_name_pattern"],
                    vendor=mapping["vendor"],
                )
                .on_conflict_do_update(
                    index_elements=["source_name_pattern"],
                    set_={"vendor": mapping["vendor"]},
                )
            )
        await db.execute(stmt)
    await db.commit()


async def _disable_reddit_sources(db: AsyncSession) -> None:
    """Mark noisy subreddits as disabled."""
    for sub in DISABLED_REDDIT_SUBREDDITS:
        await db.execute(
            update(Source)
            .where(Source.name == f"reddit:{sub.lower()}")
            .values(enabled=False),
        )
    await db.commit()


# ── Main entry point ──────────────────────────────────────────────────────────


async def bootstrap_data() -> None:
    """Populate database with seed data.

    Safe to re-run (all writes use ``ON CONFLICT`` upsert semantics).
    Each logical batch is committed independently so a failure in one group
    does not roll back work already persisted.

    Resiliency properties
    ---------------------
    * Advisory lock — prevents concurrent bootstrap across app instances.
    * Per-batch sessions — no stale transaction can leak between groups.
    * Sub-batch commits — source upserts are flushed every ``_BATCH_SIZE``
      rows to avoid ``statement_timeout`` on large catalogs.
    * Exponential-backoff retry — transient errors (connection drops,
      query cancellation, interface errors) are retried up to 5 times.
      ``IntegrityError`` is **never** retried.
    * Structured logging — every batch and every source upsert is logged
      so the exact failing row is visible in production.
    """
    logger.info("bootstrap: starting data seed")

    # Acquire a session-level advisory lock so multiple app instances
    # (e.g. during a rolling deploy) do not run bootstrap simultaneously.
    async with AsyncSessionLocal() as lock_session:
        result = await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": _BOOTSTRAP_LOCK_ID},
        )
        if not result.scalar_one():
            logger.warning(
                "bootstrap: advisory lock held by another instance, skipping",
            )
            return

        try:
            await _run_batch("keywords", _seed_keywords)
            await _run_batch("reddit_sources", _seed_reddit_sources)
            await _run_batch("sources", _seed_sources)
            await _run_batch("vendor_mappings", _seed_vendor_mappings)
            await _run_batch("disable_sources", _disable_reddit_sources)
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _BOOTSTRAP_LOCK_ID},
            )

    logger.info("bootstrap: complete")
