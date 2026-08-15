"""Tests for the bot webhook notification service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.bot_notification import notifier


def _post(**kwargs: object) -> Any:
    base: dict[str, object] = dict(
        id=7,
        title="Free AZ-900 this week",
        url="https://example.com/posts/1",
        summary=None,
        content_hash="hash123",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _extracted() -> ExtractedEvent:
    return ExtractedEvent(
        is_voucher=True,
        confidence=0.55,
        vendor="microsoft",
        promotion_name="AI Skills Fest",
        voucher_code="MS-LEARN-50",
        discount="50%",
        promotion_type="voucher",
        registrations=None,
        certifications=["AZ-900"],
        regions=["US"],
        start_date="2026-09-01",
        end_date="2026-09-30",
        reason="Free Microsoft exam voucher.",
    )


def _settings(**overrides: object) -> SimpleNamespace:
    base = SimpleNamespace(
        notification_bot_server_url="https://bot.example.com/webhook",
        webhook_secret="super-secret",
    )
    base.__dict__.update(overrides)
    return base


def test_build_voucher_payload_shape() -> None:
    payload = notifier.build_voucher_payload(_post(), _extracted())

    assert payload["event"] == "voucher_alert"
    assert payload["title"] == "Voucher: Microsoft — AI Skills Fest"
    assert payload["post"] == "https://example.com/posts/1"
    assert payload["claim_url"] == "https://example.com/posts/1"
    assert payload["confidence"] == 0.55
    assert payload["sent_at"]
    assert payload["vendor"] == "microsoft"
    assert payload["promotion_name"] == "AI Skills Fest"
    assert payload["promotion_type"] == "voucher"
    assert payload["certifications"] == ["AZ-900"]
    assert payload["voucher_code"] == "MS-LEARN-50"
    assert payload["discount"] == "50%"
    assert payload["regions"] == ["US"]
    assert payload["start_date"] == "2026-09-01"
    assert payload["end_date"] == "2026-09-30"


def test_build_voucher_payload_omits_nulls() -> None:
    payload = notifier.build_voucher_payload(_post(), ExtractedEvent(is_voucher=True))

    assert payload["event"] == "voucher_alert"
    assert "vendor" not in payload
    assert "promotion_name" not in payload
    assert "voucher_code" not in payload
    assert "discount" not in payload
    assert "certifications" not in payload
    assert "discount" not in payload


def test_build_voucher_payload_claim_url_falls_back_to_post() -> None:
    extracted = _extracted()
    extracted.registration_url = "https://aws.example.com/claim"
    payload = notifier.build_voucher_payload(_post(), extracted)

    assert payload["claim_url"] == "https://aws.example.com/claim"


@pytest.mark.asyncio
async def test_send_skips_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifier.settings, "notification_bot_server_url", None)
    result = await notifier.send_bot_notification(_post(), _extracted())
    assert result is False


@pytest.mark.asyncio
async def test_send_skips_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifier.settings, "webhook_secret", None)
    result = await notifier.send_bot_notification(_post(), _extracted())
    assert result is False


@pytest.mark.asyncio
async def test_send_posts_with_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        notifier.settings,
        "notification_bot_server_url",
        "https://bot.example.com/webhook",
    )
    monkeypatch.setattr(notifier.settings, "webhook_secret", "super-secret")

    class FakeResponse:
        status_code = 200
        raise_for_status = lambda self: None  # noqa: E731

    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(
            self, url: str, *, json: Any, headers: dict[str, str]
        ) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    with patch.object(notifier.httpx, "AsyncClient", FakeClient):
        result = await notifier.send_bot_notification(_post(), _extracted())

    assert result is True
    assert captured["url"] == "https://bot.example.com/webhook"
    assert captured["headers"] == {
        "Authorization": "Bearer super-secret",
        "Content-Type": "application/json",
    }
    assert captured["json"]["event"] == "voucher_alert"
    assert captured["json"]["promotion_name"] == "AI Skills Fest"


@pytest.mark.asyncio
async def test_send_returns_false_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_post(*args: Any, **kwargs: Any) -> None:
        raise httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "https://bot.example.com"),
            response=httpx.Response(500),
        )

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        post = _fail_post

    with patch.object(notifier.httpx, "AsyncClient", FakeClient):
        result = await notifier.send_bot_notification(_post(), _extracted())

    assert result is False
