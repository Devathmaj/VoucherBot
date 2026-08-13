"""Voucher alert emails via Resend.

Delivery uses a transactional outbox so that a voucher alert is never lost
when the ``posts.is_notified`` state commit fails, and is retried until it is
actually delivered:

1. ``stage_voucher_notification`` persists a PENDING row (rendered email +
   stable idempotency key) in the same transaction as the pipeline run.
2. ``deliver_pending_notifications`` attempts the send with the idempotency
   key; on success it marks the row SENT **and** ``posts.is_notified`` in one
   commit; on failure the row stays PENDING for the scheduler to retry.
3. ``retry_pending_notifications`` is the scheduler's background sweep. The
   idempotency key is sent to Resend as the ``Idempotency-Key`` header, so
   replaying a pending row can never deliver a duplicate email.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.config.settings import settings
from voucherbot.database.connection import session_scope
from voucherbot.models.notification import NotificationOutbox, NotificationStatus
from voucherbot.models.post import Post
from voucherbot.services.email.sender import send_email

if TYPE_CHECKING:
    from voucherbot.services.ai.schema import ExtractedEvent

logger = structlog.get_logger(__name__)

_ALLOWED_URL_SCHEMES = ("http", "https", "mailto")

# Number of delivery attempts before a PENDING outbox row is marked FAILED.
_MAX_ATTEMPTS = 5
# Max PENDING rows selected per delivery sweep.
_DELIVERY_BATCH = 100


def safe_url(url: str | None) -> str:
    """Return *url* only if it uses an allowed scheme, else a safe fallback."""
    if not url:
        return ""
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return ""
    if scheme in _ALLOWED_URL_SCHEMES:
        return url
    return ""


def _row(label: str, value: str | None) -> str:
    if not value:
        return ""
    return (
        f"<tr><td style='padding:4px 12px 4px 0;color:#666;vertical-align:top'>"
        f"{html.escape(label)}</td>"
        f"<td style='padding:4px 0'>{html.escape(value)}</td></tr>"
    )


def build_voucher_email(
    post: Post,
    extracted: ExtractedEvent,
) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for a voucher alert."""
    vendor = (extracted.vendor or "").strip()
    promo = (extracted.promotion_name or post.title or "Voucher").strip()
    subject_bits = [b for b in (vendor.title() if vendor else "", promo) if b]
    subject = "Voucher: " + " — ".join(subject_bits[:2])

    certs = ", ".join(extracted.certifications or []) or None
    description = (extracted.reason or post.summary or post.title or "").strip()

    rows = "".join(
        [
            _row("Vendor", vendor.title() if vendor else None),
            _row("Promotion", extracted.promotion_name),
            _row("Type", extracted.promotion_type),
            _row("Certifications", certs),
            _row("Code", extracted.voucher_code),
            _row("Discount", extracted.discount),
            _row("Regions", ", ".join(extracted.regions or []) or None),
            _row("Starts", extracted.start_date),
            _row("Ends", extracted.end_date),
        ]
    )

    claim_url = safe_url(extracted.registration_url or post.url)
    post_url = safe_url(post.url)

    html_body = f"""\
<div style="font-family:system-ui,sans-serif;line-height:1.45;color:#111;max-width:560px">
  <h2 style="margin:0 0 12px">New certification voucher</h2>
  <p style="margin:0 0 16px">{html.escape(description)}</p>
  <table style="border-collapse:collapse;margin:0 0 16px">{rows}</table>
  <p style="margin:0 0 8px">
    <a href="{html.escape(post_url)}">View source post</a>
  </p>
  <p style="margin:0">
    <a href="{html.escape(claim_url)}">Claim / register</a>
  </p>
</div>
"""

    text_lines = [
        "New certification voucher",
        "",
        description,
        "",
    ]
    if vendor:
        text_lines.append(f"Vendor: {vendor}")
    if extracted.promotion_name:
        text_lines.append(f"Promotion: {extracted.promotion_name}")
    if extracted.voucher_code:
        text_lines.append(f"Code: {extracted.voucher_code}")
    if extracted.discount:
        text_lines.append(f"Discount: {extracted.discount}")
    if certs:
        text_lines.append(f"Certifications: {certs}")
    text_lines.extend(["", f"Post: {post_url}", f"Claim: {claim_url}"])
    text_body = "\n".join(text_lines)

    return subject, html_body, text_body


async def notify_voucher_found(post: Post, extracted: ExtractedEvent) -> bool:
    """Email ``settings.email_id`` about a detected voucher.

    Returns True if Resend accepted the send. Skips (False) when email is
    not configured — never raises.
    """
    if not settings.email_id:
        logger.warning(
            "email.notify: EMAIL_ID not set — skipping voucher alert",
            post_id=post.id,
        )
        return False
    if not settings.resend_api_key:
        logger.warning(
            "email.notify: RESEND_API_KEY not set — skipping voucher alert",
            post_id=post.id,
        )
        return False

    subject, html_body, text_body = build_voucher_email(post, extracted)
    result = await send_email(
        to=settings.email_id,
        subject=subject,
        html=html_body,
        text=text_body,
    )
    if result is None:
        return False

    logger.info(
        "email.notify: voucher alert sent",
        post_id=post.id,
        to=settings.email_id,
        resend_id=result.get("id"),
    )
    return True


def _outbox_idempotency_key(post: Post) -> str:
    """Stable key per (post, content) so retries never duplicate a delivery."""
    return f"voucher:{post.id}:{post.content_hash or ''}"


def _email_configured() -> bool:
    if not settings.email_id:
        logger.warning(
            "email.notify: EMAIL_ID not set — voucher alert staged for retry",
        )
        return False
    if not settings.resend_api_key:
        logger.warning(
            "email.notify: RESEND_API_KEY not set — voucher alert staged for retry",
        )
        return False
    return True


async def stage_voucher_notification(
    db: AsyncSession,
    post: Post,
    extracted: ExtractedEvent,
) -> NotificationOutbox | None:
    """Persist delivery intent into the outbox (PENDING) for *post*.

    Call this **before** the pipeline's final ``db.commit()`` so the outbox row
    and the pipeline data commit atomically — delivery state then survives a
    commit failure. Returns the outbox row, or ``None`` when email is not
    configured. Idempotent per (post, content): restaging is a no-op.
    """
    if not _email_configured():
        return None
    subject, html_body, text_body = build_voucher_email(post, extracted)
    key = _outbox_idempotency_key(post)
    stmt = (
        insert(NotificationOutbox)
        .values(
            post_id=post.id,
            idempotency_key=key,
            status=NotificationStatus.PENDING,
            attempts=0,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    await db.execute(stmt)
    result = await db.execute(
        select(NotificationOutbox).where(NotificationOutbox.idempotency_key == key)
    )
    return result.scalars().first()


async def deliver_pending_notifications(session: AsyncSession) -> int:
    """Deliver PENDING outbox rows, oldest first.

    On success the row is marked SENT and ``posts.is_notified`` is set in the
    same commit. On failure the row stays PENDING (attempts incremented) for
    the next sweep, becoming FAILED after ``_MAX_ATTEMPTS``. Each send carries
    the row's idempotency key, so a crashed retry cannot duplicate the email.

    Returns the number of rows delivered in this sweep.
    """
    if not _email_configured():
        return 0
    to = settings.email_id
    if to is None:
        return 0

    now = datetime.now(timezone.utc)
    batch = await session.execute(
        select(NotificationOutbox)
        .where(NotificationOutbox.status == NotificationStatus.PENDING)
        .order_by(NotificationOutbox.created_at.asc())
        .limit(_DELIVERY_BATCH)
    )
    rows = list(batch.scalars().all())

    delivered = 0
    for row in rows:
        result = await send_email(
            to=to,
            subject=row.subject,
            html=row.html_body,
            text=row.text_body,
            idempotency_key=row.idempotency_key,
        )
        row.attempts = (row.attempts or 0) + 1
        row.last_attempt_at = now
        if result is not None:
            row.status = NotificationStatus.SENT
            row.sent_at = now
            row.last_error = None
            await session.execute(
                update(Post).where(Post.id == row.post_id).values(is_notified=True)
            )
            delivered += 1
            logger.info(
                "email.notify: outbox delivered",
                post_id=row.post_id,
                resend_id=result.get("id"),
                attempts=row.attempts,
            )
        else:
            row.last_error = "send_email returned no Resend id"
            if row.attempts >= _MAX_ATTEMPTS:
                row.status = NotificationStatus.FAILED
                logger.error(
                    "email.notify: outbox delivery failed permanently",
                    post_id=row.post_id,
                    attempts=row.attempts,
                )
            else:
                logger.warning(
                    "email.notify: outbox delivery failed, will retry",
                    post_id=row.post_id,
                    attempts=row.attempts,
                )
    await session.commit()
    return delivered


async def retry_pending_notifications() -> None:
    """Background sweep entry point: deliver PENDING outbox rows.

    Safe to call on every scheduler loop iteration; idempotency keys make
    replays harmless. Never raises — failures are logged so the scheduler
    loop stays healthy.
    """
    try:
        async with session_scope() as session:
            await deliver_pending_notifications(session)
    except Exception as exc:
        logger.warning(
            "email.notify: outbox retry sweep failed",
            error=str(exc)[:160],
        )
