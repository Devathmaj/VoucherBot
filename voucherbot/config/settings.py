from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class EventMatcherConfig(BaseModel):
    """Scoring weights and thresholds for canonical event matching.

    Scores are additive. A post's AI-extracted fields are compared against each
    existing candidate Event and a confidence score is computed:

      - >= auto_merge_threshold  → attach to existing Event.
      - >= possible_match_threshold (and < auto_merge_threshold)
                                 → mark as POSSIBLE_MATCH for future review.
      - < possible_match_threshold → create a new Event.
    """

    # --- Scoring weights ---
    weight_registration_url: int = 50
    weight_voucher_code: int = 40
    weight_promotion_name: int = 25
    weight_vendor: int = 20
    weight_discount: int = 20
    weight_promotion_type: int = 10
    weight_certifications: int = 15
    weight_date_overlap: int = 10

    # --- Thresholds ---
    auto_merge_threshold: int = 70
    possible_match_threshold: int = 45

    # --- Candidate retrieval ---
    candidate_limit: int = 100

    # --- Promotion-name similarity cutoff (0–1) for partial weight credit ---
    # Below this similarity the name score contributes 0, at or above it
    # contributes the full weight_promotion_name value.
    #
    # Set to 0.60 — conservative enough to avoid false matches between
    # generic names like "Student Discount" vs "Certification Discount"
    # (~0.58 similarity), while still matching the intended examples:
    #   "Microsoft Virtual Training Days"   vs "Virtual Training Days"       (~0.81)
    #   "Microsoft Fabric Data Days"        vs "Fabric Data Days"            (~0.76)
    name_similarity_threshold: float = 0.60


# Ordered from most to least authoritative. Lower index = higher priority.
# Used by the EventMatcher when merging fields from a new post into an existing
# Event (a higher-priority source's non-null value wins over a lower-priority
# source's non-null value).
SOURCE_PRIORITY: list[str] = [
    "WEBSITE",  # official vendor / event pages
    "PEARSONVUE",  # official Pearson VUE vendor pages
    "TRAINING_PROVIDER",  # official training partner pages
    "EVENT",
    "BLOG",
    "RSS",
    "FORUM",
    "REDDIT",
    "API",
]


class Settings(BaseSettings):
    # False → apply alembic migrations + seed on startup; True → skip all DB
    # setup (schema applied ahead of time in production)
    is_prod: bool = False
    # True → seed a localhost test source for development/troubleshooting
    is_test: bool = False
    log_level: str = "INFO"
    database_url: str

    # API rate limiting
    # Health endpoint: max requests per IP per minute. 0 disables the limit.
    health_rate_limit_per_minute: int = 60
    # Comma-separated proxies whose X-Forwarded-For values we trust.
    rate_limit_trusted_proxies: list[str] = []

    # Email
    resend_api_key: Optional[str] = None
    email_from: str = "VoucherBot <onboarding@resend.dev>"
    email_id: Optional[str] = None
    email_min_interval_seconds: float = 5.0
    # Optional per-email Reply-To; when unset Resend falls back to the From.
    email_reply_to: Optional[str] = None

    # Reddit
    reddit_client_id: Optional[str] = None
    reddit_client_secret: Optional[str] = None
    reddit_user_agent: Optional[str] = None
    reddit_fetch_interval_minutes: int = 3
    reddit_concurrency_limit: int = 5
    reddit_fetch_limit: int = 25
    reddit_ingestion_enabled: bool = False

    # Scraping policy (see deep-research-report)
    # Identifying UA; empty → built from email contact. Do not spoof browsers.
    scraper_user_agent: Optional[str] = None
    scraper_contact_email: Optional[str] = None
    scraper_respect_robots: bool = True
    # Default politeness when robots.txt has no Crawl-delay (~0.5 req/s).
    scraper_min_delay_seconds: float = 2.0

    # DB-driven scheduler
    tick_lease_ttl_seconds: int = 21600
    tick_job_timeout_seconds: Optional[int] = None
    source_backoff_base_minutes: int = 5
    source_backoff_max_minutes: int = 360

    # AI providers
    gemini_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    groq_requests_per_minute: int = 30
    groq_tokens_per_minute: Optional[int] = None
    groq_max_completion_tokens: int = 1024
    groq_max_input_chars: int = 12000

    # Event matching (nested, not sourced from env — override in tests by
    # constructing Settings(event_matcher=EventMatcherConfig(...)))
    event_matcher: EventMatcherConfig = EventMatcherConfig()

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator(
        "tick_job_timeout_seconds", "groq_tokens_per_minute", mode="before"
    )
    @classmethod
    def empty_string_as_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("rate_limit_trusted_proxies", mode="before")
    @classmethod
    def split_trusted_proxies(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


settings = Settings()  # type: ignore[call-arg]
