"""Prozorro public API client: feed pagination, tender filtering, winner extraction."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from config import Settings, parse_iso_datetime
from http_client import (
    ApiError,
    AuthOrQuotaError,
    NotFoundError,
    RateLimitExceeded,
    RetryingClient,
)
from models import Winner, WonTender

logger = logging.getLogger(__name__)

TENDER_PUBLIC_URL = "https://prozorro.gov.ua/tender/{tender_id}"


@dataclass(frozen=True, slots=True)
class TenderMatch:
    """Why a tender matched and which lots count (``None`` = the whole tender)."""

    reason: str
    lot_ids: frozenset[str] | None


class TenderMatcher:
    """Decides whether a tender is drone-related (by CPV code or keywords)."""

    def __init__(
        self, cpv_codes: tuple[str, ...], keyword_stems: tuple[str, ...]
    ) -> None:
        self._cpv_codes = frozenset(self._normalize_cpv(code) for code in cpv_codes)
        self._keywords = (
            re.compile(
                r"\b(?:" + "|".join(re.escape(stem) for stem in keyword_stems) + ")",
                re.IGNORECASE,
            )
            if keyword_stems
            else None
        )

    @staticmethod
    def _normalize_cpv(code: str) -> str:
        """'35610000-4' -> '35610000' (the check digit is irrelevant for matching)."""
        return code.split("-", maxsplit=1)[0].strip()

    def match(self, tender: dict[str, Any]) -> TenderMatch | None:
        """Return a ``TenderMatch`` or ``None`` when the tender is irrelevant."""
        cpv_hit = False
        cpv_untied = False  # a matching item that does not belong to any lot
        cpv_lots: set[str] = set()

        for item in tender.get("items") or []:
            classification = item.get("classification") or {}
            if (
                self._normalize_cpv(str(classification.get("id", "")))
                not in self._cpv_codes
            ):
                continue
            cpv_hit = True
            related_lot = item.get("relatedLot")
            if related_lot:
                cpv_lots.add(str(related_lot))
            else:
                cpv_untied = True

        keyword_hit = self._has_keyword(tender)
        if not (cpv_hit or keyword_hit):
            return None

        if cpv_hit and keyword_hit:
            reason = "cpv+keyword"
        else:
            reason = "cpv" if cpv_hit else "keyword"

        # Restrict to the matching lots only when the CPV alone triggered the match.
        restrict_to_lots = cpv_hit and not keyword_hit and not cpv_untied
        return TenderMatch(reason, frozenset(cpv_lots) if restrict_to_lots else None)

    def _has_keyword(self, tender: dict[str, Any]) -> bool:
        if self._keywords is None:
            return False
        parts: list[Any] = [tender.get("title"), tender.get("description")]
        parts += [item.get("description") for item in tender.get("items") or []]
        parts += [lot.get("title") for lot in tender.get("lots") or []]
        text = " ".join(part for part in parts if isinstance(part, str))
        return self._keywords.search(text) is not None


class ProzorroClient:
    """Walks the Prozorro tenders feed and yields winners of drone-related tenders."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tenders_url = f"{settings.prozorro_api_url.rstrip('/')}/tenders"
        self._http = RetryingClient(
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            min_interval=settings.prozorro_request_delay,
        )
        self._matcher = TenderMatcher(settings.cpv_codes, settings.keyword_stems)
        # Offset of the first page that has not been fully processed (resume point).
        self.last_offset: str = settings.prozorro_offset_from

    def iter_winners(self) -> Iterator[Winner]:
        """Yield every winner of every matching completed tender in the configured window.

        Raises:
            ApiError: the feed request failed after retries (the caller may keep
                the winners collected so far and resume from ``last_offset``).
        """
        offset: str | None = self._settings.prozorro_offset_from
        pages = 0

        with ThreadPoolExecutor(max_workers=self._settings.prozorro_workers) as pool:
            while True:
                payload = self._http.get_json(
                    self._tenders_url,
                    params={
                        "offset": offset,
                        "limit": self._settings.prozorro_page_limit,
                        "opt_fields": "status",
                    },
                )
                items = payload.get("data") or [] if isinstance(payload, dict) else []
                if not items:
                    logger.info("Feed exhausted")
                    break

                candidate_ids, window_ended = self._select_candidates(items)
                matched = 0
                for tender in pool.map(self._fetch_tender, candidate_ids):
                    if tender is None:
                        continue
                    for winner in self._extract_winners(tender):
                        matched += 1
                        yield winner

                pages += 1
                next_offset = (payload.get("next_page") or {}).get("offset")
                logger.info(
                    "Page %d: %d feed items, %d completed candidates, %d matching awards",
                    pages,
                    len(items),
                    len(candidate_ids),
                    matched,
                )

                if window_ended:
                    logger.info("Reached PROZORRO_DATE_TO")
                    break
                if not next_offset or next_offset == offset:
                    break
                offset = str(next_offset)
                self.last_offset = offset

                max_pages = self._settings.prozorro_max_pages
                if max_pages is not None and pages >= max_pages:
                    logger.info("Reached PROZORRO_MAX_PAGES=%d", max_pages)
                    break

    def _select_candidates(self, items: list[dict[str, Any]]) -> tuple[list[str], bool]:
        """Pick tender ids worth fetching; also report whether the date window ended."""
        date_to = self._settings.prozorro_date_to
        candidate_ids: list[str] = []
        for item in items:
            raw_date = item.get("dateModified")
            if date_to is not None and isinstance(raw_date, str):
                modified = parse_iso_datetime(raw_date)
                if modified is not None and modified > date_to:
                    return candidate_ids, True
            status = item.get("status")
            if status is not None and status != "complete":
                continue
            if item.get("id"):
                candidate_ids.append(str(item["id"]))
        return candidate_ids, False

    def _fetch_tender(self, tender_id: str) -> dict[str, Any] | None:
        """Fetch full tender details; ``None`` if missing, broken or not completed."""
        try:
            payload = self._http.get_json(f"{self._tenders_url}/{tender_id}")
        except NotFoundError:
            return None
        except (RateLimitExceeded, AuthOrQuotaError):
            raise  # continuing would be pointless: abort the scan
        except ApiError as exc:
            logger.warning("Skipping tender %s: %s", tender_id, exc)
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("status") != "complete":
            return None
        return data

    def _extract_winners(self, tender: dict[str, Any]) -> Iterator[Winner]:
        match = self._matcher.match(tender)
        if match is None:
            return

        tender_id = str(tender.get("id", ""))
        prozorro_id = str(tender.get("tenderID") or tender_id)

        for award in tender.get("awards") or []:
            if award.get("status") != "active":
                continue
            if match.lot_ids is not None and award.get("lotID") not in match.lot_ids:
                continue

            amount, currency = self._resolve_amount(tender, award)
            for supplier in award.get("suppliers") or []:
                identifier = supplier.get("identifier") or {}
                edrpou = str(identifier.get("id") or "").strip()
                if not edrpou:
                    continue
                yield Winner(
                    edrpou=edrpou,
                    identifier_scheme=str(identifier.get("scheme") or ""),
                    name=str(
                        identifier.get("legalName") or supplier.get("name") or ""
                    ).strip(),
                    tender=WonTender(
                        tender_id=tender_id,
                        prozorro_id=prozorro_id,
                        award_id=str(award.get("id", "")),
                        title=str(tender.get("title") or "").strip(),
                        url=TENDER_PUBLIC_URL.format(tender_id=prozorro_id),
                        matched_by=match.reason,
                        amount=amount,
                        currency=currency,
                        date_modified=tender.get("dateModified"),
                    ),
                )

    @staticmethod
    def _resolve_amount(
        tender: dict[str, Any], award: dict[str, Any]
    ) -> tuple[float, str]:
        """Award value first; fall back to the lot value, then the tender value."""
        candidates: list[Any] = [award.get("value")]
        lot_id = award.get("lotID")
        candidates += [
            lot.get("value")
            for lot in tender.get("lots") or []
            if lot.get("id") == lot_id
        ]
        candidates.append(tender.get("value"))

        for value in candidates:
            if not isinstance(value, dict):
                continue
            try:
                amount = float(value["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            return amount, str(value.get("currency") or "UAH")
        return 0.0, "UAH"
