"""Tests for the transactional notification outbox."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voucherbot.models.notification import NotificationStatus
from voucherbot.services.ai.schema import ExtractedEvent
from voucherbot.services.email import notifications


def _settings(**overrides: object) -> SimpleNamespace:
    base = SimpleNamespace(email_id="admin@example.com", resend_api_key="re_test")
    base.__dict__.update(overrides)
    return base


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
        voucher_code=None,
        reason="Free Microsoft exam voucher.",
        certifications=["AZ-900"],
        registration_url=None,
    )


def _outbox_row(**kwargs: object) -> Any:
    base: dict[str, object] = dict(
        id=1,
        post_id=7,
        idempotency_key="voucher:7:hash123",
        status=NotificationStatus.PENDING,
        attempts=0,
        last_error=None,
        last_attempt_at=None,
        sent_at=None,
        subject="Voucher: AI Skills Fest",
        html_body="<p>hi</p>",
        text_body="hi",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_outbox_idempotency_key_stable_per_post_and_content() -> None:
    assert notifications._outbox_idempotency_key(_post()) == "voucher:7:hash123"
    # Changing the content hash yields a different key → update re-notifies.
    assert notifications._outbox_idempotency_key(_post(content_hash="hash456")) != (
        "voucher:7:hash123"
    )
    # Different post, same content → different key.
    assert notifications._outbox_idempotency_key(_post(id=8)) != ("voucher:7:hash123")


@pytest.mark.asyncio
async def test_stage_persists_pending_row_with_idempotency_key() -> None:
    db = AsyncMock()
    row = _outbox_row()
    select_result = MagicMock()
    select_result.scalars.return_value.first.return_value = row
    db.execute.side_effect = [MagicMock(), select_result]

    with patch("voucherbot.services.email.notifications.settings", _settings()):
        result = await notifications.stage_voucher_notification(
            db, _post(), _extracted()
        )

    assert result is row
    insert_stmt = db.execute.await_args_list[0].args[0]
    assert insert_stmt.table.name == "notification_outbox"
    values = {col.key: bind.value for col, bind in insert_stmt._values.items()}
    assert values["post_id"] == 7
    assert values["idempotency_key"] == "voucher:7:hash123"
    assert values["status"] == NotificationStatus.PENDING
    assert "AI Skills Fest" in values["subject"]


@pytest.mark.asyncio
async def test_stage_skips_when_email_unconfigured() -> None:
    db = AsyncMock()
    with patch(
        "voucherbot.services.email.notifications.settings",
        _settings(email_id=None),
    ):
        result = await notifications.stage_voucher_notification(
            db, _post(), _extracted()
        )

    assert result is None
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_marks_sent_and_notifies_post() -> None:
    session = AsyncMock()
    row = _outbox_row()
    batch = MagicMock()
    batch.scalars.return_value.all.return_value = [row]
    session.execute = AsyncMock(return_value=batch)
    send = AsyncMock(return_value={"id": "email_1"})

    with (
        patch("voucherbot.services.email.notifications.settings", _settings()),
        patch("voucherbot.services.email.notifications.send_email", new=send),
    ):
        delivered = await notifications.deliver_pending_notifications(session)

    assert delivered == 1
    assert row.status == NotificationStatus.SENT
    assert row.sent_at is not None
    assert row.last_attempt_at is not None
    assert row.last_error is None
    session.commit.assert_awaited_once()
    # send carried the stable idempotency key
    call = send.await_args
    assert call is not None
    assert call.kwargs["idempotency_key"] == "voucher:7:hash123"
    assert call.kwargs["to"] == "admin@example.com"
    # posts.is_notified updated in the same commit
    update_stmt = session.execute.await_args_list[1].args[0]
    assert update_stmt.table.name == "posts"
    assert str(update_stmt).find("is_notified") != -1


@pytest.mark.asyncio
async def test_deliver_failure_retries_then_fails_out() -> None:
    session = AsyncMock()
    row = _outbox_row(attempts=notifications._MAX_ATTEMPTS - 1)
    batch = MagicMock()
    batch.scalars.return_value.all.return_value = [row]
    session.execute = AsyncMock(return_value=batch)
    send = AsyncMock(return_value=None)

    with (
        patch("voucherbot.services.email.notifications.settings", _settings()),
        patch("voucherbot.services.email.notifications.send_email", new=send),
    ):
        delivered = await notifications.deliver_pending_notifications(session)

    assert delivered == 0
    assert row.status == NotificationStatus.FAILED
    assert row.attempts == notifications._MAX_ATTEMPTS
    assert row.last_error is not None
    assert row.sent_at is None
    session.commit.assert_awaited_once()
    # No posts.is_notified update on failure.
    assert len(session.execute.await_args_list) == 1


@pytest.mark.asyncio
async def test_deliver_keeps_pending_below_max_attempts() -> None:
    session = AsyncMock()
    row = _outbox_row(attempts=1)
    batch = MagicMock()
    batch.scalars.return_value.all.return_value = [row]
    session.execute = AsyncMock(return_value=batch)

    with (
        patch("voucherbot.services.email.notifications.settings", _settings()),
        patch(
            "voucherbot.services.email.notifications.send_email",
            new=AsyncMock(return_value=None),
        ),
    ):
        delivered = await notifications.deliver_pending_notifications(session)

    assert delivered == 0
    assert row.status == NotificationStatus.PENDING
    assert row.attempts == 2


@pytest.mark.asyncio
async def test_deliver_returns_zero_when_email_unconfigured() -> None:
    session = AsyncMock()
    with patch(
        "voucherbot.services.email.notifications.settings",
        _settings(email_id=None),
    ):
        delivered = await notifications.deliver_pending_notifications(session)

    assert delivered == 0
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_pending_notifications_never_raises() -> None:
    class _Ctx:
        async def __aenter__(self) -> AsyncMock:
            return AsyncMock()

        async def __aexit__(self, *args: object) -> bool:
            return False

    with (
        patch(
            "voucherbot.services.email.notifications.session_scope",
            return_value=_Ctx(),
        ),
        patch(
            "voucherbot.services.email.notifications.deliver_pending_notifications",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        # Must swallow the delivery error so the scheduler loop stays healthy.
        await notifications.retry_pending_notifications()
