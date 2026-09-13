from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from research_query.ingestion.http import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureTransport:
    def __init__(self, responses: Mapping[str, bytes | str | object | Exception]) -> None:
        self.responses = dict(responses)
        self.requests: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
        self.requests.append((url, headers or {}))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            body = value
        elif isinstance(value, str):
            body = value.encode()
        else:
            body = json.dumps(value).encode()
        return HttpResponse(200, body, {}, url)


@pytest.fixture
def fixture_transport() -> type[FixtureTransport]:
    return FixtureTransport
