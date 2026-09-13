"""Validated boundary models; persistence rows never leak into adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    REVIEW = "review"
    EXCLUDED = "excluded"


class ResolutionStatus(StrEnum):
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    RESOLVED = "resolved"


class DocumentType(StrEnum):
    FACULTY_RESEARCH = "faculty_research"
    FACULTY_BIO = "faculty_bio"
    LAB_DESCRIPTION = "lab_description"
    PUBLICATION = "publication"


@dataclass(frozen=True, slots=True)
class RawAffiliation:
    school: str
    department: str | None = None
    title: str | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class RawFacultyRecord:
    source_name: str
    source_faculty_key: str
    source_url: str
    name: str
    title: str | None = None
    affiliations: tuple[RawAffiliation, ...] = ()
    profile_url: str | None = None
    lab_url: str | None = None
    research_areas: tuple[str, ...] = ()
    research_summary: str | None = None
    biography: str | None = None
    source_role_category: str | None = None
    eligibility_evidence: tuple[str, ...] = ()
    external_identifiers: Mapping[str, str] = field(default_factory=dict)
    source_payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FacultySourceSnapshot:
    source_name: str
    snapshot_id: str
    fetched_at: datetime
    records: tuple[RawFacultyRecord, ...]
    membership_complete: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FacultyForResolution:
    faculty_id: UUID
    name: str
    titles: tuple[str, ...]
    affiliations: tuple[str, ...]
    research_text: str
    known_publication_titles: tuple[str, ...]
    external_identifiers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    source_name: str
    external_id: str
    title: str
    abstract: str | None
    year: int | None
    publication_date: str | None
    url: str | None
    doi: str | None
    authors: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthorCandidate:
    source_name: str
    author_id: str
    name: str
    affiliations: tuple[str, ...]
    topics: tuple[str, ...]
    known_papers: tuple[PublicationRecord, ...]


@dataclass(frozen=True, slots=True)
class AuthorCandidateBatch:
    candidates: tuple[AuthorCandidate, ...]
    fetched_at: datetime
    complete_for_request: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationBatch:
    records: tuple[PublicationRecord, ...]
    fetched_at: datetime
    complete_for_request: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedFaculty:
    source: RawFacultyRecord
    normalized_name: str
    profile_url: str
    eligibility_status: EligibilityStatus
    eligibility_reason: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class AuthorResolution:
    status: ResolutionStatus
    external_author_id: str | None
    confidence: float | None
    resolver_version: str
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ResearchDocument:
    document_id: UUID
    faculty_id: UUID
    document_type: DocumentType
    title: str
    text: str
    publication_year: int | None
    source_url: str
    source_name: str
    external_id: str
    chunk_key: str
    content_hash: str
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    document_id: UUID
    content_hash: str
    text: str
    input_hash: str
