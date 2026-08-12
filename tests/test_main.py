import pytest
from httpx import AsyncClient, ASGITransport
from voucherbot.main import app
from voucherbot.api import rate_limit


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
