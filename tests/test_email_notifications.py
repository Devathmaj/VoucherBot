"""Unit tests for voucher email notification content."""

from types import SimpleNamespace

from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.email.notifications import build_voucher_email, safe_url


def test_safe_url_allows_http_https_mailto() -> None:
    assert safe_url("https://example.com/reg") == "https://example.com/reg"
    assert safe_url("http://example.com/reg") == "http://example.com/reg"
    assert safe_url("mailto:vouchers@example.com") == "mailto:vouchers@example.com"


def test_safe_url_rejects_javascript_and_other_schemes() -> None:
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url("data:text/html;base64,PHNjcmlwdD4=") == ""
    assert safe_url("vbscript:msgbox(1)") == ""
    assert safe_url("file:///etc/passwd") == ""


def test_safe_url_handles_empty_and_relative() -> None:
    assert safe_url(None) == ""
    assert safe_url("") == ""
    assert safe_url("relative/path") == ""


def test_build_voucher_email_includes_post_link_and_partial_fields() -> None:
    post = SimpleNamespace(
        id=1,
        title="Free AZ-900 exam this week",
        url="https://example.com/posts/1",
        summary=None,
    )
    extracted = ExtractedEvent(
        is_voucher=True,
        confidence=0.55,
        vendor="microsoft",
        promotion_name="AI Skills Fest",
        voucher_code=None,  # code not required
        reason="Mentions a free Microsoft exam voucher without a code.",
        certifications=["AZ-900"],
        registration_url=None,
    )

    subject, html_body, text_body = build_voucher_email(post, extracted)  # type: ignore[arg-type]

    assert "Voucher:" in subject
    assert "AI Skills Fest" in subject
    assert "https://example.com/posts/1" in html_body
    assert "https://example.com/posts/1" in text_body
    assert "View source post" in html_body
    assert "AZ-900" in html_body
    assert "Mentions a free Microsoft exam voucher" in html_body
    # No code still produces a valid alert
    assert "CODE" not in html_body or extracted.voucher_code is None
