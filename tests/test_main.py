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
