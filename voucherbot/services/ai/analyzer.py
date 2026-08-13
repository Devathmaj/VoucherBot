"""
AI Analyzer service — provider-agnostic structured extraction.

Architecture
------------
The public function ``analyze_post`` accepts raw post text and returns a
canonical ``ExtractedEvent`` (defined in ``voucherbot.services.ai.schema``).

Internally, providers are tried in priority order:
  1. Groq / llama-3.1-8b-instant  (primary)
  2. Groq / openai/gpt-oss-120b   (fallback on non-429 failure)
  3. Gemini                        (final fallback on non-429 failure)

Each adapter is responsible for converting its raw provider response into an
``ExtractedEvent``.  The pipeline NEVER depends on which provider responded.

Retry logic on 429 rate-limit errors is preserved from the original
implementation.
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import re
import time
import structlog
from typing import Any

from google import genai
from groq import AsyncGroq

from voucherbot.config.settings import settings
from voucherbot.services.ai.schema import ExtractedEvent

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared prompt
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a structured data extractor for a certification voucher aggregator.\n"
    "Decide whether the post shows promotional intent — an offer, discount, "
    "or deal for certification exams.\n\n"
    "Set is_voucher=true when the content offers something of value:\n"
    "- Discounts: '50% off', 'save $100', '20% discount on certification'\n"
    "- Free exams: 'free exam', 'complimentary voucher', 'no cost'\n"
    "- Voucher/reward programs: 'earn rewards', 'earn exam credits', "
    "'get a voucher when you'\n"
    "- Limited-time promotions: 'limited time offer', 'special promotion'\n"
    "- Giveaways/coupons: 'voucher giveaway', 'promo code', 'coupon'\n\n"
    "Set is_voucher=false when there is NO promotional offer, even if the "
    "content is certification-related:\n"
    "- Study guides, learning paths, training courses, study materials\n"
    "- General certification info, exam overviews, certification news\n"
    "- Forum discussions about exams, exam difficulty, study tips\n"
    "- Podcasts, blog posts, articles about certification topics\n"
    "- Job postings, career advice, industry trends\n"
    "- Pure e-commerce: pages where you buy a voucher at full price with "
    "no discount, special offer, or promotion — a storefront is not a promotion\n"
    "- 'Purchase vouchers', 'buy exam vouchers', 'order your voucher' — "
    "these are standard transactions, not promotions\n\n"
    "Nuance:\n"
    "- 'earn' alone ('earn your certification', 'earn a badge') = NOT promotional\n"
    "- 'earn rewards', 'earn discounts', 'earn vouchers', 'earn credits' = "
    "IS promotional (offering something of value)\n"
    "- 'credit' in 'exam credit', 'voucher credit' = promotional\n"
    "- 'credit' in 'CPE credits', 'CE credits', 'college credit' = NOT promotional\n"
    "- A page that sells vouchers without any discount or special offer = "
    "NOT promotional (just a store)\n\n"
    "Confidence: 0.0–1.0. Use >0.7 for clear offers with dollar amounts or "
    "percentages. Use 0.3–0.7 for vague or conditional offers. Use <0.3 for "
    "weak signals.\n\n"
    "For registration_url: extract the most specific action/registration link for "
    "the promotion. When multiple URLs are present, prefer the direct registration "
    "or 'START NOW'/'Sign up'/'Register' link over generic informational pages "
    "(e.g., vendor certification pages, course catalog pages). The redemption page "
    "for a voucher code or the promotion landing page is ideal. Use null if no "
    "relevant URL is found.\n\n"
    "Respond with ONLY a valid JSON object matching this exact schema. If a field is unknown or not mentioned, use the JSON `null` literal (do not use strings like 'not mentioned'). Do NOT include comments.\n"
    "{\n"
    '  "is_voucher": true | false,\n'
    '  "confidence": 0.0–1.0,\n'
    '  "reason": "string",\n'
    '  "vendor": "string or null",\n'
    '  "promotion_name": "string or null",\n'
    '  "promotion_type": "string or null",\n'
    '  "certifications": ["string"] or null,\n'
    '  "voucher_code": "string or null",\n'
    '  "discount": "string or null",\n'
    '  "registration_url": "string or null",\n'
    '  "start_date": "YYYY-MM-DD or null",\n'
    '  "end_date": "YYYY-MM-DD or null",\n'
    '  "regions": ["string"] or null\n'
    "}\n"
)

# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3
_FALLBACK_WAIT_S = 65
_GROQ_PRIMARY_MODEL = "llama-3.1-8b-instant"
_GROQ_FALLBACK_MODEL = "openai/gpt-oss-120b"
_GROQ_TERTIARY_MODEL = "llama-3.3-70b-versatile"
# Batch rotation order — posts distributed round-robin across these.
# llama-3.3-70b-versatile is last (index 2) to conserve its tight 100K TPD.
_GROQ_BATCH_MODELS = [_GROQ_PRIMARY_MODEL, _GROQ_FALLBACK_MODEL, _GROQ_TERTIARY_MODEL]

_GROQ_MODEL_TPM = {
    "openai/gpt-oss-120b": 8000,
    "openai/gpt-oss-20b": 8000,
    "llama-3.3-70b-versatile": 12000,
    "llama-3.1-8b-instant": 6000,
}
_GROQ_MODEL_TPD = {
    "openai/gpt-oss-120b": 200_000,
    "openai/gpt-oss-20b": 200_000,
    "llama-3.3-70b-versatile": 100_000,
    "llama-3.1-8b-instant": 500_000,
}
_GROQ_MODEL_RPD = {
    "openai/gpt-oss-120b": 1_000,
    "openai/gpt-oss-20b": 1_000,
    "llama-3.3-70b-versatile": 1_000,
    "llama-3.1-8b-instant": 14_400,
}

# Global cap: max concurrent AI calls across all models combined.
# Bounds memory from in-flight prompt strings (~12KB each) to ~48KB total.
_GLOBAL_AI_SEMAPHORE = asyncio.Semaphore(4)


class _ModelRateState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        # Per-minute sliding window
        self.request_times: deque[float] = deque()
        self.token_times: deque[tuple[float, int]] = deque()
        self.reservations: dict[int, tuple[float, int]] = {}
        self.counter: int = 0
        # Per-day counters (reset at UTC midnight)
        self.day_tokens: int = 0
        self.day_requests: int = 0
        self.day_date: str = ""  # YYYY-MM-DD
        self.daily_exhausted: bool = False

    def _refresh_day(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.day_date != today:
            self.day_date = today
            self.day_tokens = 0
            self.day_requests = 0
            self.daily_exhausted = False


_model_state: dict[str, _ModelRateState] = {}


def _get_model_state(model: str) -> _ModelRateState:
    if model not in _model_state:
        _model_state[model] = _ModelRateState()
    return _model_state[model]


def is_model_available(model: str) -> bool:
    """Return False if the model has hit its daily limit today."""
    state = _get_model_state(model)
    state._refresh_day()
    return not state.daily_exhausted


def _parse_retry_delay(error_str: str) -> float:
    match = re.search(r"retryDelay['\"]:\s*['\"](\\d+(?:\\.\\d+)?)s['\"]", error_str)
    if match:
        return min(float(match.group(1)) + 2, 70)
    return _FALLBACK_WAIT_S


def _groq_tokens_per_minute(model: str) -> int:
    if settings.groq_tokens_per_minute:
        return settings.groq_tokens_per_minute
    return _GROQ_MODEL_TPM.get(model, 6000)


def _estimate_tokens(text: str) -> int:
    # ~4 chars/token is realistic for English; cap completion buffer at 512.
    return max(1, len(text) // 4) + min(settings.groq_max_completion_tokens, 512)


async def _wait_for_groq_budget(estimated_tokens: int, model: str) -> int:
    """Reserve per-model per-minute budget; returns reservation ID. Raises RuntimeError if daily limit hit."""
    state = _get_model_state(model)
    rpm = max(1, settings.groq_requests_per_minute)
    tpm = max(1, _groq_tokens_per_minute(model))
    tpd = _GROQ_MODEL_TPD.get(model, 500_000)
    rpd = _GROQ_MODEL_RPD.get(model, 14_400)
    estimated_tokens = min(estimated_tokens, tpm)

    async with state.lock:
        state._refresh_day()
        if state.daily_exhausted:
            raise RuntimeError(f"daily_limit:{model}")
        if state.day_tokens + estimated_tokens > tpd or state.day_requests >= rpd:
            state.daily_exhausted = True
            logger.warning(
                "ai.analyzer: daily limit reached",
                model=model,
                day_tokens=state.day_tokens,
                day_requests=state.day_requests,
            )
            raise RuntimeError(f"daily_limit:{model}")

        while True:
            now = time.monotonic()
            cutoff = now - 60
            while state.request_times and state.request_times[0] <= cutoff:
                state.request_times.popleft()
            while state.token_times and state.token_times[0][0] <= cutoff:
                state.token_times.popleft()
            stale = [
                rid for rid, (ts, _) in state.reservations.items() if now - ts > 120
            ]
            for rid in stale:
                state.reservations.pop(rid, None)

            reserved = sum(t for _, t in state.reservations.values())
            used_tokens = sum(t for _, t in state.token_times) + reserved
            has_request_budget = len(state.request_times) < rpm
            has_token_budget = used_tokens + estimated_tokens <= tpm

            if has_request_budget and has_token_budget:
                state.request_times.append(now)
                state.counter += 1
                rid = state.counter
                state.reservations[rid] = (now, estimated_tokens)
                return rid

            wait_until = now + 1
            if not has_request_budget and state.request_times:
                wait_until = max(wait_until, state.request_times[0] + 60)
            if not has_token_budget and state.token_times:
                wait_until = max(wait_until, state.token_times[0][0] + 60)

            wait_seconds = max(1.0, wait_until - now)
            logger.info(
                "ai.analyzer: waiting for Groq rate budget",
                wait_seconds=round(wait_seconds, 2),
                model=model,
                rpm=rpm,
                tpm=tpm,
                estimated_tokens=estimated_tokens,
                used_tokens=used_tokens,
            )
            await asyncio.sleep(wait_seconds)


async def _settle_groq_budget(
    reservation_id: int, actual_tokens: int, model: str
) -> None:
    """Replace reservation with actual token count and update daily counters."""
    state = _get_model_state(model)
    tpd = _GROQ_MODEL_TPD.get(model, 500_000)
    rpd = _GROQ_MODEL_RPD.get(model, 14_400)
    async with state.lock:
        if state.reservations.pop(reservation_id, None) is not None:
            # Use current time so the entry sits correctly in the sliding window
            state.token_times.append((time.monotonic(), actual_tokens))
            state.day_tokens += actual_tokens
            state.day_requests += 1
            if state.day_tokens >= tpd or state.day_requests >= rpd:
                state.daily_exhausted = True
                logger.warning(
                    "ai.analyzer: daily limit reached after settle",
                    model=model,
                    day_tokens=state.day_tokens,
                    day_requests=state.day_requests,
                )


# ---------------------------------------------------------------------------
# Shared response parser
# ---------------------------------------------------------------------------
def _parse_to_extracted_event(raw_text: str) -> ExtractedEvent | None:
    """Parse a raw provider response string into an ``ExtractedEvent``.

    Handles accidental markdown fences that some models emit despite
    instructions.  Returns ``None`` only on catastrophic parse failures.
    """
    try:
        text = raw_text.strip()
        if text.startswith("```"):
            # Strip ```json ... ``` fences
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)

        data: dict[str, Any] = json.loads(text)
        event = ExtractedEvent.model_validate(data)
        logger.debug(
            "ai.analyzer: extraction complete",
            is_voucher=event.is_voucher,
            confidence=event.confidence,
            vendor=event.vendor,
        )
        return event
    except Exception as exc:
        logger.warning(
            "ai.analyzer: failed to parse response",
            error=str(exc),
            raw=raw_text[:300],
        )
        # Return a safe default rather than None so callers can always proceed.
        return ExtractedEvent(is_voucher=False, confidence=0.0, reason="parse_error")


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------
async def _call_groq_model(
    title: str, content: str | None, model: str, source_name: str | None = None
) -> ExtractedEvent | None:
    """Call a specific Groq model. Returns None on daily limit or non-retryable failure."""
    if not is_model_available(model):
        logger.info("ai.analyzer: model daily limit exhausted, skipping", model=model)
        return None

    client = AsyncGroq(api_key=settings.groq_api_key)
    content_for_prompt = content or "(no content)"

    source_hint = f"Source: {source_name}\n" if source_name else ""
    user_prompt = f"{source_hint}Title: {title}\n\nContent: {content_for_prompt}"
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    estimated_tokens = _estimate_tokens(_SYSTEM_PROMPT + user_prompt)

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            rid = await _wait_for_groq_budget(estimated_tokens, model)
        except RuntimeError:
            # Daily limit hit while waiting
            return None
        try:
            params = {
                "model": model,
                "messages": messages,
                "temperature": 1,
                "max_completion_tokens": settings.groq_max_completion_tokens,
                "top_p": 1,
            }
            if model.startswith("openai/gpt-oss-"):
                params["reasoning_effort"] = "medium"
            if "llama" in model.lower():
                params["response_format"] = {"type": "json_object"}

            resp = await client.chat.completions.create(**params)  # type: ignore[call-overload]
            actual = getattr(resp.usage, "total_tokens", None) or estimated_tokens
            await _settle_groq_budget(rid, actual, model)
            raw_text: str = resp.choices[0].message.content.strip()
            return _parse_to_extracted_event(raw_text)
        except Exception as exc:
            error_str = str(exc)
            if "429" in error_str:
                state = _get_model_state(model)
                async with state.lock:
                    state.reservations.pop(rid, None)
                # Check if it's a daily (RPD/TPD) 429 vs per-minute
                if "day" in error_str.lower() or "daily" in error_str.lower():
                    state.daily_exhausted = True
                    logger.warning(
                        "ai.analyzer: daily limit confirmed by API", model=model
                    )
                    return None
                if attempt < _MAX_RETRIES:
                    wait = _parse_retry_delay(error_str)
                    logger.warning(
                        "ai.analyzer: Groq rate-limited, retrying",
                        model=model,
                        attempt=attempt,
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
            else:
                logger.warning(
                    "ai.analyzer: Groq model failed, will try next provider",
                    model=model,
                    error=error_str[:120],
                )
                return None
    return None


async def _call_groq(
    title: str, content: str | None, source_name: str | None = None
) -> ExtractedEvent | None:
    """Try all batch models in order, skipping daily-exhausted ones."""
    for model in _GROQ_BATCH_MODELS:
        if not is_model_available(model):
            continue
        result = await _call_groq_model(title, content, model, source_name)
        if result is not None:
            return result
    return None


async def _call_gemini(
    title: str, content: str | None, source_name: str | None = None
) -> ExtractedEvent | None:
    """Gemini provider adapter.  Returns None on non-retryable failure."""
    client = genai.Client(api_key=settings.gemini_api_key)
    source_hint = f"Source: {source_name}\n" if source_name else ""
    full_prompt = (
        _SYSTEM_PROMPT
        + source_hint
        + f"Title: {title}\n\nContent: {content or '(no content)'}"
    )

    for attempt in range(1, _MAX_RETRIES + 1):
        try:

            def _call() -> str:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=full_prompt,
                )
                return resp.text or ""

            raw_text = (await asyncio.to_thread(_call)).strip()
            return _parse_to_extracted_event(raw_text)
        except Exception as exc:
            error_str = str(exc)
            if "429" in error_str and attempt < _MAX_RETRIES:
                wait = _parse_retry_delay(error_str)
                logger.warning(
                    "ai.analyzer: Gemini rate-limited, retrying",
                    attempt=attempt,
                    wait_seconds=wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "ai.analyzer: Gemini unexpected error",
                    error=error_str[:200],
                )
                return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def analyze_post(
    title: str, content: str | None, source_name: str | None = None
) -> ExtractedEvent | None:
    """Extract structured promotion data from a single post.

    Provider priority: Groq/llama (primary) → Groq/gpt-oss (fallback) → Gemini (final fallback).
    Fallback is triggered only on non-429 failures; 429s are retried within each provider.
    """
    if settings.groq_api_key:
        result = await _call_groq(title, content, source_name)
        if result is not None:
            return result

    if settings.gemini_api_key:
        result = await _call_gemini(title, content, source_name)
        if result is not None:
            return result

    if not settings.groq_api_key and not settings.gemini_api_key:
        logger.warning("ai.analyzer: no API keys configured — skipping AI extraction.")
    else:
        logger.error("ai.analyzer: all configured providers failed.")

    return None


async def analyze_post_batch(
    posts: list[tuple[str, str | None]],
    source_name: str | None = None,
) -> list[ExtractedEvent | None]:
    """Analyze multiple posts concurrently, distributing across available Groq models.

    Each model gets a semaphore sized to its TPM capacity so we don't fire more
    concurrent requests than the budget can absorb. Posts are round-robin assigned
    to available models; on per-model failure the next available model is tried,
    then Gemini as final fallback.
    Returns results in the same order as the input list.
    """
    if not settings.groq_api_key or not posts:
        return [await analyze_post(t, c, source_name) for t, c in posts]

    available = [m for m in _GROQ_BATCH_MODELS if is_model_available(m)]
    if not available:
        logger.warning(
            "ai.analyzer: all Groq models daily-exhausted, falling back to Gemini"
        )
        return [await _call_gemini(t, c, source_name) for t, c in posts]

    n = len(available)

    async def _call_one(
        idx: int, title: str, content: str | None
    ) -> tuple[int, ExtractedEvent | None]:
        async with _GLOBAL_AI_SEMAPHORE:
            for attempt_offset in range(n):
                model = available[(idx + attempt_offset) % n]
                if not is_model_available(model):
                    continue
                result = await _call_groq_model(title, content, model, source_name)
                if result is not None:
                    return idx, result
            if settings.gemini_api_key:
                return idx, await _call_gemini(title, content, source_name)
        return idx, None

    results: list[ExtractedEvent | None] = [None] * len(posts)
    for idx, result in await asyncio.gather(
        *[_call_one(i, t, c) for i, (t, c) in enumerate(posts)]
    ):
        results[idx] = result
    return results
