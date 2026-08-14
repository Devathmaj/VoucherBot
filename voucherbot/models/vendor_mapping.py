from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from voucherbot.models.base import Base


class VendorMapping(Base):
    __tablename__ = "vendor_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    url_pattern: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True, unique=True
    )
    source_name_pattern: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True, unique=True
    )
    vendor: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
