"""Unit tests for database init / enum migration helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voucherbot.database import init_db as init
from voucherbot.database.init_db import _ensure_source_type_enum


class _FakeResult:
    """Minimal stand-in for a SQLAlchemy result/mapping."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)

    def scalar(self) -> Any:
        if not self._rows:
            return None
        return self._rows[0][0] if isinstance(self._rows[0], tuple) else self._rows[0]


@pytest.mark.asyncio
async def test_ensure_source_type_enum_skips_when_type_missing() -> None:
    conn = AsyncMock()
    conn.execution_options.return_value = conn
    conn.scalar.return_value = False
    engine = MagicMock()
    engine.connect.return_value = conn

    with patch.object(init, "engine", engine):
        await _ensure_source_type_enum()

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_source_type_enum_adds_missing_values_only() -> None:
    calls: list[str] = []

    async def fake_execute(statement: Any) -> Any:
        calls.append(str(statement))
        if "enum_range" in str(statement):
            return _FakeResult([("REDDIT",), ("RSS",), ("BLOG",)])
        return _FakeResult([])

    conn = AsyncMock()
    conn.execution_options.return_value = conn
    conn.scalar.return_value = True
    conn.execute.side_effect = fake_execute
    engine = MagicMock()
    engine.connect.return_value = conn

    with patch.object(init, "engine", engine):
        await _ensure_source_type_enum()

    alter_sql = [c for c in calls if c.startswith("ALTER TYPE")]
    assert alter_sql == [
        "ALTER TYPE sourcetype ADD VALUE 'PEARSONVUE'",
        "ALTER TYPE sourcetype ADD VALUE 'TRAINING_PROVIDER'",
    ]
    assert "REDDIT" not in alter_sql


@pytest.mark.asyncio
async def test_ensure_source_type_enum_closes_connection() -> None:
    conn = AsyncMock()
    conn.execution_options.return_value = conn
    conn.scalar.return_value = True
    conn.execute.return_value = _FakeResult([])
    engine = MagicMock()
    engine.connect.return_value = conn

    with patch.object(init, "engine", engine):
        await _ensure_source_type_enum()

    conn.close.assert_awaited_once()
