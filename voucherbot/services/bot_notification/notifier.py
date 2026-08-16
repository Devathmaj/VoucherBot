"""
Bot webhook notification service.

POSTs the same voucher data that the email notification module sends to a
remote bot server (e.g. a Discord bot) so the recipient gets an alert there
too.  The request is authenticated with ``WEBHOOK_SECRET`` in the
``Authorization`` header.

Payload shape mirrors ``build_voucher_email`` so both channels carry the same
information: vendor, promotion, type, certifications, voucher code, discount,
regions, dates, URLs, and a confidence signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from voucherbot.config.settings import settings

if TYPE_CHECKING:
    from voucherbot.models.post import Post
    from voucherbot.services.ai.schema import ExtractedEvent

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 10.0


def build_voucher_payload(post: Post, extracted: ExtractedEvent) -> dict[str, Any]:
    """Build the JSON payload POSTed to the bot server.

    Mirrors the email notification content:
    - ``title`` — human-readable alert heading like the email subject
    - ``post`` — source post URL (like the email's "View source post")
    - ``claim_url`` — registration URL when present, else the post URL
    - one key per voucher field the AI extracted (null fields are omitted)
    - ``confidence`` — AI confidence in ``is_voucher``
    - ``sent_at`` — ISO timestamp of this notification
    """
    vendor = (extracted.vendor or "").strip()
    promo = (extracted.promotion_name or post.title or "Voucher").strip()
    subject_bits = [b for b in (vendor.title() if vendor else "", promo) if b]
    title = "Voucher: " + " — ".join(subject_bits[:2])

    claim_url = extracted.registration_url or post.url

    payload: dict[str, Any] = {
        "event": "voucher_alert",
        "title": title,
        "post": post.url,
        "claim_url": claim_url,
        "confidence": extracted.confidence,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }

    # Non-null voucher fields, mirroring build_voucher_email's table rows.
    field_map: dict[str, Any] = {
        "vendor": extracted.vendor,
        "promotion_name": extracted.promotion_name,
        "promotion_type": extracted.promotion_type,
        "certifications": extracted.certifications,
        "voucher_code": extracted.voucher_code,
        "discount": extracted.discount,
        "regions": extracted.regions,
        "start_date": extracted.start_date,
        "end_date": extracted.end_date,
        "reason": extracted.reason,
    }
    for key, value in field_map.items():
        if value not in (None, "", []):
            payload[key] = value

    return payload


async def send_bot_notification(post: Post, extracted: ExtractedEvent) -> bool:
    """POST a voucher alert to the configured bot server.

    Skips (False) when ``NOTIFICATION_BOT_SERVER_URL`` or ``WEBHOOK_SECRET``
    are not set — never raises.  Returns True when the server accepts the
    request (2xx).
    """
    if not settings.notification_bot_server_url:
        logger.warning(
            "bot_notification.send: NOTIFICATION_BOT_SERVER_URL not set — skipping",
            post_id=post.id,
        )
        return False
    if not settings.webhook_secret:
        logger.warning(
            "bot_notification.send: WEBHOOK_SECRET not set — skipping",
            post_id=post.id,
        )
        return False

    payload = build_voucher_payload(post, extracted)
    headers = {
        "Authorization": f"Bearer {settings.webhook_secret}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.post(
                settings.notification_bot_server_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "bot_notification.send: webhook POST failed",
            post_id=post.id,
            url=settings.notification_bot_server_url,
            error=str(exc)[:160],
        )
        return False

    logger.info(
        "bot_notification.send: voucher alert sent to bot server",
        post_id=post.id,
        status_code=response.status_code,
    )
    return True
