from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import asyncio

import feedparser
import structlog

from voucherbot.config.settings import settings
from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.providers.reddit.client import RedditClient
from voucherbot.providers.http_policy import (
    RobotsDisallowedError,
    polite_get,
    scraper_user_agent,
)

logger = structlog.get_logger(__name__)


class RedditCollector(BaseCollector):
    """Collects posts from a subreddit via asyncpraw."""

    def __init__(self, client: RedditClient) -> None:
        self.client = client

    async def collect(
        self, source_config: dict[str, Any], limit: int = 25
    ) -> list[NormalizedPost]:
        subreddit_name = source_config.get("subreddit", "")
        if not subreddit_name:
            logger.warning(
                "RedditCollector: no subreddit in config", config=source_config
            )
            return []

        if not settings.reddit_ingestion_enabled or not self.client.is_configured:
            return await self._collect_via_rss(source_config, limit)

        query_terms = source_config.get("query_terms") or []
        if query_terms:
            query = " OR ".join(
                f'"{term}"' if " " in term else term for term in query_terms
            )
            raw_posts = await self.client.search_posts(
                query=query,
                subreddit_name=subreddit_name,
                limit=limit,
            )
        else:
            raw_posts = await self.client.fetch_new_posts(subreddit_name, limit=limit)

        return self._normalize_praw_posts(subreddit_name, raw_posts)

    def _normalize_praw_posts(
        self, subreddit_name: str, raw_posts: Any
    ) -> list[NormalizedPost]:
        results: list[NormalizedPost] = []

        for post in raw_posts:
            results.append(
                NormalizedPost(
                    url=f"https://www.reddit.com{post.permalink}",
                    title=post.title,
                    content=post.selftext or None,
                    summary=None,
                    author=None,
                    published_at=datetime.fromtimestamp(
                        post.created_utc, tz=timezone.utc
                    ),
                    raw_data={
                        "score": post.score,
                        "num_comments": post.num_comments,
                        "url": post.url,
                        "subreddit": subreddit_name,
                        "flair": post.link_flair_text,
                    },
                )
            )

        return results

    async def _collect_via_rss(
        self,
        source_config: dict[str, Any],
        limit: int,
    ) -> list[NormalizedPost]:
        subreddit_name = source_config["subreddit"]
        query_terms = source_config.get("query_terms") or []
        if query_terms:
            query = quote_plus(" OR ".join(query_terms))
            url = f"https://www.reddit.com/r/{subreddit_name}/search.rss?q={query}&restrict_sr=on&sort=new"
        else:
            url = f"https://www.reddit.com/r/{subreddit_name}/new.rss"

        headers = {
            "User-Agent": scraper_user_agent(),
        }
        logger.info("RedditCollector: fetching RSS fallback", subreddit=subreddit_name)

        try:
            response = await polite_get(
                url,
                accept="application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                timeout=15,
                extra_headers=headers,
            )
        except RobotsDisallowedError:
            logger.info(
                "RedditCollector: RSS fallback blocked by robots.txt",
                subreddit=subreddit_name,
            )
            return []
        except Exception as exc:
            logger.error(
                "RedditCollector: RSS fallback failed",
                subreddit=subreddit_name,
                error=str(exc),
            )
            return []

        feed = await asyncio.to_thread(feedparser.parse, response.content)
        results: list[NormalizedPost] = []
        for entry in feed.entries[:limit]:
            link = entry.get("link", "")
            title = entry.get("title", "(no title)")
            results.append(
                NormalizedPost(
                    url=link,
                    title=title,
                    content=entry.get("summary") or None,
                    summary=None,
                    author=None,
                    published_at=None,
                    raw_data={
                        "subreddit": subreddit_name,
                        "feed_url": url,
                        "auth_mode": "rss",
                    },
                )
            )

        return results
