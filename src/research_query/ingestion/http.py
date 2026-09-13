"""Small respectful HTTP boundary with bounded retry behavior."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]
    url: str

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> object:
        return json.loads(self.body)


class HttpTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse: ...


class CachingHttpTransport:
    """Run-scoped in-memory cache that avoids repeat provider requests."""

    def __init__(self, wrapped: HttpTransport) -> None:
        self.wrapped = wrapped
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], HttpResponse] = {}

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
        key = (url, tuple(sorted((headers or {}).items())))
        if key not in self._cache:
            self._cache[key] = self.wrapped.get(url, headers=headers)
        return self._cache[key]


class RetryingHttpTransport:
    """urllib transport used by live adapters; tests inject a fixture transport."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        max_retries: int,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if "replace-with-project-email" in user_agent:
            raise ValueError("configure a descriptive HTTP_USER_AGENT contact before live requests")
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter

    @staticmethod
    def _headers(message: Message | None) -> dict[str, str]:
        return {} if message is None else {key.lower(): value for key, value in message.items()}

    @staticmethod
    def _retry_after(value: str | None, fallback: float) -> float:
        if not value:
            return fallback
        if value.isdigit():
            return float(value)
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
        request_headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/json"}
        request_headers.update(headers or {})
        for attempt in range(self.max_retries + 1):
            try:
                request = Request(url, headers=request_headers)
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return HttpResponse(
                        status=response.status,
                        body=response.read(),
                        headers=self._headers(response.headers),
                        url=response.url,
                    )
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt >= self.max_retries:
                    raise
                delay = self._retry_after(
                    exc.headers.get("Retry-After"),
                    float(2**attempt),
                )
            except URLError:
                if attempt >= self.max_retries:
                    raise
                delay = 2**attempt
            self._sleep(delay + self._jitter())
        raise RuntimeError("unreachable retry loop")
