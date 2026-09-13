from __future__ import annotations

from email.message import Message
from urllib.error import HTTPError

import pytest

from research_query.ingestion.http import CachingHttpTransport, RetryingHttpTransport


def test_live_transport_rejects_placeholder_contact() -> None:
    with pytest.raises(ValueError):
        RetryingHttpTransport(
            user_agent="ResearchQuery (replace-with-project-email)",
            timeout_seconds=1,
            max_retries=0,
        )


def test_retry_after_and_bounded_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    class Response:
        status = 200
        headers = Message()
        url = "https://example.edu"

        def read(self) -> bytes:
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(*args: object, **kwargs: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            headers = Message()
            headers["Retry-After"] = "2"
            raise HTTPError("https://example.edu", 429, "rate", headers, None)
        return Response()

    monkeypatch.setattr("research_query.ingestion.http.urlopen", fake_urlopen)
    transport = RetryingHttpTransport(
        user_agent="ResearchQuery/0.1 contact@example.edu",
        timeout_seconds=1,
        max_retries=1,
        sleep=delays.append,
        jitter=lambda: 0.0,
    )
    assert transport.get("https://example.edu").body == b"ok"
    assert attempts == 2
    assert delays == [2.0]


def test_run_scoped_cache_reuses_identical_get(fixture_transport: type) -> None:
    wrapped = fixture_transport({"https://example.edu": "body"})
    cached = CachingHttpTransport(wrapped)
    assert cached.get("https://example.edu").body == b"body"
    assert cached.get("https://example.edu").body == b"body"
    assert len(wrapped.requests) == 1
