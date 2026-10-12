"""Domain models shared across modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EnrichmentStatus(str, Enum):
    """Whether a company was successfully enriched from an external registry API."""

    ENRICHED = "Enriched"
    UNENRICHED = "Unenriched"


@dataclass(frozen=True, slots=True)
class WonTender:
    """One won award (a tender, or a single lot of a multi-lot tender)."""

    tender_id: str
    prozorro_id: str
    award_id: str
    title: str
    url: str
    matched_by: str  # "cpv" | "keyword" | "cpv+keyword"
    amount: float
    currency: str
    date_modified: str | None


@dataclass(frozen=True, slots=True)
class Winner:
    """A supplier extracted from an active award, with the tender it won."""

    edrpou: str
    identifier_scheme: str
    name: str
    tender: WonTender


@dataclass(slots=True)
class EnrichmentResult:
    """Data returned by a company-registry provider."""

    company_name: str | None = None
    director: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    address: str | None = None
    company_status: str | None = (
        None  # active | bankrupt | liquidating | terminated | unknown
    )
    company_status_raw: str | None = None


@dataclass(slots=True)
class CompanyRecord:
    """A unique winning company with all of its won tenders."""

    edrpou: str
    identifier_scheme: str
    name: str
    won_tenders: list[WonTender] = field(default_factory=list)
    enrichment_status: EnrichmentStatus = EnrichmentStatus.UNENRICHED
    enrichment_note: str | None = None
    enrichment: EnrichmentResult | None = None

    @property
    def total_awarded_by_currency(self) -> dict[str, float]:
        """Sum of won amounts per currency."""
        totals: dict[str, float] = {}
        for tender in self.won_tenders:
            totals[tender.currency] = totals.get(tender.currency, 0.0) + tender.amount
        return {currency: round(total, 2) for currency, total in sorted(totals.items())}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a flat, JSON-friendly structure."""
        data = self.enrichment or EnrichmentResult()
        tenders = sorted(self.won_tenders, key=lambda t: t.date_modified or "")
        return {
            "edrpou": self.edrpou,
            "identifier_scheme": self.identifier_scheme,
            "name": data.company_name or self.name,
            "prozorro_name": self.name,
            "enrichment_status": self.enrichment_status.value,
            "enrichment_note": self.enrichment_note,
            "director": data.director,
            "phones": data.phones,
            "emails": data.emails,
            "address": data.address,
            "company_status": data.company_status,
            "company_status_raw": data.company_status_raw,
            "tenders_count": len(self.won_tenders),
            "total_awarded_by_currency": self.total_awarded_by_currency,
            "tenders": [asdict(tender) for tender in tenders],
        }
