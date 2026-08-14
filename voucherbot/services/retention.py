"""Content retention: periodically null out the content of old posts.

Posts accumulate large scraped bodies over time. Since only the most recent
posts are ever surfaced to users, this module keeps the ``posts.content``
column bounded by nulling it for every post whose ``created_at`` is older than
``settings.content_retention_days``. Only the ``content`` column is updated —
every other column is left untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, text

from voucherbot.config.settings import settings
from voucherbot.database.connection import session_scope

logger = structlog.get_logger(__name__)


async def purge_expired_post_content() -> int:
    """Null out ``posts.content`` for every post older than the retention window.

    Returns the number of rows updated. Never raises — failures are logged so
    the scheduler loop stays healthy.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.content_retention_days
    )
    try:
        async with session_scope() as session:
            # Raw UPDATE on purpose: a Core/ORM ``update()`` would also write
            # ``updated_at = now()`` via the model's ``onupdate`` hook, but this
            # retention job must touch *only* the content column.
            result = await session.execute(
                text(
                    """
                    UPDATE posts
                    SET content = NULL
                    WHERE created_at < :cutoff
                      AND content IS NOT NULL
                    """
                ),
                {"cutoff": cutoff},
            )
            await session.commit()
            rows = cast(CursorResult[Any], result).rowcount or 0
        if rows:
            logger.info(
                "retention: purged expired post content",
                rows=rows,
                cutoff=cutoff.isoformat(),
            )
        return rows
    except Exception as exc:
        logger.warning(
            "retention: content purge sweep failed",
            error=str(exc)[:160],
        )
        return 0
