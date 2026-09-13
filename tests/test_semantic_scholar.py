from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from uuid import UUID

from research_query.data.models import FacultyForResolution
from research_query.ingestion.publications.base import PublicationSource
from research_query.ingestion.publications.semantic_scholar import SemanticScholarPublicationSource

FIXTURES = Path(__file__).parent / "fixtures" / "semantic_scholar"


def faculty() -> FacultyForResolution:
    return FacultyForResolution(
        UUID(int=1),
        "José Álvarez",
        ("Associate Professor",),
        ("Johns Hopkins Materials Science",),
        "machine learning materials",
        ("Generative Models for Crystal Discovery",),
        {},
    )


def test_semantic_scholar_maps_provider_payload_and_uses_api_key(fixture_transport: type) -> None:
    now = datetime(2026, 9, 13, tzinfo=UTC)
    author_payload = json.loads((FIXTURES / "authors.json").read_text())
    paper_payload = json.loads((FIXTURES / "papers.json").read_text())

    class RoutingTransport:
        def __init__(self) -> None:
            self.requests: list[tuple[str, object]] = []

        def get(self, url: str, *, headers: object = None):
            self.requests.append((url, headers))
            payload = author_payload if "/author/search?" in url else paper_payload
            from research_query.ingestion.http import HttpResponse

            return HttpResponse(200, json.dumps(payload).encode(), {}, url)

    transport = RoutingTransport()
    source = SemanticScholarPublicationSource(transport, api_key="secret", now=lambda: now)
    assert isinstance(source, PublicationSource)
    authors = source.find_authors(faculty())
    papers = source.get_publications("111", candidate_limit=50, start_year=2020)
    assert authors.complete_for_request and len(authors.candidates) == 2
    assert authors.candidates[0].known_papers[0].doi == "10.1000/CRYSTAL"
    assert papers.complete_for_request and len(papers.records) == 2
    assert papers.records[1].url == "https://www.semanticscholar.org/paper/paper-c"
    assert all(headers == {"x-api-key": "secret"} for _, headers in transport.requests)


def test_provider_failure_is_an_incomplete_batch(fixture_transport: type) -> None:
    source = SemanticScholarPublicationSource(fixture_transport({}), now=lambda: datetime(2026, 9, 13, tzinfo=UTC))
    batch = source.find_authors(faculty())
    assert batch.complete_for_request is False
    assert batch.errors


def test_authentication_failure_is_not_downgraded_to_partial(fixture_transport: type) -> None:
    class UnauthorizedTransport:
        def get(self, url: str, *, headers: object = None):
            raise HTTPError(url, 401, "unauthorized", {}, None)

    source = SemanticScholarPublicationSource(UnauthorizedTransport(), now=lambda: datetime(2026, 9, 13, tzinfo=UTC))
    import pytest

    with pytest.raises(HTTPError):
        source.find_authors(faculty())
