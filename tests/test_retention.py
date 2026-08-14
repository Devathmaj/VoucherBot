"""Tests for content retention cleanup of the posts.content column."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import voucherbot.services.retention as retention


def _fake_session(rowcount: int = 0) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    session.commit = AsyncMock()
    return session


async def _run_purge(session: MagicMock) -> int:
    with patch("voucherbot.services.retention.session_scope") as scope:
        scope.return_value.__aenter__ = AsyncMock(return_value=session)
        scope.return_value.__aexit__ = AsyncMock(return_value=False)
        return await retention.purge_expired_post_content()


@pytest.mark.asyncio
async def test_purge_only_targets_content_of_old_posts() -> None:
    session = _fake_session(rowcount=3)
    rows = await _run_purge(session)

    assert rows == 3
    session.commit.assert_awaited_once()

    stmt = session.execute.await_args.args[0]
    sql = str(stmt)
    assert "UPDATE posts" in sql
    assert "SET content = NULL" in sql
    assert "created_at" in sql
    assert "cutoff" in str(stmt)


@pytest.mark.asyncio
async def test_purge_never_touches_other_columns() -> None:
    """The UPDATE's SET clause must contain exactly one column: content."""
    session = _fake_session(rowcount=1)
    await _run_purge(session)

    stmt = session.execute.await_args.args[0]
    sql = str(stmt)
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert set_clause.strip() == "content = NULL"
    assert "updated_at" not in set_clause
    assert "summary" not in set_clause


@pytest.mark.asyncio
async def test_purge_skips_already_null_content() -> None:
    session = _fake_session(rowcount=0)
    rows = await _run_purge(session)

    assert rows == 0
    stmt = session.execute.await_args.args[0]
    assert "IS NOT NULL" in str(stmt)


@pytest.mark.asyncio
async def test_purge_logs_and_swallows_errors() -> None:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        patch("voucherbot.services.retention.session_scope") as scope,
        patch("voucherbot.services.retention.logger.warning") as warn,
    ):
        scope.return_value.__aenter__ = AsyncMock(return_value=session)
        scope.return_value.__aexit__ = AsyncMock(return_value=False)
        rows = await retention.purge_expired_post_content()

    assert rows == 0
    warn.assert_called_once()
