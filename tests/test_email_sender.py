"""Unit tests for the Resend email sender params."""

from __future__ import annotations

from typing import Any, Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from voucherbot.services.email import sender


@pytest.fixture(autouse=True)
def _reset_sender_state() -> Generator[None, None, None]:
    sender._initialized = True
    sender._last_send_at = 0.0
    yield
    sender._initialized = False


def _settings(**overrides: object) -> SimpleNamespace:
    base = SimpleNamespace(
        resend_api_key="re_test",
        email_from="VoucherBot <onboarding@resend.dev>",
        email_min_interval_seconds=0.0,
        email_reply_to=None,
    )
    base.__dict__.update(overrides)
    return base


@pytest.mark.asyncio
async def test_send_email_includes_reply_to_when_configured() -> None:
    captured: dict[str, Any] = {}

    def _fake_send(params: Any, options: Any = None) -> dict[str, Any]:
        captured.clear()
        captured.update(dict(params))
        return {"id": "email_123"}

    with (
        patch(
            "voucherbot.services.email.sender.settings",
            _settings(email_reply_to="reply@example.com"),
        ),
        patch("resend.Emails.send", new=_fake_send),
        patch("asyncio.to_thread", side_effect=lambda fn: fn()),
    ):
        result = await sender.send_email(
            to="user@example.com", subject="Hi", html="<p>hi</p>"
        )

    assert result == {"id": "email_123"}
    assert captured["reply_to"] == "reply@example.com"


@pytest.mark.asyncio
async def test_send_email_sends_supported_fields_by_default() -> None:
    captured: dict[str, Any] = {}

    def _fake_send(params: Any, options: Any = None) -> dict[str, Any]:
        captured.clear()
        captured.update(dict(params))
        return {"id": "email_124"}

    with (
        patch("voucherbot.services.email.sender.settings", _settings()),
        patch("resend.Emails.send", new=_fake_send),
        patch("asyncio.to_thread", side_effect=lambda fn: fn()),
    ):
        result = await sender.send_email(
            to="user@example.com", subject="Hi", html="<p>hi</p>", text="hi"
        )

    assert result == {"id": "email_124"}
    assert "reply_to" not in captured
    assert "track_opens" not in captured
    assert "track_clicks" not in captured
    assert captured["text"] == "hi"


@pytest.mark.asyncio
async def test_send_email_forwards_idempotency_key() -> None:
    captured: dict[str, Any] = {}

    def _fake_send(params: Any, options: Any = None) -> dict[str, Any]:
        captured.clear()
        captured.update(dict(params))
        captured["options"] = options
        return {"id": "email_125"}

    with (
        patch("voucherbot.services.email.sender.settings", _settings()),
        patch("resend.Emails.send", new=_fake_send),
        patch("asyncio.to_thread", side_effect=lambda fn: fn()),
    ):
        result = await sender.send_email(
            to="user@example.com",
            subject="Hi",
            html="<p>hi</p>",
            idempotency_key="voucher:1:abc",
        )

    assert result == {"id": "email_125"}
    assert captured["options"] == {"idempotency_key": "voucher:1:abc"}


@pytest.mark.asyncio
async def test_send_email_skips_when_not_initialized() -> None:
    sender._initialized = False
    with patch("voucherbot.services.email.sender.settings", _settings()):
        result = await sender.send_email(
            to="user@example.com", subject="Hi", html="<p>hi</p>"
        )
    assert result is None


def test_init_skips_without_api_key() -> None:
    sender._initialized = False
    with patch(
        "voucherbot.services.email.sender.settings", _settings(resend_api_key=None)
    ):
        sender._init()
    assert sender._initialized is False


def test_init_initializes_with_api_key() -> None:
    sender._initialized = False
    with (
        patch("voucherbot.services.email.sender.settings", _settings()),
        patch("resend.api_key", "re_test"),
    ):
        sender._init()
    assert sender._initialized is True


@pytest.mark.asyncio
async def test_send_test_email_skips_without_email_id() -> None:
    with (
        patch("voucherbot.services.email.sender.settings", _settings(email_id=None)),
        patch(
            "voucherbot.services.email.sender.send_email",
            new=AsyncMock(),
        ) as send,
    ):
        result = await sender.send_test_email()

    assert result is None
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_test_email_sends_when_configured() -> None:
    with (
        patch(
            "voucherbot.services.email.sender.settings",
            _settings(email_id="test@example.com"),
        ),
        patch(
            "voucherbot.services.email.sender.send_email",
            new=AsyncMock(return_value={"id": "email_t1"}),
        ) as send,
    ):
        result = await sender.send_test_email()

    assert result == {"id": "email_t1"}
    assert send.await_count == 1
    call = send.await_args
    assert call is not None
    assert call.kwargs["to"] == "test@example.com"
    assert "test email" in call.kwargs["subject"].lower()
    assert "test email" in call.kwargs["text"].lower()
