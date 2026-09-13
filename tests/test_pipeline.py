from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from research_query.config import Settings
from research_query.data.models import (
    AuthorCandidate,
    AuthorCandidateBatch,
    FacultySourceSnapshot,
    PublicationBatch,
    RawAffiliation,
    RawFacultyRecord,
)
from research_query.data.repositories import AppliedFaculty, SnapshotApplyResult
from research_query.ingestion.normalize import normalize_faculty
from research_query.ingestion.pipeline import IngestionPipeline

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def raw() -> RawFacultyRecord:
    return RawFacultyRecord(
        "whiting",
        "1",
        "https://engineering.jhu.edu/faculty",
        "Alex Kim",
        "Professor",
        (RawAffiliation("Whiting School of Engineering", "Computer Science"),),
        "https://engineering.jhu.edu/faculty/alex-kim",
        research_summary="machine learning",
        source_role_category="Faculty",
    )


class FacultyFixtureSource:
    source_name = "whiting"

    def fetch_faculty(self) -> FacultySourceSnapshot:
        return FacultySourceSnapshot("whiting", "snapshot", NOW, (raw(),), True)


class FakeRepository:
    def __init__(self, existing: Mapping[str, object] | None = None) -> None:
        self.existing = existing
        self.resolutions: list[object] = []
        self.publication_calls: list[tuple[Sequence[object], bool]] = []
        self.finished: tuple[str, Sequence[str]] | None = None
        self.quarantined = 0

    @contextmanager
    def source_lock(self, source_name: str) -> Iterator[None]:
        yield

    def start_run(self, source_name: str, configuration: Mapping[str, object]) -> UUID:
        assert configuration["resolver_version"]
        return UUID(int=9)

    def finish_run(self, run_id: UUID, snapshot: object, *, status: str, counts=None, errors=()) -> None:
        self.finished = (status, errors)

    def apply_faculty_snapshot(self, snapshot: FacultySourceSnapshot) -> SnapshotApplyResult:
        applied = AppliedFaculty(UUID(int=1), UUID(int=2), normalize_faculty(raw()))
        return SnapshotApplyResult((applied,), 1, 1, 1, 0)

    def get_author_link(self, faculty_id: UUID, source_name: str):
        return self.existing

    def save_author_resolution(self, faculty_id: UUID, source_name: str, resolution: object) -> bool:
        self.resolutions.append(resolution)
        return True

    def apply_publication_documents(
        self,
        faculty_id: UUID,
        source_name: str,
        documents: Sequence[object],
        *,
        complete_for_request: bool,
        missing_grace_runs: int,
    ) -> int:
        self.publication_calls.append((documents, complete_for_request))
        return len(documents)

    def quarantine_publications(self, faculty_id: UUID, source_name: str) -> int:
        self.quarantined += 1
        return 1


class AmbiguousPublicationSource:
    source_name = "semantic_scholar"

    def __init__(self) -> None:
        self.publication_fetches = 0

    def find_authors(self, faculty: object) -> AuthorCandidateBatch:
        candidates = (
            AuthorCandidate(
                self.source_name,
                "1",
                "Alex Kim",
                ("Johns Hopkins University",),
                ("machine learning computer science engineering",),
                (),
            ),
            AuthorCandidate(
                self.source_name,
                "2",
                "Alex Kim",
                ("Johns Hopkins University",),
                ("machine learning computer science engineering",),
                (),
            ),
        )
        return AuthorCandidateBatch(candidates, NOW, True)

    def get_publications(self, author_id: str, *, candidate_limit: int, start_year: int | None) -> PublicationBatch:
        self.publication_fetches += 1
        return PublicationBatch((), NOW, True)


def test_pipeline_never_fetches_or_attaches_publications_for_ambiguous_author() -> None:
    repository = FakeRepository()
    publications = AmbiguousPublicationSource()
    report = IngestionPipeline(repository, FacultyFixtureSource(), publications, Settings()).run()
    assert report.status == "complete"
    assert report.traces[0].author_status == "ambiguous"
    assert report.traces[0].author_id is None
    assert publications.publication_fetches == 0
    assert repository.publication_calls == []


def test_failed_validation_quarantines_old_publications_without_switching_id() -> None:
    repository = FakeRepository(
        {
            "resolution_status": "resolved",
            "external_author_id": "old-id",
            "confidence": 0.9,
            "resolver_version": "weighted-signals-v1",
        }
    )
    publications = AmbiguousPublicationSource()
    report = IngestionPipeline(repository, FacultyFixtureSource(), publications, Settings()).run()
    assert report.traces[0].author_status == "ambiguous"
    assert repository.quarantined == 1
    assert publications.publication_fetches == 0
    assert repository.publication_calls == []


def test_manual_resolution_is_not_invalidated_by_automatic_candidate_search() -> None:
    repository = FakeRepository(
        {
            "resolution_status": "resolved",
            "external_author_id": "manual-id",
            "confidence": 1.0,
            "resolver_version": "manual",
        }
    )
    publications = AmbiguousPublicationSource()
    report = IngestionPipeline(repository, FacultyFixtureSource(), publications, Settings()).run()
    assert report.traces[0].author_status == "resolved"
    assert report.traces[0].author_id == "manual-id"
    assert repository.quarantined == 0
    assert publications.publication_fetches == 1
    assert repository.publication_calls == [((), True)]
