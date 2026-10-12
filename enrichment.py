"""Company enrichment: pluggable providers behind a fail-safe service."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from config import Settings
from http_client import (
    ApiError,
    AuthOrQuotaError,
    NotFoundError,
    RateLimitExceeded,
    RetryingClient,
)
from models import CompanyRecord, EnrichmentResult, EnrichmentStatus

logger = logging.getLogger(__name__)

EDRPOU_PATTERN = re.compile(r"\d{8}")
MAX_CONSECUTIVE_FAILURES = 5


class EnrichmentUnavailable(Exception):
    """The provider cannot be used for the rest of the run (bad key / exhausted quota)."""


class CompanyEnricher(ABC):
    """Interface for company-registry providers (Opendatabot, YouControl, ...)."""

    @abstractmethod
    def fetch(self, edrpou: str) -> EnrichmentResult | None:
        """Return company data, or ``None`` if the company is not found.

        Raises:
            EnrichmentUnavailable: key rejected or quota exhausted.
            ApiError: transient failure for this particular company.
        """


def _clean(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _string_list(value: Any) -> list[str]:
    """Normalize a str / list[str] into a de-duplicated list of non-empty strings."""
    raw = (
        [value] if isinstance(value, str) else value if isinstance(value, list) else []
    )
    result: list[str] = []
    for item in raw:
        cleaned = _clean(item)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def normalize_company_status(raw: str | None) -> str | None:
    """Map a Ukrainian registry status to active/bankrupt/liquidating/terminated/unknown."""
    if not raw:
        return None
    lowered = raw.lower()
    if "банкрут" in lowered:
        return "bankrupt"
    if "в стані припинення" in lowered:
        return "liquidating"
    if "припинено" in lowered:
        return "terminated"
    if "зареєстровано" in lowered:
        return "active"
    return "unknown"


class OpendatabotEnricher(CompanyEnricher):
    """Opendatabot API v3 (``full-company`` endpoint).

    All knowledge about the response shape lives in ``_parse`` so it is easy to
    adjust. A YouControl provider is added the same way: subclass ``CompanyEnricher``.
    """

    def __init__(self, api_key: str, base_url: str, http: RetryingClient) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http

    def fetch(self, edrpou: str) -> EnrichmentResult | None:
        url = f"{self._base_url}/full-company/{edrpou}"
        try:
            payload = self._http.get_json(url, params={"apiKey": self._api_key})
        except NotFoundError:
            return None
        except AuthOrQuotaError as exc:
            raise EnrichmentUnavailable(
                "Opendatabot rejected the key or the quota is exhausted"
            ) from exc
        except RateLimitExceeded as exc:
            raise EnrichmentUnavailable(
                "Opendatabot rate limit persisted after retries"
            ) from exc
        return self._parse(payload)

    @staticmethod
    def _parse(payload: Any) -> EnrichmentResult:
        if not isinstance(payload, dict):
            raise ApiError("Unexpected Opendatabot response shape")

        raw_status = _clean(payload.get("status"))
        return EnrichmentResult(
            company_name=_clean(payload.get("full_name"))
            or _clean(payload.get("short_name")),
            director=OpendatabotEnricher._extract_director(payload),
            phones=_string_list(payload.get("phones")),
            emails=_string_list(payload.get("emails")),
            address=_clean(payload.get("address")),
            company_status=normalize_company_status(raw_status),
            company_status_raw=raw_status,
        )

    @staticmethod
    def _extract_director(payload: dict[str, Any]) -> str | None:
        ceo = payload.get("ceo")
        if isinstance(ceo, dict) and (name := _clean(ceo.get("name"))):
            return name
        heads = payload.get("heads")
        if isinstance(heads, list):
            for head in heads:
                if isinstance(head, dict) and (name := _clean(head.get("name"))):
                    return name
        return None


class EnrichmentService:
    """Enriches records in place and never raises because of provider problems.

    On a missing key, rejected key, exhausted quota or repeated failures the
    remaining records are marked "Unenriched" and keep their base Prozorro data.
    """

    def __init__(self, enricher: CompanyEnricher | None) -> None:
        self._enricher = enricher
        self._disabled_reason: str | None = (
            None if enricher else "API key is not configured"
        )
        self._consecutive_failures = 0

    def enrich_records(self, records: list[CompanyRecord]) -> None:
        """Enrich every record (one provider call per unique EDRPOU)."""
        if self._enricher is not None:
            logger.info("Enriching %d companies", len(records))
        for index, record in enumerate(records, start=1):
            self._enrich_one(record)
            if self._enricher is not None and index % 25 == 0:
                logger.info("Enriched %d/%d", index, len(records))

    def _enrich_one(self, record: CompanyRecord) -> None:
        if self._enricher is None or self._disabled_reason is not None:
            self._mark_unenriched(
                record, self._disabled_reason or "Enrichment disabled"
            )
            return

        if record.identifier_scheme not in (
            "",
            "UA-EDR",
        ) or not EDRPOU_PATTERN.fullmatch(record.edrpou):
            self._mark_unenriched(record, "Not a Ukrainian 8-digit EDRPOU")
            return

        try:
            result = self._enricher.fetch(record.edrpou)
        except EnrichmentUnavailable as exc:
            self._disabled_reason = str(exc)
            logger.warning("Enrichment disabled for the rest of the run: %s", exc)
            self._mark_unenriched(record, self._disabled_reason)
            return
        except ApiError as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._disabled_reason = "Too many consecutive provider failures"
                logger.warning(
                    "Enrichment disabled for the rest of the run: %s",
                    self._disabled_reason,
                )
            self._mark_unenriched(record, f"Provider error: {exc}")
            return

        self._consecutive_failures = 0
        if result is None:
            self._mark_unenriched(record, "Company not found in the registry")
            return

        record.enrichment = result
        record.enrichment_status = EnrichmentStatus.ENRICHED
        record.enrichment_note = None

    @staticmethod
    def _mark_unenriched(record: CompanyRecord, note: str) -> None:
        record.enrichment_status = EnrichmentStatus.UNENRICHED
        record.enrichment_note = note


def build_enricher(settings: Settings) -> CompanyEnricher | None:
    """Create the configured provider, or ``None`` when no API key is set."""
    if not settings.opendatabot_api_key:
        logger.warning(
            "OPENDATABOT_API_KEY is not set: all companies will be 'Unenriched'"
        )
        return None
    http = RetryingClient(
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        min_interval=settings.enrichment_delay,
    )
    return OpendatabotEnricher(
        settings.opendatabot_api_key, settings.opendatabot_api_url, http
    )
