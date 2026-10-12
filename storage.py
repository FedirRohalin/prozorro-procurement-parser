"""Deduplication of winners and atomic JSON persistence."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from models import CompanyRecord, Winner


class CompanyAggregator:
    """Collapses winners into one ``CompanyRecord`` per EDRPOU."""

    def __init__(self) -> None:
        self._companies: dict[str, CompanyRecord] = {}
        self._seen_awards: set[tuple[str, str, str]] = set()

    def add(self, winner: Winner) -> bool:
        """Register a winner. Returns ``False`` for an already seen award (duplicate)."""
        key = (winner.edrpou, winner.tender.tender_id, winner.tender.award_id)
        if key in self._seen_awards:
            return False
        self._seen_awards.add(key)

        record = self._companies.get(winner.edrpou)
        if record is None:
            record = CompanyRecord(
                edrpou=winner.edrpou,
                identifier_scheme=winner.identifier_scheme,
                name=winner.name,
            )
            self._companies[winner.edrpou] = record
        elif not record.name and winner.name:
            record.name = winner.name

        record.won_tenders.append(winner.tender)
        return True

    def records(self) -> list[CompanyRecord]:
        """Unique companies, most active first."""
        return sorted(
            self._companies.values(), key=lambda r: (-len(r.won_tenders), r.edrpou)
        )


class JsonStorage:
    """Writes the result atomically (temp file + replace) as UTF-8 JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, records: Iterable[CompanyRecord]) -> int:
        """Persist the records and return how many companies were written.

        Raises:
            OSError: the file could not be written.
        """
        companies = [record.to_dict() for record in records]
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "companies_count": len(companies),
            "companies": companies,
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self._path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
        return len(companies)
