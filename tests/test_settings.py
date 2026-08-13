"""Unit tests for the pydantic-settings configuration layer.

These construct ``Settings`` instances directly with an explicit
``database_url`` and ignore the ambient ``.env`` file plus process
environment so the tests are fully deterministic.  The module-level
``settings`` singleton is left untouched.
"""

from __future__ import annotations

from typing import Generator

import pytest
from pydantic import ValidationError

from voucherbot.config.settings import EventMatcherConfig, Settings

_DB_URL = "postgresql+asyncpg://user:pass@localhost:5432/voucherbot"

_ENV_KEYS = [
    "IS_PROD",
    "IS_TEST",
    "LOG_LEVEL",
    "DATABASE_URL",
    "HEALTH_RATE_LIMIT_PER_MINUTE",
    "RATE_LIMIT_TRUSTED_PROXIES",
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "EMAIL_ID",
    "EMAIL_MIN_INTERVAL_SECONDS",
    "EMAIL_REPLY_TO",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
    "REDDIT_FETCH_INTERVAL_MINUTES",
    "REDDIT_CONCURRENCY_LIMIT",
    "REDDIT_FETCH_LIMIT",
    "REDDIT_INGESTION_ENABLED",
    "SCRAPER_USER_AGENT",
    "SCRAPER_CONTACT_EMAIL",
    "SCRAPER_RESPECT_ROBOTS",
    "SCRAPER_MIN_DELAY_SECONDS",
    "TICK_LEASE_TTL_SECONDS",
    "TICK_JOB_TIMEOUT_SECONDS",
    "SOURCE_BACKOFF_BASE_MINUTES",
    "SOURCE_BACKOFF_MAX_MINUTES",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "GROQ_REQUESTS_PER_MINUTE",
    "GROQ_TOKENS_PER_MINUTE",
    "GROQ_MAX_COMPLETION_TOKENS",
    "GROQ_MAX_INPUT_CHARS",
]


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _settings(**overrides: object) -> Settings:
    return Settings(database_url=_DB_URL, _env_file=None, **overrides)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_minimal_settings_requires_database_url() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_applied() -> None:
    s = _settings()
    assert s.is_prod is False
    assert s.database_url == _DB_URL
    assert s.health_rate_limit_per_minute == 60
    assert s.email_from == "VoucherBot <onboarding@resend.dev>"
    assert s.email_min_interval_seconds == 5.0
    assert s.reddit_fetch_limit == 25
    assert s.scraper_respect_robots is True
    assert s.scraper_min_delay_seconds == 2.0
    assert s.groq_requests_per_minute == 30
    assert s.groq_max_completion_tokens == 1024


def test_optional_keys_default_to_none() -> None:
    s = _settings()
    for field in (
        "resend_api_key",
        "email_id",
        "reddit_client_id",
        "gemini_api_key",
        "groq_api_key",
        "scraper_user_agent",
        "scraper_contact_email",
    ):
        assert getattr(s, field) is None


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------


def test_empty_string_becomes_none() -> None:
    s = _settings(tick_job_timeout_seconds="", groq_tokens_per_minute="")
    assert s.tick_job_timeout_seconds is None
    assert s.groq_tokens_per_minute is None


def test_numeric_values_are_preserved() -> None:
    s = _settings(tick_job_timeout_seconds=300, groq_tokens_per_minute=8000)
    assert s.tick_job_timeout_seconds == 300
    assert s.groq_tokens_per_minute == 8000


def test_trusted_proxies_comma_separated() -> None:
    s = _settings(rate_limit_trusted_proxies="127.0.0.1, 10.0.0.1, proxy.example.com")
    assert s.rate_limit_trusted_proxies == [
        "127.0.0.1",
        "10.0.0.1",
        "proxy.example.com",
    ]


def test_trusted_proxies_list_passthrough() -> None:
    s = _settings(rate_limit_trusted_proxies=["127.0.0.1"])
    assert s.rate_limit_trusted_proxies == ["127.0.0.1"]


# ---------------------------------------------------------------------------
# Event matcher config
# ---------------------------------------------------------------------------


def test_event_matcher_defaults() -> None:
    s = _settings()
    assert s.event_matcher.auto_merge_threshold == 70
    assert s.event_matcher.possible_match_threshold == 45
    assert s.event_matcher.candidate_limit == 100
    assert s.event_matcher.name_similarity_threshold == 0.60
    assert s.event_matcher.weight_registration_url == 50
    assert s.event_matcher.weight_voucher_code == 40


def test_event_matcher_override() -> None:
    s = _settings(
        event_matcher=EventMatcherConfig(
            auto_merge_threshold=80,
            possible_match_threshold=50,
            name_similarity_threshold=0.75,
        )
    )
    assert s.event_matcher.auto_merge_threshold == 80
    assert s.event_matcher.possible_match_threshold == 50
    assert s.event_matcher.name_similarity_threshold == 0.75


def test_source_priority_order_is_stable() -> None:
    from voucherbot.config.settings import SOURCE_PRIORITY

    assert SOURCE_PRIORITY[0] == "WEBSITE"
    assert "REDDIT" in SOURCE_PRIORITY
    assert SOURCE_PRIORITY[-1] == "API"
