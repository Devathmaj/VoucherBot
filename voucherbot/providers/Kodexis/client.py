import httpx
from typing import Any, Dict, List
import structlog

logger = structlog.get_logger(__name__)


KODEXIS_API_BASE = "https://app.kodexisapi.com"


class KodexisRedditClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.base_url = KODEXIS_API_BASE
        self.http_client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key},
            timeout=30.0,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http_client.aclose()

    async def fetch_latest_posts(
        self, subreddit: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Fetch latest posts from a subreddit using Kodexis API."""
        try:
            response = await self.http_client.get(
                f"/marketplace/reddit/api/reddit/{subreddit}",
                params={
                    "sort": "new",
                    "limit": min(limit, 10),  # API max is 10
                },
            )
            response.raise_for_status()
            data = response.json()
            # Parse Reddit's Listing format
            posts = []
            for child in data.get("data", {}).get("children", []):
                if child.get("kind") == "t3":  # t3 = link/post
                    posts.append(child.get("data", {}))
            return posts
        except httpx.HTTPStatusError as e:
            logger.error(
                "Kodexis: HTTP error fetching posts from {subreddit}",
                subreddit=subreddit,
                error=str(e),
            )
            raise
        except Exception as e:
            logger.error(
                "Kodexis: error fetching posts from {subreddit}",
                subreddit=subreddit,
                error=str(e),
            )
            raise

    async def close(self) -> None:
        await self.http_client.aclose()
