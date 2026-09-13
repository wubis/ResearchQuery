"""Semantic Scholar Graph API adapter with bounded, complete-request semantics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode

from research_query.data.models import (
    AuthorCandidate,
    AuthorCandidateBatch,
    FacultyForResolution,
    PublicationBatch,
    PublicationRecord,
)
from research_query.ingestion.http import HttpTransport

_API_ROOT = "https://api.semanticscholar.org/graph/v1"
_PAPER_FIELDS = "paperId,title,abstract,year,publicationDate,url,externalIds,authors,venue,fieldsOfStudy"


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _publication(value: object) -> PublicationRecord | None:
    data = _mapping(value)
    paper_id = str(data.get("paperId") or "").strip()
    title = str(data.get("title") or "").strip()
    if not paper_id or not title:
        return None
    external_ids = _mapping(data.get("externalIds"))
    authors_value = data.get("authors")
    authors_raw: list[object] = authors_value if isinstance(authors_value, list) else []
    authors = tuple(
        str(_mapping(author).get("name") or "").strip()
        for author in authors_raw
        if str(_mapping(author).get("name") or "").strip()
    )
    year_raw = data.get("year")
    return PublicationRecord(
        source_name="semantic_scholar",
        external_id=paper_id,
        title=title,
        abstract=str(data["abstract"]).strip() if data.get("abstract") else None,
        year=int(year_raw) if isinstance(year_raw, int | float) else None,
        publication_date=str(data["publicationDate"]) if data.get("publicationDate") else None,
        url=str(data["url"]) if data.get("url") else f"https://www.semanticscholar.org/paper/{paper_id}",
        doi=str(external_ids["DOI"]).strip() if external_ids.get("DOI") else None,
        authors=authors,
        metadata={
            "venue": data.get("venue"),
            "fields_of_study": data.get("fieldsOfStudy") or [],
            "external_ids": dict(external_ids),
        },
    )


class SemanticScholarPublicationSource:
    source_name = "semantic_scholar"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_key: str | None = None,
        now: Callable[[], datetime] | None = None,
        candidate_count: int = 10,
    ) -> None:
        self.transport = transport
        self.api_key = api_key
        self.now = now or (lambda: datetime.now(UTC))
        self.candidate_count = candidate_count

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    def find_authors(self, faculty: FacultyForResolution) -> AuthorCandidateBatch:
        query_parts = [faculty.name, "Johns Hopkins"]
        if faculty.affiliations:
            query_parts.append(faculty.affiliations[0])
        query = " ".join(query_parts)
        params = urlencode(
            {
                "query": query,
                "limit": self.candidate_count,
                "fields": "authorId,name,affiliations,papers.paperId,papers.title,"
                "papers.abstract,papers.year,papers.publicationDate,papers.url,"
                "papers.externalIds,papers.authors,papers.venue,papers.fieldsOfStudy",
            }
        )
        fetched_at = self.now()
        try:
            response = self.transport.get(f"{_API_ROOT}/author/search?{params}", headers=self._headers)
            payload = _mapping(response.json())
            values = payload.get("data")
            if not isinstance(values, list):
                raise ValueError("author response has no data list")
            candidates: list[AuthorCandidate] = []
            errors: list[str] = []
            for raw in values:
                data = _mapping(raw)
                author_id = str(data.get("authorId") or "").strip()
                name = str(data.get("name") or "").strip()
                if not author_id or not name:
                    errors.append("candidate missing authorId or name")
                    continue
                papers_value = data.get("papers")
                papers_raw: list[object] = papers_value if isinstance(papers_value, list) else []
                papers = tuple(publication for item in papers_raw if (publication := _publication(item)) is not None)
                topics: list[str] = []
                for paper in papers:
                    topics.extend(_strings(paper.metadata.get("fields_of_study")))
                candidates.append(
                    AuthorCandidate(
                        source_name=self.source_name,
                        author_id=author_id,
                        name=name,
                        affiliations=_strings(data.get("affiliations")),
                        topics=tuple(dict.fromkeys(topics)),
                        known_papers=papers,
                    )
                )
            return AuthorCandidateBatch(
                candidates=tuple(candidates),
                fetched_at=fetched_at,
                complete_for_request=not errors,
                errors=tuple(errors),
            )
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise
            return AuthorCandidateBatch((), fetched_at, False, (str(exc),))
        except Exception as exc:
            return AuthorCandidateBatch((), fetched_at, False, (str(exc),))

    def get_publications(self, author_id: str, *, candidate_limit: int, start_year: int | None) -> PublicationBatch:
        fetched_at = self.now()
        offset = 0
        records: list[PublicationRecord] = []
        try:
            while len(records) < candidate_limit:
                page_limit = min(100, candidate_limit - len(records))
                params = urlencode({"limit": page_limit, "offset": offset, "fields": _PAPER_FIELDS})
                response = self.transport.get(f"{_API_ROOT}/author/{author_id}/papers?{params}", headers=self._headers)
                payload = _mapping(response.json())
                values = payload.get("data")
                if not isinstance(values, list):
                    raise ValueError("publication response has no data list")
                for item in values:
                    publication = _publication(item)
                    in_range = publication is not None and (
                        start_year is None or publication.year is None or publication.year >= start_year
                    )
                    if publication is not None and in_range:
                        records.append(publication)
                        if len(records) == candidate_limit:
                            break
                next_offset = payload.get("next")
                if next_offset is None or len(values) == 0 or len(records) >= candidate_limit:
                    break
                if not isinstance(next_offset, int) or next_offset <= offset:
                    raise ValueError("invalid Semantic Scholar pagination offset")
                offset = next_offset
            return PublicationBatch(tuple(records), fetched_at, True)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise
            return PublicationBatch(tuple(records), fetched_at, False, (str(exc),))
        except Exception as exc:
            return PublicationBatch(tuple(records), fetched_at, False, (str(exc),))
