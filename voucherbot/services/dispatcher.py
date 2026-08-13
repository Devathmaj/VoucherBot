"""
DB-driven pipeline dispatcher.

Each tick acquires a Postgres lease, picks exactly one due source, runs the
pipeline for that source, updates scheduling fields, and releases the lease.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.exc import DBAPIError

from voucherbot.config.settings import settings
from voucherbot.database.connection import session_scope
from voucherbot.models.source import Source, SourceType
from voucherbot.providers.base import BaseCollector
from voucherbot.services.ingestion.pipeline import run_pipeline_for_source

logger = structlog.get_logger(__name__)

LOCK_NAME = "pipeline"
PROCESS_BOOT_AT = datetime.now(timezone.utc)


def set_process_boot_at(value: datetime | None = None) -> None:
    """Record the real process start time.

    Called during app startup (lifespan) so lease-staleness checks reflect the
    actual boot, not module import time (uvicorn ``--preload`` imports modules
    in the master before workers fork).
    """
    global PROCESS_BOOT_AT
    PROCESS_BOOT_AT = value if value is not None else datetime.now(timezone.utc)

# Default poll intervals (minutes) when config lacks poll_interval_minutes.
_TIER_DEFAULT_MINUTES = {
    "A": 15,
    "B": 60,
    "C": 240,
    "D": 720,
}


# HTTP status codes / error patterns that indicate a permanently broken source.
# These skip backoff and disable the source immediately.
_UNRECOVERABLE_HTTP = {400, 401, 403, 404, 410, 451}
_UNRECOVERABLE_PATTERNS = (
    "404",
    "403",
    "401",
    "410",
    "not found",
    "forbidden",
    "unauthorized",
    "gone",
    "no longer available",
    "does not exist",
)


def _is_unrecoverable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _UNRECOVERABLE_PATTERNS)


def compute_backoff_minutes(consecutive_failures: int) -> int:
    """Exponential backoff capped at source_backoff_max_minutes."""
    if consecutive_failures <= 0:
        return 0
    delay = settings.source_backoff_base_minutes * (2 ** (consecutive_failures - 1))
    return int(min(delay, settings.source_backoff_max_minutes))


def poll_interval_minutes(source: Source) -> int:
    config = source.config or {}
    interval = config.get("poll_interval_minutes")
    if interval is not None:
        try:
            val = int(interval)
            return max(1, min(val, 43200))
        except (TypeError, ValueError):
            pass
    tier = source.priority_tier or "C"
    return _TIER_DEFAULT_MINUTES.get(tier, 240)


def rolling_avg_runtime_ms(previous: int | None, elapsed_ms: int) -> int:
    if previous is None:
        return elapsed_ms
    return (previous + elapsed_ms) // 2


async def _acquire_lease(session: AsyncSession, holder_id: str) -> bool:
    ttl = timedelta(seconds=settings.tick_lease_ttl_seconds)
    now = datetime.now(timezone.utc)
    expires_at = now + ttl

    await session.execute(
        text(
            """
            INSERT INTO pipeline_lock (name, holder, acquired_at, expires_at)
            VALUES (:lock_name, NULL, NULL, NULL)
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {"lock_name": LOCK_NAME},
    )

    result = await session.execute(
        text(
            """
            UPDATE pipeline_lock
            SET holder = :holder_id,
                acquired_at = :now,
                expires_at = :expires_at
            WHERE name = :lock_name
                            AND (
                                        holder IS NULL
                                 OR expires_at < :now
                                 OR acquired_at < :boot_at
                            )
            RETURNING name
            """
        ),
        {
            "holder_id": holder_id,
            "now": now,
            "expires_at": expires_at,
            "boot_at": PROCESS_BOOT_AT,
            "lock_name": LOCK_NAME,
        },
    )
    acquired = result.scalar_one_or_none() is not None
    if acquired:
        await session.commit()
    return acquired


async def _release_lease(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            UPDATE pipeline_lock
            SET holder = NULL,
                acquired_at = NULL,
                expires_at = NULL
            WHERE name = :lock_name
            """
        ),
        {"lock_name": LOCK_NAME},
    )
    await session.commit()


async def reset_lease(session: AsyncSession) -> None:
    """Clear any leftover lease state so a fresh app start can run immediately."""
    await session.execute(
        text(
            """
            UPDATE pipeline_lock
            SET holder = NULL,
                acquired_at = NULL,
                expires_at = NULL
            WHERE name = :lock_name
            """
        ),
        {"lock_name": LOCK_NAME},
    )
    await session.commit()


async def _pick_due_source(session: AsyncSession) -> Source | None:
    now = datetime.now(timezone.utc)
    filters = [
        Source.enabled.is_(True),
        or_(
            Source.config["unsupported"].is_(None),
            Source.config["unsupported"].as_string() != "true",
        ),
        or_(Source.next_due_at.is_(None), Source.next_due_at <= now),
        or_(Source.backoff_until.is_(None), Source.backoff_until <= now),
    ]
    if not settings.reddit_ingestion_enabled:
        filters.append(Source.type != SourceType.REDDIT)

    result = await session.execute(
        select(Source)
        .where(*filters)
        .order_by(Source.next_due_at.asc().nulls_first(), Source.priority_tier.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


async def _mark_success(
    session: AsyncSession,
    source: Source,
    elapsed_ms: int,
) -> None:
    now = datetime.now(timezone.utc)
    interval = poll_interval_minutes(source)
    avg_runtime_ms = rolling_avg_runtime_ms(source.avg_runtime_ms, elapsed_ms)
    next_due_at = now + timedelta(minutes=interval)
    source.last_checked_utc = now
    source.next_due_at = next_due_at
    source.consecutive_failures = 0
    source.backoff_until = None
    source.avg_runtime_ms = avg_runtime_ms
    source.error_count = 0
    await session.execute(
        text(
            """
            UPDATE sources
            SET last_checked_utc = :now,
                next_due_at = :next_due_at,
                consecutive_failures = 0,
                backoff_until = NULL,
                avg_runtime_ms = :avg_runtime_ms,
                error_count = 0
            WHERE id = :source_id
            """
        ),
        {
            "now": now,
            "next_due_at": next_due_at,
            "avg_runtime_ms": avg_runtime_ms,
            "source_id": source.id,
        },
    )
    await session.commit()


async def _mark_failure(
    session: AsyncSession,
    source: Source,
    elapsed_ms: int,
) -> None:
    now = datetime.now(timezone.utc)
    failures = (source.consecutive_failures or 0) + 1
    backoff_minutes = compute_backoff_minutes(failures)
    backoff_until = now + timedelta(minutes=backoff_minutes)
    avg_runtime_ms = rolling_avg_runtime_ms(source.avg_runtime_ms, elapsed_ms)
    error_count = (source.error_count or 0) + 1
    source.consecutive_failures = failures
    source.backoff_until = backoff_until
    source.next_due_at = backoff_until
    source.avg_runtime_ms = avg_runtime_ms
    source.error_count = error_count
    await session.execute(
        text(
            """
            UPDATE sources
            SET consecutive_failures = :failures,
                backoff_until = :backoff_until,
                next_due_at = :backoff_until,
                avg_runtime_ms = :avg_runtime_ms,
                error_count = :error_count
            WHERE id = :source_id
            """
        ),
        {
            "failures": failures,
            "backoff_until": backoff_until,
            "avg_runtime_ms": avg_runtime_ms,
            "error_count": error_count,
            "source_id": source.id,
        },
    )
    await session.commit()


async def _mark_unrecoverable(
    session: AsyncSession,
    source: Source,
    error: str,
) -> None:
    """Disable a source permanently after an unrecoverable error."""
    logger.warning(
        "dispatcher: disabling source — unrecoverable error",
        source=source.name,
        error=error[:120],
    )
    await session.execute(
        text(
            """
            UPDATE sources
            SET enabled = false,
                error_count = error_count + 1
            WHERE id = :source_id
            """
        ),
        {"source_id": source.id},
    )
    await session.commit()


async def _with_fresh_session(
    action: str,
    callback: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Run *callback(*args, **kwargs) inside a fresh session, swallowing DB errors."""
    try:
        async with session_scope() as fresh:
            await callback(fresh, *args, **kwargs)
    except Exception as e:
        logger.warning(
            "dispatcher: fresh session fallback failed",
            action=action,
            error=str(e)[:160],
        )


async def _safe_release_lease(session: AsyncSession | None) -> bool:
    """Release the lease on *session*, or with a fresh session if broken.
    Returns True if the lease was released successfully.
    """
    if session is not None:
        try:
            await session.rollback()
            await _release_lease(session)
            return True
        except DBAPIError:
            logger.warning(
                "dispatcher: session broken, releasing lease with fresh session"
            )

    await _with_fresh_session("release_lease", _release_lease)
    return False


async def dispatch_tick(
    session: AsyncSession,
    collectors: dict[str, BaseCollector],
    holder_id: str,
) -> dict[str, Any]:
    """
    Run one scheduler tick: acquire lease, process one source, release lease.

    Returns ``{"status": "ran"|"busy"|"idle", ...}``.
    """
    if not await _acquire_lease(session, holder_id):
        logger.info("dispatcher: lease busy", holder_id=holder_id)
        return {"status": "busy"}

    try:
        source = await _pick_due_source(session)
        if source is None:
            logger.info("dispatcher: no sources due")
            return {"status": "idle"}

        start = datetime.now(timezone.utc)
        source_name = source.name
        failure_source = Source()
        failure_source.id = source.id
        failure_source.consecutive_failures = source.consecutive_failures
        failure_source.error_count = source.error_count
        failure_source.avg_runtime_ms = source.avg_runtime_ms

        try:
            if settings.tick_job_timeout_seconds:
                import asyncio

                async with asyncio.timeout(settings.tick_job_timeout_seconds):
                    stats = await run_pipeline_for_source(session, source, collectors)
            else:
                stats = await run_pipeline_for_source(session, source, collectors)
            # Pipeline's db.commit() expires the source ORM instance.
            # Refresh before _mark_success reads source.config / source.avg_runtime_ms
            # to avoid the "greenlet_spawn has not been called" error.
            await session.refresh(source)
            elapsed_ms = int(
                (datetime.now(timezone.utc) - start).total_seconds() * 1000
            )
            await _mark_success(session, source, elapsed_ms)
            logger.info(
                "dispatcher: tick ran",
                source=source_name,
                elapsed_ms=elapsed_ms,
                **stats,
            )
            return {
                "status": "ran",
                "source": source_name,
                "elapsed_ms": elapsed_ms,
                **stats,
            }
        except Exception as exc:
            elapsed_ms = int(
                (datetime.now(timezone.utc) - start).total_seconds() * 1000
            )
            try:
                await session.rollback()
            except DBAPIError:
                pass
            try:
                if _is_unrecoverable(exc):
                    await _mark_unrecoverable(session, source, str(exc))
                else:
                    await _mark_failure(session, failure_source, elapsed_ms)
            except DBAPIError:
                logger.warning(
                    "dispatcher: session broken after pipeline error, "
                    "retrying with fresh session",
                    source=source_name,
                )
                async with session_scope() as fresh:
                    try:
                        if _is_unrecoverable(exc):
                            await _mark_unrecoverable(fresh, source, str(exc))
                        else:
                            await _mark_failure(fresh, failure_source, elapsed_ms)
                    except Exception as inner:
                        logger.error(
                            "dispatcher: fresh session fallback also failed",
                            source=source_name,
                            error=str(inner)[:160],
                        )
            logger.error(
                "dispatcher: tick failed",
                source=source_name,
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )
            return {
                "status": "failed",
                "source": source_name,
                "elapsed_ms": elapsed_ms,
                "error": str(exc),
            }
    finally:
        await _safe_release_lease(session)
