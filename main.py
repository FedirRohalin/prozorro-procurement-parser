"""Entry point: Prozorro -> deduplication -> enrichment -> b2b_drone_manufacturers.json."""

from __future__ import annotations

import logging
import sys

from config import ConfigError, Settings
from enrichment import EnrichmentService, build_enricher
from http_client import ApiError
from models import EnrichmentStatus
from prozorro_client import ProzorroClient
from storage import CompanyAggregator, JsonStorage

logger = logging.getLogger("drone_export")

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG_ERROR = 2
EXIT_STORAGE_ERROR = 3


def configure_logging() -> None:
    """Send logs to stderr in a compact format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run() -> int:
    """Run the whole pipeline and return a process exit code."""
    configure_logging()

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        return EXIT_CONFIG_ERROR

    prozorro = ProzorroClient(settings)
    enrichment = EnrichmentService(build_enricher(settings))
    aggregator = CompanyAggregator()
    scan_completed = True

    logger.info("Scanning Prozorro from offset %s", settings.prozorro_offset_from)
    try:
        for winner in prozorro.iter_winners():
            aggregator.add(winner)
    except KeyboardInterrupt:
        scan_completed = False
        logger.warning("Interrupted by user; continuing with partial results")
    except ApiError as exc:
        scan_completed = False
        logger.error("Prozorro scan aborted: %s", exc)

    if not scan_completed:
        logger.info(
            "To resume the scan set PROZORRO_OFFSET_FROM=%s", prozorro.last_offset
        )

    records = aggregator.records()
    logger.info("Unique winning companies: %d", len(records))

    try:
        enrichment.enrich_records(records)
    except KeyboardInterrupt:
        logger.warning("Enrichment interrupted; remaining companies stay 'Unenriched'")

    try:
        saved = JsonStorage(settings.output_path).save(records)
    except OSError as exc:
        logger.error("Could not write %s: %s", settings.output_path, exc)
        return EXIT_STORAGE_ERROR

    enriched = sum(
        1 for r in records if r.enrichment_status is EnrichmentStatus.ENRICHED
    )
    logger.info(
        "Saved %d companies (%d enriched, %d unenriched) to %s",
        saved,
        enriched,
        saved - enriched,
        settings.output_path,
    )
    return EXIT_OK if scan_completed else EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(run())
