"""Minimal in-memory rate limiter for the health endpoint.

Single-process sliding-window limit keyed by client IP. Adequate for the
/health endpoint (static, cheap, and the app is deployed as one uvicorn
process). Not shared across multiple workers — document that if scaling.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from voucherbot.config.settings import settings as settings

__all__ = ["health_rate_limit", "_reset_limiter", "settings"]

_WINDOW_SECONDS = 60.0

# IP -> timestamps of recent requests (oldest first).
_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _reset_limiter() -> None:
    """Test helper — clear all recorded hits."""
    _hits.clear()


async def health_rate_limit(request: Request) -> None:
    """Reject requests over the per-IP per-minute budget with HTTP 429.

    ``health_rate_limit_per_minute <= 0`` disables the limit entirely.
    """
    limit = settings.health_rate_limit_per_minute
    if limit <= 0:
        return

    key = _client_key(request)
    now = time.monotonic()
    window = _hits[key]
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] <= cutoff:
        window.popleft()

    if len(window) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(int(_WINDOW_SECONDS))},
        )
    window.append(now)
