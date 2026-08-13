import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String,
    Integer,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from voucherbot.models.base import Base


class NotificationStatus(enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NotificationOutbox(Base):
    """Transactional outbox for voucher alert emails.

    Delivery intent is persisted here in the same transaction as the pipeline
    run. A background sweep (and the pipeline itself) retries PENDING rows until
    they are SENT, using a stable ``idempotency_key`` so replaying a row can
    never deliver a duplicate email.
    """

    __tablename__ = "notification_outbox"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_notification_outbox_idempotency_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    # Stable per (post, content) — e.g. "voucher:{post_id}:{content_hash}".
    # Passed to Resend as the Idempotency-Key header so retried deliveries
    # cannot duplicate emails. Unique (via __table_args__) to prevent double-stage.
    idempotency_key: Mapped[str] = mapped_column(String)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus),
        default=NotificationStatus.PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Rendered email snapshot so retries do not depend on re-running AI analysis.
    subject: Mapped[str] = mapped_column(String)
    html_body: Mapped[str] = mapped_column(Text)
    text_body: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    post = relationship("Post")
