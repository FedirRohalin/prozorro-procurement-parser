"""Shared HTTP layer: client-side throttling, retries with backoff, typed errors."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 120.0


class ApiError(Exception):
    """Base class for every failed API interaction."""


class NotFoundError(ApiError):
    """The resource does not exist (HTTP 404)."""


class AuthOrQuotaError(ApiError):
    """Invalid key, exhausted credits or no access (HTTP 401/402/403)."""


class RateLimitExceeded(ApiError):
    """HTTP 429 persisted after all retries."""


class RetryingClient:
    """``requests.Session`` wrapper that is safe to share between threads.

    Guarantees a minimum interval between consecutive requests and retries
    transient failures (429, 5xx, timeouts, connection errors) with exponential
    backoff, honouring the ``Retry-After`` header.

    Error messages never include the request URL because it may carry an API key.
    """

    def __init__(
        self,
        *,
        timeout: float,
        max_retries: int,
        min_interval: float,
        user_agent: str = "drone-suppliers-export/1.0",
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``url`` and return the decoded JSON body.

        Raises:
            NotFoundError: HTTP 404.
            AuthOrQuotaError: HTTP 401/402/403.
            RateLimitExceeded: HTTP 429 persisted after all retries.
            ApiError: any other failure.
        """
        failure = "unknown failure"
        rate_limited = False

        for attempt in range(self._max_retries + 1):
            self._throttle()
            retry_after: float | None = None
            rate_limited = False
            try:
                response = self._session.get(url, params=params, timeout=self._timeout)
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as exc:
                failure = f"network error ({type(exc).__name__})"
            except requests.exceptions.RequestException as exc:
                raise ApiError(f"Request failed ({type(exc).__name__})") from None
            else:
                status = response.status_code
                if status == 200:
                    return self._decode(response)
                if status == 404:
                    raise NotFoundError("HTTP 404")
                if status in (401, 402, 403):
                    raise AuthOrQuotaError(f"HTTP {status}")
                if status != 429 and status not in RETRYABLE_STATUS_CODES:
                    raise ApiError(f"Unexpected HTTP {status}")
                rate_limited = status == 429
                failure = f"HTTP {status}"
                retry_after = self._parse_retry_after(response)

            if attempt < self._max_retries:
                self._backoff(attempt, retry_after, failure, rate_limited)

        message = f"{failure} after {self._max_retries} retries"
        if rate_limited:
            raise RateLimitExceeded(message)
        raise ApiError(message)

    def _throttle(self) -> None:
        """Block until the minimum interval since the previous request has passed."""
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _backoff(
        self, attempt: int, retry_after: float | None, failure: str, rate_limited: bool
    ) -> None:
        base = 2.0 if rate_limited else 1.0
        delay = (
            retry_after
            if retry_after is not None
            else min(base * 2**attempt, MAX_BACKOFF_SECONDS)
        )
        logger.warning(
            "%s; retry %d/%d in %.1fs", failure, attempt + 1, self._max_retries, delay
        )
        time.sleep(delay)

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return min(max(float(raw), 0.0), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            return (
                None  # HTTP-date form is ignored; exponential backoff is used instead
            )

    @staticmethod
    def _decode(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise ApiError("Response body is not valid JSON") from None
