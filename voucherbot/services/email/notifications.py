"""Voucher alert emails via Resend."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog

from voucherbot.config.settings import settings
from voucherbot.services.email.sender import send_email

if TYPE_CHECKING:
    from voucherbot.models.post import Post
    from voucherbot.services.ai.schema import ExtractedEvent

logger = structlog.get_logger(__name__)

_ALLOWED_URL_SCHEMES = ("http", "https", "mailto")


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
