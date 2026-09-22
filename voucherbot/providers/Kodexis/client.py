import httpx
from typing import Any, Dict, List, Optional
import structlog

logger = structlog.get_logger(__name__)


KODEXIS_API_BASE = "https://api.kodexis.com/v1"
KODEXIS_REDDIT_ENDPOINT = "/reddit/posts"


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
                KODEXIS_REDDIT_ENDPOINT,
                params={
                    "subreddit": subreddit,
                    "sort": "new",
                    "limit": limit,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("posts", [])
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