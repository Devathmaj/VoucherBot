from typing import Any

import structlog

from voucherbot.core.logging import redact_secrets, setup_logging


def _process(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_secrets(None, "info", dict(event)))


def test_redacts_sensitive_keys() -> None:
    event = _process({"api_key": "gsk_abc123", "level": "info"})
    assert event["api_key"] == "[REDACTED]"
    assert event["level"] == "info"


def test_redacts_query_param_credentials_in_urls() -> None:
    event = _process(
        {
            "url": (
                "https://api.example.com/oauth2/token"
                "?client_secret=CS123&refresh_token=RT456&auth_token=AT789"
            )
        }
    )
    assert "CS123" not in event["url"]
    assert "RT456" not in event["url"]
    assert "AT789" not in event["url"]


def test_redacts_secret_shaped_values() -> None:
    event = _process(
        {
            "error": (
                "connection failed: postgres://user:S3cretPass@db:5432/x"
                "?api_key=abcd1234"
            )
        }
    )
    assert "S3cretPass" not in event["error"]
    assert "abcd1234" not in event["error"]


def test_redacts_bearer_token() -> None:
    event = _process({"error": "Unauthorized Bearer eyJhbGciOiJIUzI1NiJ9.token"})
    assert "eyJhbGciOiJIUzI1NiJ9.token" not in event["error"]


def test_redacts_nested_config() -> None:
    event = _process(
        {
            "config": {
                "url": "https://x.com",
                "vendor": "AWS",
                "credentials": {"client_secret": "hunter2"},
            }
        }
    )
    assert event["config"]["vendor"] == "AWS"
    assert event["config"]["credentials"]["client_secret"] == "[REDACTED]"


def test_keeps_legitimate_values() -> None:
    event = _process(
        {
            "event": "fetch ok",
            "url": "https://example.com/blog/feed?category=aws",
            "day_tokens": 123,
            "used_tokens": 45,
            "model": "llama-3.3-70b",
            "to": "a@b.com",
        }
    )
    assert event["url"] == "https://example.com/blog/feed?category=aws"
    assert event["day_tokens"] == 123
    assert event["used_tokens"] == 45
    assert event["model"] == "llama-3.3-70b"
    assert event["to"] == "a@b.com"


def test_exception_message_is_redacted(caplog: Any) -> None:
    setup_logging()
    secret = "gsk_supersecrettokenabc123"
    logger = structlog.get_logger("test-redact-exc")
    try:
        raise RuntimeError(f"connection failed with key {secret}")
    except RuntimeError:
        logger.exception("request failed")

    rendered = (caplog.text or "").replace("\x1b[0m", "").replace("\x1b[31m", "")
    assert secret not in rendered
    assert "[REDACTED]" in rendered
