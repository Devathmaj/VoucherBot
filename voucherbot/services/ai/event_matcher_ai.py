"""
AI-backed canonical event matching.

The qwen reasoning model decides whether an incoming ``ExtractedEvent``
describes the same real-world promotion as an existing canonical ``Event``.
This replaces the deterministic weighted scoring used for the merge decision in
the EventMatcher; deterministic scoring is retained only as a fallback when the
model is unavailable.

The decision output is an ``EventMatchDecision``: ``is_same_promotion`` plus a
0–1 ``confidence`` and a human-readable ``reason`` for the audit log.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

import structlog
from pydantic import BaseModel, field_validator

from voucherbot.config.settings import settings
from voucherbot.models.event import Event
from voucherbot.services.ai.analyzer import _GROQ_REASONER_MODEL, _call_groq_raw
from voucherbot.services.ai.schema import ExtractedEvent

logger = structlog.get_logger(__name__)

# Fields compared when the model decides whether two promotions are the same.
_FIELD_NAMES: tuple[str, ...] = (
    "vendor",
    "promotion_name",
    "promotion_type",
    "certifications",
    "voucher_code",
    "discount",
    "registration_url",
    "start_date",
    "end_date",
    "regions",
)

_MATCH_SYSTEM_PROMPT = (
    "You are a deduplication judge for a certification voucher aggregator. "
    "You are given two promotion records — EXISTING (an already-known "
    "promotion) and INCOMING (a newly detected promotion). Decide whether they "
    "describe the SAME real-world promotion.\n\n"
    "They are the SAME promotion when the same vendor offers the same deal for "
    "the same certification exams, even if wording, casing, URLs, tracking "
    "parameters, or publication dates differ. Strong indicators of sameness:\n"
    "- identical or near-identical voucher code or registration URL\n"
    "- identical vendor with the same or overlapping certification list\n"
    "- the same promotion name with minor wording differences\n"
    "- the same discount value (e.g. both '50% off AZ-900')\n\n"
    "They are DIFFERENT promotions when:\n"
    "- vendors differ\n"
    "- the certification exams differ\n"
    "- discounts differ materially (e.g. 50% off vs 80% off)\n"
    "- the date ranges do not overlap and describe distinct campaigns\n\n"
    "When uncertain, prefer is_same_promotion=false to avoid merging distinct "
    "promotions — a false merge destroys provenance.\n\n"
    "Respond with ONLY a valid JSON object matching this exact schema:\n"
    "{\n"
    '  "is_same_promotion": true | false,\n'
    '  "confidence": 0.0-1.0,\n'
    '  "reason": "string"\n'
    "}\n"
)


class EventMatchDecision(BaseModel):
    """Model output deciding whether two promotions are the same real-world event."""

    is_same_promotion: bool  # required: absence of the field is a parse failure
    confidence: float = 0.0  # 0.0 – 1.0, belief in the decision
    reason: Optional[str] = None

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


def _event_to_dict(event: Event) -> dict[str, Any]:
    """Serialize the comparable Event fields for the prompt (dates as ISO)."""

    def _serialise(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return {field: _serialise(getattr(event, field, None)) for field in _FIELD_NAMES}


def _extracted_to_dict(extracted: ExtractedEvent) -> dict[str, Any]:
    """Serialize the comparable ExtractedEvent fields for the prompt."""
    return {field: getattr(extracted, field, None) for field in _FIELD_NAMES}


def _parse_decision(raw_text: str) -> Optional[EventMatchDecision]:
    """Parse a raw provider response into an ``EventMatchDecision``.

    Handles accidental markdown fences.  Returns ``None`` on parse failure so
    callers can fall back to deterministic scoring.
    """
    try:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)

        data: dict[str, Any] = json.loads(text)
        return EventMatchDecision.model_validate(data)
    except Exception as exc:
        logger.warning(
            "ai.event_matcher: failed to parse match decision",
            error=str(exc),
            raw=raw_text[:300],
        )
        return None


def _build_match_messages(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> list[dict[str, str]]:
    """Build the qwen chat messages framing two records for a same-promotion judge."""
    prompt = (
        "EXISTING (already-known) promotion:\n"
        + json.dumps(existing, ensure_ascii=False, indent=2)
        + "\n\nINCOMING (newly detected) promotion:\n"
        + json.dumps(incoming, ensure_ascii=False, indent=2)
    )
    return [
        {"role": "system", "content": _MATCH_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


async def _ask_match_decision(
    messages: list[dict[str, str]],
) -> Optional[EventMatchDecision]:
    """Send match-judge messages to qwen and parse the decision."""
    raw_text = await _call_groq_raw(messages, _GROQ_REASONER_MODEL)
    if raw_text is None:
        return None
    return _parse_decision(raw_text)


async def compare_candidate(
    candidate: Event,
    extracted: ExtractedEvent,
) -> Optional[EventMatchDecision]:
    """Ask qwen whether ``extracted`` is the same promotion as ``candidate``.

    Returns ``None`` when no Groq key is configured, the model is unavailable,
    or the response cannot be parsed — callers then fall back to deterministic
    scoring.
    """
    if not settings.groq_api_key:
        return None
    messages = _build_match_messages(
        _event_to_dict(candidate), _extracted_to_dict(extracted)
    )
    return await _ask_match_decision(messages)


async def compare_events(
    existing: Event,
    incoming: Event,
) -> Optional[EventMatchDecision]:
    """Ask qwen whether two canonical Events are the same real-world promotion.

    Used by the periodic consolidation sweep to decide whether two already-
    created Events should be merged into one.  Same fallback semantics as
    ``compare_candidate``.
    """
    if not settings.groq_api_key:
        return None
    messages = _build_match_messages(_event_to_dict(existing), _event_to_dict(incoming))
    return await _ask_match_decision(messages)
