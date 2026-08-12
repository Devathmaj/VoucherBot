from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class NormalizedPost:
    """Provider-agnostic representation of a single post."""

    url: str
    title: str
    content: Optional[str] = None
    summary: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    raw_data: Optional[dict[str, Any]] = field(default_factory=dict)


class BaseCollector(ABC):
    """Abstract base class all providers must implement."""

    @abstractmethod
    async def collect(
        self, source_config: dict[str, Any], limit: int
    ) -> list[NormalizedPost]:
        """Fetch and normalize posts for a given source config.

        Args:
            source_config: The JSONB config from the source row.
            limit: Maximum number of posts to collect.

        Returns:
            List of NormalizedPost objects.
        """
        ...
