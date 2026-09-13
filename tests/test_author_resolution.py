from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from research_query.data.models import (
    AuthorCandidate,
    AuthorCandidateBatch,
    FacultyForResolution,
    PublicationRecord,
    ResolutionStatus,
)
from research_query.ingestion.author_resolution import resolve_author


def publication(title: str) -> PublicationRecord:
    return PublicationRecord("semantic_scholar", title, title, "abstract", 2026, None, None, None, (), {})


def faculty(name: str = "Jane Q. Doe") -> FacultyForResolution:
    return FacultyForResolution(
        UUID(int=1),
        name,
        ("Professor",),
        ("Johns Hopkins University", "Materials Science"),
        "machine learning materials crystal",
        ("Crystal Generation",),
        {},
    )


def candidate(
    author_id: str,
    *,
    name: str = "Jane Q. Doe",
    affiliation: str = "Johns Hopkins University",
    paper: str = "Crystal Generation",
) -> AuthorCandidate:
    return AuthorCandidate(
        "semantic_scholar",
        author_id,
        name,
        (affiliation,),
        ("machine learning", "materials"),
        (publication(paper),),
    )


def batch(*items: AuthorCandidate, complete: bool = True) -> AuthorCandidateBatch:
    return AuthorCandidateBatch(tuple(items), datetime(2026, 9, 13, tzinfo=UTC), complete)


def test_resolves_only_strong_clear_candidate_and_records_features() -> None:
    resolution = resolve_author(
        faculty(),
        batch(candidate("1"), candidate("2", affiliation="Other University", paper="Unrelated")),
    )
    assert resolution is not None
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.external_author_id == "1"
    assert resolution.confidence is not None and resolution.confidence >= 0.8
    assert resolution.evidence["margin"] >= 0.15
    assert resolution.evidence["candidates"]


def test_close_common_name_candidates_are_ambiguous_and_attach_no_id() -> None:
    resolution = resolve_author(
        faculty("Alex Kim"),
        batch(candidate("1", name="Alex Kim"), candidate("2", name="Alex Kim")),
    )
    assert resolution is not None
    assert resolution.status is ResolutionStatus.AMBIGUOUS
    assert resolution.external_author_id is None


def test_name_only_or_contradictory_candidate_is_unresolved() -> None:
    weak = AuthorCandidate("semantic_scholar", "1", "Jane Q. Doe", (), (), ())
    resolution = resolve_author(faculty(), batch(weak))
    assert resolution is not None
    assert resolution.status is ResolutionStatus.UNRESOLVED
    contradictory = candidate("2", name="Jane R. Doe", affiliation="Other University", paper="Unrelated")
    resolution = resolve_author(faculty(), batch(contradictory))
    assert resolution is not None and resolution.status is ResolutionStatus.UNRESOLVED


def test_incomplete_candidate_fetch_preserves_prior_state() -> None:
    assert resolve_author(faculty(), batch(candidate("1"), complete=False)) is None


def test_explicit_provider_id_resolves_after_name_validation() -> None:
    value = faculty()
    value = FacultyForResolution(
        value.faculty_id,
        value.name,
        value.titles,
        value.affiliations,
        value.research_text,
        value.known_publication_titles,
        {"semantic_scholar": "explicit"},
    )
    direct = AuthorCandidate("semantic_scholar", "explicit", "Jane Q. Doe", ("Other University",), (), ())
    resolution = resolve_author(value, batch(direct))
    assert resolution is not None
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.external_author_id == "explicit"
    assert resolution.confidence == 1.0
