"""Background scheduler: sweep all due sources, then sleep until the next one is due.

Each sweep runs every source that is currently due, one at a time (sequential,
not concurrent — keeps CPU flat under 0.1 vCPU). After the sweep the loop
sleeps until the earliest next_due_at across all enabled sources, capped at
MAX_SLEEP_SECONDS so the process stays responsive to newly-added sources.
"""

from __future__ import annotations

import asyncio
from typing import Any
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, or_, select

from voucherbot.database.connection import session_scope
from voucherbot.models.notification import NotificationOutbox, NotificationStatus
from voucherbot.models.source import Source
from voucherbot.providers.reddit.client import RedditClient
from voucherbot.providers.reddit.collector import RedditCollector
from voucherbot.providers.rss.collector import RssCollector
from voucherbot.providers.website.collector import WebsiteCollector
from voucherbot.providers.pearsonvue.collector import PearsonVUECollector
from voucherbot.providers.training_provider.collector import TrainingProviderCollector
from voucherbot.services.dispatcher import dispatch_tick
from voucherbot.services.email.notifications import retry_pending_notifications
from voucherbot.services.event_consolidation import consolidate_events
from voucherbot.services.retention import purge_expired_post_content

logger = structlog.get_logger(__name__)

_reddit_client = RedditClient()
_collectors = {
    "reddit": RedditCollector(_reddit_client),
    "rss": RssCollector(),
    "web": WebsiteCollector(),
    "pearsonvue": PearsonVUECollector(),
    "training_provider": TrainingProviderCollector(),
}

_loop_task: asyncio.Task[Any] | None = None

# Hard ceiling on how long we sleep between sweeps (6 hours).
# Prevents the loop from sleeping forever if all sources have distant due times.
MAX_SLEEP_SECONDS = 6 * 3600
# Short cap applied while notification outbox rows are PENDING so undelivered
# voucher alerts are retried promptly even when no source is due.
_EMAIL_RETRY_INTERVAL_SECONDS = 60


async def _seconds_until_next_due() -> float:
    """Return seconds until the earliest next_due_at among sources that are
    actually eligible to run — same filters as _pick_due_source.

    Returns MAX_SLEEP_SECONDS when no eligible source has a future due time,
    so the loop never busy-spins on an empty or all-disabled source list.
    Capped at _EMAIL_RETRY_INTERVAL_SECONDS while notification outbox rows are
    pending, so undelivered emails are retried promptly.
    """
    now = datetime.now(timezone.utc)
    pending = 0
    async with session_scope() as session:
        stmt = (
            select(Source.next_due_at)
            .where(
                Source.enabled.is_(True),
                or_(Source.backoff_until.is_(None), Source.backoff_until <= now),
            )
            .order_by(Source.next_due_at.asc().nulls_last())
            .limit(1)
        )
        result = await session.execute(stmt)
        next_due = result.scalar_one_or_none()
        pending = (
            await session.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(NotificationOutbox.status == NotificationStatus.PENDING)
            )
            or 0
        )

    if next_due is None:
        return _EMAIL_RETRY_INTERVAL_SECONDS if pending else MAX_SLEEP_SECONDS

    if next_due.tzinfo is None:
        next_due = next_due.replace(tzinfo=timezone.utc)

    remaining = (next_due - now).total_seconds()
    # If remaining <= 0 a source is already overdue; start the next sweep
    # immediately but yield the event loop first to avoid a tight spin.
    if remaining <= 0:
        return 1.0
    if pending:
        return min(remaining, _EMAIL_RETRY_INTERVAL_SECONDS)
    return min(remaining, MAX_SLEEP_SECONDS)


async def _run_sweep() -> int:
    """Run one full sweep: process every due source sequentially.

    Returns the number of sources that were actually run.
    """
    ran = 0
    busy_waited = 0
    # Cap how long we'll wait for another instance to release the lease.
    # tick_lease_ttl_seconds is 21600 (6 h) by default; 120 retries × 5 s = 10 min
    # is enough to cover any normal source run without spinning forever on a
    # crashed/stuck holder.
    _MAX_BUSY_RETRIES = 120
    while True:
        holder_id = str(uuid.uuid4())[:8]
        async with session_scope() as session:
            result = await dispatch_tick(session, _collectors, holder_id)
        status = result.get("status")
        if status == "ran":
            ran += 1
            busy_waited = 0
            logger.info("scheduler: source processed", **result)
        elif status in ("failed", "skipped"):
            ran += 1
            busy_waited = 0
            logger.warning("scheduler: source error", **result)
        elif status == "busy":
            busy_waited += 1
            if busy_waited >= _MAX_BUSY_RETRIES:
                logger.error(
                    "scheduler: lease held too long, abandoning sweep",
                    retries=busy_waited,
                )
                break
            await asyncio.sleep(5)
        else:
            # "idle" — no more due sources; sweep is complete.
            break
    return ran


async def _run_loop() -> None:
    logger.info("scheduler: loop started")
    while True:
        try:
            ran = await _run_sweep()
            logger.info("scheduler: sweep complete", sources_ran=ran)
            # Retry undelivered voucher alerts (idempotency keyed) even when
            # no source ran; failed rows are re-attempted on later sweeps.
            await retry_pending_notifications()
            # Null out content of posts older than the retention window.
            await purge_expired_post_content()
            # Merge duplicate canonical Events that never saw each other at
            # ingestion time (throttled internally).
            await consolidate_events()
            sleep_seconds = await _seconds_until_next_due()
            logger.info(
                "scheduler: sleeping until next sweep",
                sleep_seconds=round(sleep_seconds),
            )
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            logger.info("scheduler: loop cancelled")
            return
        except Exception as exc:
            logger.error("scheduler: unexpected loop error", error=str(exc))
            await asyncio.sleep(60)


def start_scheduler() -> None:
    global _loop_task
    logger.info("scheduler: starting loop")
    _loop_task = asyncio.ensure_future(_run_loop())


async def stop_scheduler() -> None:
    global _loop_task
    logger.info("scheduler: shutting down")
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
        await asyncio.gather(_loop_task, return_exceptions=True)
    await _reddit_client.close()
