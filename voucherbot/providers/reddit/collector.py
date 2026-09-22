from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus
import re

import asyncio

import feedparser
import structlog

from voucherbot.config.settings import settings
from voucherbot.providers.base import BaseCollector, NormalizedPost
from voucherbot.providers.Kodexis.client import KodexisRedditClient
from voucherbot.providers.reddit.client import RedditClient
from voucherbot.providers.http_policy import (
    RobotsDisallowedError,
    polite_get,
    scraper_user_agent,
)

logger = structlog.get_logger(__name__)


class RedditCollector(BaseCollector):
    """Collects posts from a subreddit via asyncpraw or Kodexis fallback."""

    def __init__(self, reddit_client: RedditClient, kodexis_client: KodexisRedditClient | None = None) -> None:
        self.client = reddit_client
        self.kodexis_client = kodexis_client

    async def collect(
        self, source_config: dict[str, Any], limit: int = 25
    ) -> list[NormalizedPost]:
        subreddit_name = source_config.get("subreddit", "")
        if not subreddit_name:
            logger.warning(
                "RedditCollector: no subreddit in config", config=source_config
            )
            return []

        if settings.reddit_ingestion_enabled and self.client.is_configured:
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

        # Fallback to Kodexis API when REDDIT_INGESTION_ENABLED is false
        if self.kodexis_client:
            return await self._collect_via_kodexis(source_config, limit)

        # Fallback to RSS when Kodexis is not configured
        return await self._collect_via_rss(source_config, limit)

    async def _collect_via_kodexis(
        self, source_config: dict[str, Any], limit: int
    ) -> list[NormalizedPost]:
        subreddit_name = source_config.get("subreddit", "")
        if not subreddit_name:
            return []

        try:
            raw_posts = await self.kodexis_client.fetch_latest_posts(
                subreddit=subreddit_name, limit=limit
            )
        except Exception as e:
            logger.error(
                "Kodexis fallback failed for {subreddit}", subreddit=subreddit_name, error=str(e)
            )
            return []

        filtered_posts = self._filter_exam_pass_posts(raw_posts)
        return self._normalize_kodexis_posts(subreddit_name, filtered_posts, limit)

    def _filter_exam_pass_posts(
        self, raw_posts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter out posts mentioning 'exam' or 'certification' + 'pass'/'passed'.
        
        Only filters when 'exam' or 'certification' appear as standalone terms,
        not as part of 'free exam', 'exam voucher', 'certification voucher', etc.
        """
        filtered = []
        pass_regex = re.compile(r"\b(pass|passed|passing)\b")
        exam_regex = re.compile(r"\bexam\b")
        cert_regex = re.compile(r"\bcertification\b")
        
        exclude_exam = re.compile(r"\b(free exam|exam voucher|exam credit|beta exam|retake)\b")
        exclude_cert = re.compile(r"\b(free certification|certification voucher)\b")
        
        for post in raw_posts:
            title = (post.get("title", "") or "").lower()
            content = (post.get("selftext", "") or "").lower()
            text = f"{title} {content}"
            
            has_exam = bool(exam_regex.search(text)) and not exclude_exam.search(text)
            has_cert = bool(cert_regex.search(text)) and not exclude_cert.search(text)
            has_pass = bool(pass_regex.search(text))
            
            if (has_exam or has_cert) and has_pass:
                logger.info(
                    "Kodexis: filtered exam/cert + pass post",
                    title=post.get("title", "")[:80],
                    subreddit=post.get("subreddit", ""),
                )
                continue
            
            filtered.append(post)
        
        return filtered

    def _normalize_kodexis_posts(
        self, subreddit_name: str, raw_posts: list[dict[str, Any]], limit: int
    ) -> list[NormalizedPost]:
        results: list[NormalizedPost] = []

        for post in raw_posts[:limit]:
            results.append(
                NormalizedPost(
                    url=f"https://www.reddit.com{post.get('permalink', '')}",
                    title=post.get("title", "(no title)"),
                    content=post.get("selftext") or None,
                    summary=None,
                    author=None,
                    published_at=datetime.fromtimestamp(
                        post.get("created_utc", 0), tz=timezone.utc
                    ),
                    raw_data={
                        "score": post.get("score", 0),
                        "num_comments": post.get("num_comments", 0),
                        "url": post.get("url", ""),
                        "subreddit": subreddit_name,
                        "auth_mode": "kodexis",
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
