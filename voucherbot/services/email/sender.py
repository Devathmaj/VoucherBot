"""
Email sender service using the Resend API.

Usage:
    from voucherbot.services.email.sender import send_email

    await send_email(
        to="user@example.com",
        subject="New Voucher Found!",
        html="<p>We found a voucher for you.</p>",
    )
"""

import asyncio
import time
import structlog
from typing import Any, Optional, cast

import resend

from voucherbot.config.settings import settings

logger = structlog.get_logger(__name__)

_initialized = False
_send_lock = asyncio.Lock()
_last_send_at = 0.0


def _init() -> None:
    global _initialized
    if not settings.resend_api_key:
        logger.warning("email.sender: RESEND_API_KEY not set - email will be skipped.")
        return

    resend.api_key = settings.resend_api_key
    _initialized = True
    logger.info("email.sender: Resend client initialized.")


_init()


async def send_test_email() -> dict[str, Any] | None:
    """
    Send a test email to the configured EMAIL_ID to verify the Resend setup.

    Returns the Resend API response dict or None on failure / if not configured.
    """
    if not settings.email_id:
        logger.warning("email.sender: EMAIL_ID not set — skipping test email.")
        return None

    subject = "VoucherBot — Test email"
    html = (
        "<div style='font-family:system-ui,sans-serif;line-height:1.45;color:#111;max-width:560px'>"
        "<h2 style='margin:0 0 12px'>VoucherBot — Test email</h2>"
        "<p style='margin:0 0 8px'>This is a test email from VoucherBot. "
        "If you received this, the Resend email configuration is working correctly.</p>"
        "</div>"
    )
    text = "This is a test email from VoucherBot. If you received this, the Resend email configuration is working correctly."

    result = await send_email(
        to=settings.email_id, subject=subject, html=html, text=text
    )
    if result is not None:
        logger.info(
            "email.sender: test email sent successfully",
            to=settings.email_id,
            resend_id=result.get("id"),
        )
    return result


async def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    text: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any] | None:
    """
    Send an email via Resend.

    Args:
        to:              Recipient address(es).
        subject:         Email subject line.
        html:            HTML body.
        text:            Optional plain-text fallback body.
        idempotency_key: Optional stable key sent as the ``Idempotency-Key``
            header so Resend won't process the same operation twice on retry.

    Returns:
        The Resend API response dict (contains 'id') or None on failure.
    """
    if not _initialized:
        logger.warning("email.sender: skipping send - not initialized.")
        return None

    params: resend.Emails.SendParams = {
        "from": settings.email_from,
        "to": [to] if isinstance(to, str) else to,
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text
    if settings.email_reply_to:
        params["reply_to"] = settings.email_reply_to

    options: resend.Emails.SendOptions | None = None
    if idempotency_key:
        options = {"idempotency_key": idempotency_key}

    try:
        async with _send_lock:
            global _last_send_at
            elapsed = time.monotonic() - _last_send_at
            delay = settings.email_min_interval_seconds - elapsed
            if delay > 0:
                logger.info(
                    "email.sender: throttling",
                    delay_seconds=round(delay, 2),
                    to=to,
                )
                await asyncio.sleep(delay)

            def _send() -> dict[str, Any]:
                return cast(dict[str, Any], resend.Emails.send(params, options))

            result = await asyncio.to_thread(_send)
            _last_send_at = time.monotonic()
        logger.info("email.sender: sent", to=to, subject=subject, id=result.get("id"))
        return result

    except Exception as exc:
        logger.error(
            "email.sender: failed to send", to=to, subject=subject, error=str(exc)
        )
        return None
