"""Application settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv

N = TypeVar("N", int, float)

DEFAULT_CPV_CODES: tuple[str, ...] = ("35610000-4",)
# Stems rather than whole words: "дрон" also matches "дрони", "дронів", etc.
DEFAULT_KEYWORD_STEMS: tuple[str, ...] = (
    "бпак",
    "бпла",
    "безпілот",
    "дрон",
    "квадрокоптер",
    "ударн",
)


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


def parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 string into an aware datetime (naive values are treated as UTC).

    Returns ``None`` when the string cannot be parsed.
    """
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _get_str(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _get_optional_str(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def _get_number(name: str, default: N, cast: Callable[[str], N], minimum: N) -> N:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime configuration."""

    prozorro_api_url: str
    prozorro_offset_from: str
    prozorro_date_to: datetime | None
    prozorro_page_limit: int
    prozorro_max_pages: int | None
    prozorro_workers: int
    prozorro_request_delay: float
    http_timeout: float
    http_max_retries: int
    opendatabot_api_key: str | None
    opendatabot_api_url: str
    enrichment_delay: float
    output_path: Path
    cpv_codes: tuple[str, ...] = DEFAULT_CPV_CODES
    keyword_stems: tuple[str, ...] = DEFAULT_KEYWORD_STEMS

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment; raise ``ConfigError`` on bad values."""
        load_dotenv()

        default_offset = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(
            timespec="seconds"
        )

        date_to_raw = _get_optional_str("PROZORRO_DATE_TO")
        date_to: datetime | None = None
        if date_to_raw:
            date_to = parse_iso_datetime(date_to_raw)
            if date_to is None:
                raise ConfigError(
                    "PROZORRO_DATE_TO must be an ISO-8601 date or datetime"
                )

        max_pages_raw = _get_optional_str("PROZORRO_MAX_PAGES")
        max_pages = (
            _get_number("PROZORRO_MAX_PAGES", 1, int, 1) if max_pages_raw else None
        )

        return cls(
            prozorro_api_url=_get_str(
                "PROZORRO_API_URL", "https://public.api.openprocurement.org/api/2.5"
            ),
            prozorro_offset_from=_get_str("PROZORRO_OFFSET_FROM", default_offset),
            prozorro_date_to=date_to,
            prozorro_page_limit=_get_number("PROZORRO_PAGE_LIMIT", 100, int, 1),
            prozorro_max_pages=max_pages,
            prozorro_workers=_get_number("PROZORRO_WORKERS", 4, int, 1),
            prozorro_request_delay=_get_number(
                "PROZORRO_REQUEST_DELAY", 0.15, float, 0.0
            ),
            http_timeout=_get_number("HTTP_TIMEOUT", 30.0, float, 1.0),
            http_max_retries=_get_number("HTTP_MAX_RETRIES", 5, int, 0),
            opendatabot_api_key=_get_optional_str("OPENDATABOT_API_KEY"),
            opendatabot_api_url=_get_str(
                "OPENDATABOT_API_URL", "https://opendatabot.com/api/v3"
            ),
            enrichment_delay=_get_number("ENRICHMENT_DELAY", 0.5, float, 0.0),
            output_path=Path(_get_str("OUTPUT_PATH", "b2b_drone_manufacturers.json")),
        )
