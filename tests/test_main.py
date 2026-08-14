import pytest
from contextlib import asynccontextmanager
from httpx import AsyncClient, ASGITransport
from typing import AsyncIterator, Generator
from unittest.mock import AsyncMock, MagicMock

from alembic.config import Config

from voucherbot.main import app
from voucherbot.main import PROJECT_ROOT, run_migrations
from voucherbot.api import rate_limit
from voucherbot.database.connection import get_session


@asynccontextmanager
async def _fake_session_scope() -> AsyncIterator[MagicMock]:
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    yield session


@pytest.fixture(autouse=True)
def _override_db_session() -> Generator[None, None, None]:
    """Provide a fake DB session so /health works without a live database."""
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock())
    app.dependency_overrides[get_session] = lambda: fake_session
    yield
    app.dependency_overrides.clear()


def test_run_migrations_points_at_project_head(monkeypatch: pytest.MonkeyPatch) -> None:
    import voucherbot.main as main_module

    captured: dict[str, object] = {}

    def fake_upgrade(cfg: Config, revision: str) -> None:
        captured["revision"] = revision
        captured["script_location"] = cfg.get_main_option("script_location")
        captured["config_file_name"] = cfg.config_file_name

    monkeypatch.setattr(
        main_module.alembic_command,  # type: ignore[attr-defined]
        "upgrade",
        fake_upgrade,
    )

    run_migrations()

    assert captured["revision"] == "head"
    assert captured["config_file_name"] is None
    assert captured["script_location"] is not None
    assert str(PROJECT_ROOT / "migrations") in str(captured["script_location"])


@pytest.mark.asyncio
async def test_lifespan_runs_migrations_when_not_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voucherbot.main as main_module

    calls: dict[str, int] = {"migrations": 0, "bootstrap": 0}

    def fake_run_migrations() -> None:
        calls["migrations"] += 1

    async def fake_bootstrap_data() -> None:
        calls["bootstrap"] += 1

    monkeypatch.setattr(
        main_module.settings,  # type: ignore[attr-defined]
        "is_prod",
        False,
    )
    monkeypatch.setattr(main_module, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(main_module, "bootstrap_data", fake_bootstrap_data)
    monkeypatch.setattr(main_module, "set_process_boot_at", lambda: None)
    monkeypatch.setattr(main_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(main_module, "reset_lease", AsyncMock())
    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "stop_scheduler", AsyncMock())
    monkeypatch.setattr(main_module, "send_test_email", AsyncMock())

    async with main_module.lifespan(main_module.app):
        pass

    assert calls == {"migrations": 1, "bootstrap": 1}


@pytest.mark.asyncio
async def test_lifespan_skips_migrations_when_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voucherbot.main as main_module

    calls: dict[str, int] = {"migrations": 0, "bootstrap": 0}

    def fake_run_migrations() -> None:
        calls["migrations"] += 1

    async def fake_bootstrap_data() -> None:
        calls["bootstrap"] += 1

    monkeypatch.setattr(
        main_module.settings,  # type: ignore[attr-defined]
        "is_prod",
        True,
    )
    monkeypatch.setattr(main_module, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(main_module, "bootstrap_data", fake_bootstrap_data)
    monkeypatch.setattr(main_module, "set_process_boot_at", lambda: None)
    monkeypatch.setattr(main_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(main_module, "reset_lease", AsyncMock())
    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "stop_scheduler", AsyncMock())
    monkeypatch.setattr(main_module, "send_test_email", AsyncMock())

    async with main_module.lifespan(main_module.app):
        pass

    assert calls == {"migrations": 0, "bootstrap": 0}


@pytest.mark.asyncio
async def test_health_check() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "health_rate_limit_per_minute", 2)
    rate_limit._reset_limiter()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        first = await ac.get("/health")
        second = await ac.get("/health")
        blocked = await ac.get("/health")
    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"


@pytest.mark.asyncio
async def test_health_rate_limit_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "health_rate_limit_per_minute", 0)
    rate_limit._reset_limiter()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        responses = [await ac.get("/health") for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)


@pytest.mark.asyncio
async def test_health_rate_limit_ignores_untrusted_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit.settings, "health_rate_limit_per_minute", 2)
    rate_limit._reset_limiter()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        headers = {"x-forwarded-for": "1.2.3.4"}
        responses = [await ac.get("/health", headers=headers) for _ in range(3)]
    assert responses[0].status_code == 200
    assert responses[1].status_code == 200
    # XFF must NOT be trusted: same client.host key, so it stays limited.
    assert responses[2].status_code == 429


@pytest.mark.asyncio
async def test_health_rate_limit_uses_xff_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit.settings, "health_rate_limit_per_minute", 2)
    monkeypatch.setattr(
        rate_limit.settings, "rate_limit_trusted_proxies", ["127.0.0.1"]
    )
    rate_limit._reset_limiter()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        blocked = await ac.get("/health", headers={"x-forwarded-for": "1.2.3.4"})
        blocked_again = await ac.get("/health", headers={"x-forwarded-for": "1.2.3.4"})
        blocked_thrice = await ac.get("/health", headers={"x-forwarded-for": "1.2.3.4"})
    assert blocked.status_code == 200
    assert blocked_again.status_code == 200
    assert blocked_thrice.status_code == 429


@pytest.mark.asyncio
async def test_health_rate_limit_cache_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit.settings, "health_rate_limit_per_minute", 100)
    rate_limit._reset_limiter()
    original_max = rate_limit._MAX_CLIENT_KEYS
    rate_limit._MAX_CLIENT_KEYS = 5
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            for i in range(20):
                response = await ac.get(
                    "/health", headers={"x-forwarded-for": f"10.0.0.{i}"}
                )
        assert response.status_code == 200
        assert len(rate_limit._hits) <= 5
    finally:
        rate_limit._MAX_CLIENT_KEYS = original_max
