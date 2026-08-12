import logging
import re
from typing import Any, Mapping, MutableMapping

import structlog

from voucherbot.config.settings import settings

_REDACTED = "[REDACTED]"

# Event keys that hold secrets directly (exact, case-insensitive match).
# Deliberately excludes bare "token"/"tokens" so diagnostic counters like
# day_tokens / used_tokens / token_times are not over-redacted.
_SENSITIVE_KEY = re.compile(
    r"^(api[_-]?key|apikey|client[_-]?secret|client_id|secret|password|passwd|"
    r"authorization|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"bearer[_-]?token|credential|private[_-]?key|database_url|dsn)$",
    re.IGNORECASE,
)

# Secret-shaped fragments that may be embedded inside free-text values
# (exception messages, URLs, raw payloads). Applied to every string.
_SECRET_IN_TEXT = [
    # Groq / Resend / Google / OpenAI-style / GitHub / Slack keys
    re.compile(r"gsk_[A-Za-z0-9_-]+"),
    re.compile(r"re_[A-Za-z0-9_-]+"),
    re.compile(r"AIza[A-Za-z0-9_-]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9_-]+"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    # Authorization header
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    # URL userinfo credentials: scheme://user:pass@host
    re.compile(r"//[^/@:]+:[^@/]+@"),
    # Sensitive query params: ?api_key=... / &access_token=...
    re.compile(
        r"(?i)([?&](?:api_key|apikey|token|access_token|auth|signature|sig|"
        r"password|secret)=)[^&\s]*"
    ),
]


def _redact_string(value: str) -> str:
    out = value
    for pattern in _SECRET_IN_TEXT:
        if pattern.search(out):
            out = pattern.sub(_REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY.match(key):
            out[key] = _REDACTED
        else:
            out[key] = _redact_value(value)
    return out


def redact_secrets(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """Structlog processor: mask secrets before anything is rendered or written.

    Masks both whole values under sensitive key names and secret-shaped
    fragments embedded inside arbitrary strings. Purely cosmetic — never
    alters application behavior or control flow.
    """
    return _redact_dict(dict(event_dict))


def setup_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_secrets,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    logging.basicConfig(
        format="%(message)s",
        stream=None,
        level=log_level,
    )
