"""HTTP client for the BoI public exchange-rates API.

JSON numbers are parsed straight into ``Decimal`` (``parse_float=Decimal``), so a
rate never exists as a binary float in this process.
"""

import json
import logging
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import WaitBaseT

from rates.domain.errors import SnapshotRejected

log = logging.getLogger(__name__)


class BoiFetchError(Exception):
    """BoI could not be reached or returned an unusable HTTP response."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):  # timeouts, DNS, TLS, connection errors
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class BoiClient:
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        http: httpx.AsyncClient | None = None,
        wait: WaitBaseT | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._http = http
        self._wait = wait or wait_exponential_jitter(initial=1, max=10, jitter=1)

    async def fetch(self) -> dict[str, Any]:
        """Fetch and JSON-decode the current snapshot (numbers as Decimal).

        Raises ``BoiFetchError`` after bounded retries, or ``SnapshotRejected`` if the
        body is not JSON (not retried: a malformed body is not a transient failure).
        """
        response: httpx.Response | None = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=self._wait,
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    attempt_no = attempt.retry_state.attempt_number
                    log.debug("boi.fetch attempt=%d url=%s", attempt_no, self._url)
                    response = await self._get()
                    response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise BoiFetchError(f"BoI request failed: {exc!r}") from exc

        assert response is not None
        try:
            payload = response.json(parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SnapshotRejected(f"BoI response is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SnapshotRejected("BoI response is not a JSON object")
        return payload

    async def _get(self) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if self._http is not None:
            return await self._http.get(self._url, timeout=self._timeout, headers=headers)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(self._url, headers=headers)
