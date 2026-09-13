"""Linear Phase 1 ingestion orchestration with inspectable per-faculty traces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol
from uuid import UUID

from research_query.config import Settings
from research_query.data.models import (
    AuthorResolution,
    FacultyForResolution,
    FacultySourceSnapshot,
    ResearchDocument,
    ResolutionStatus,
)
from research_query.data.repositories import AppliedFaculty, SnapshotApplyResult
from research_query.documents.builder import build_publication_document
from research_query.ingestion.author_resolution import (
    RESOLVER_VERSION,
    ResolutionPolicy,
    names_compatible,
    resolve_author,
)
from research_query.ingestion.faculty.base import FacultySource
from research_query.ingestion.publications.base import PublicationSource
from research_query.ingestion.publications.policy import select_recent_publications


class CorpusRepository(Protocol):
    def source_lock(self, source_name: str) -> AbstractContextManager[None]: ...

    def start_run(self, source_name: str, configuration: Mapping[str, object]) -> UUID: ...

    def finish_run(
        self,
        run_id: UUID,
        snapshot: FacultySourceSnapshot | None,
        *,
        status: str,
        counts: Mapping[str, int] | None = None,
        errors: Sequence[str] = (),
    ) -> None: ...

    def apply_faculty_snapshot(self, snapshot: FacultySourceSnapshot) -> SnapshotApplyResult: ...

    def get_author_link(self, faculty_id: UUID, source_name: str) -> Mapping[str, object] | None: ...

    def save_author_resolution(self, faculty_id: UUID, source_name: str, resolution: AuthorResolution) -> bool: ...

    def apply_publication_documents(
        self,
        faculty_id: UUID,
        source_name: str,
        documents: Sequence[ResearchDocument],
        *,
        complete_for_request: bool,
        missing_grace_runs: int,
    ) -> int: ...

    def quarantine_publications(self, faculty_id: UUID, source_name: str) -> int: ...


@dataclass(frozen=True, slots=True)
class FacultyIngestionTrace:
    faculty_id: UUID
    source_faculty_key: str
    author_status: str
    author_id: str | None
    candidates_complete: bool
    publications_complete: bool | None
    publications_fetched: int
    publications_selected: int
    errors: tuple[str, ...]
    publication_diagnostics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestionReport:
    run_id: UUID
    snapshot_id: str | None
    membership_complete: bool
    status: str
    faculty_seen: int
    source_records_created: int
    source_records_deactivated: int
    documents_changed: int
    traces: tuple[FacultyIngestionTrace, ...]
    errors: tuple[str, ...]


def _resolution_input(applied: AppliedFaculty) -> FacultyForResolution:
    record = applied.normalized.source
    known_titles_raw = record.source_payload.get("known_publication_titles", [])
    known_titles = tuple(str(item) for item in known_titles_raw) if isinstance(known_titles_raw, list) else ()
    affiliations = tuple(" — ".join(filter(None, (item.school, item.department))) for item in record.affiliations)
    research_text = " ".join(filter(None, (record.research_summary, " ".join(record.research_areas), record.biography)))
    return FacultyForResolution(
        faculty_id=applied.faculty_id,
        name=record.name,
        titles=tuple(filter(None, (record.title,))),
        affiliations=affiliations,
        research_text=research_text,
        known_publication_titles=known_titles,
        external_identifiers=record.external_identifiers,
    )


class IngestionPipeline:
    def __init__(
        self,
        repository: CorpusRepository,
        faculty_source: FacultySource,
        publication_source: PublicationSource | None,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.faculty_source = faculty_source
        self.publication_source = publication_source
        self.settings = settings

    def _enrich(self, applied: AppliedFaculty, cutoff: date) -> tuple[FacultyIngestionTrace, int]:
        if self.publication_source is None:
            return (
                FacultyIngestionTrace(
                    applied.faculty_id,
                    applied.normalized.source.source_faculty_key,
                    "not_requested",
                    None,
                    True,
                    None,
                    0,
                    0,
                    (),
                ),
                0,
            )
        faculty = _resolution_input(applied)
        provider = self.publication_source
        batch = provider.find_authors(faculty)
        existing = self.repository.get_author_link(applied.faculty_id, provider.source_name)
        resolution: AuthorResolution | None
        author_id: str | None = None
        if existing and existing.get("resolution_status") == ResolutionStatus.RESOLVED.value:
            existing_id = str(existing.get("external_author_id") or "")
            persisted = next((item for item in batch.candidates if item.author_id == existing_id), None)
            if existing.get("resolver_version") == "manual" or not batch.complete_for_request:
                resolution = None
                author_id = existing_id
            elif persisted and names_compatible(faculty.name, persisted.name):
                author_id = existing_id
                confidence_value = existing.get("confidence")
                confidence = float(confidence_value) if isinstance(confidence_value, int | float) else 1.0
                resolution = AuthorResolution(
                    ResolutionStatus.RESOLVED,
                    existing_id,
                    confidence,
                    str(existing.get("resolver_version") or RESOLVER_VERSION),
                    {
                        "decision": "persisted author ID validated",
                        "candidate_id": existing_id,
                        "candidate_name": persisted.name,
                    },
                )
                self.repository.save_author_resolution(applied.faculty_id, provider.source_name, resolution)
            else:
                resolution = AuthorResolution(
                    ResolutionStatus.AMBIGUOUS,
                    None,
                    None,
                    RESOLVER_VERSION,
                    {"decision": "persisted author ID failed name/existence validation", "prior_id": existing_id},
                )
                self.repository.save_author_resolution(applied.faculty_id, provider.source_name, resolution)
                self.repository.quarantine_publications(applied.faculty_id, provider.source_name)
        else:
            resolution = resolve_author(
                faculty,
                batch,
                ResolutionPolicy(
                    self.settings.author_resolve_threshold,
                    self.settings.author_ambiguous_threshold,
                    self.settings.author_resolve_margin,
                ),
            )
            if resolution is not None:
                self.repository.save_author_resolution(applied.faculty_id, provider.source_name, resolution)
                author_id = resolution.external_author_id
        errors = list(batch.errors)
        if not author_id:
            status = (
                resolution.status.value
                if resolution is not None
                else str(existing.get("resolution_status", "preserved") if existing else "preserved")
            )
            return (
                FacultyIngestionTrace(
                    applied.faculty_id,
                    applied.normalized.source.source_faculty_key,
                    status,
                    None,
                    batch.complete_for_request,
                    None,
                    0,
                    0,
                    tuple(errors),
                ),
                0,
            )
        publication_batch = provider.get_publications(
            author_id,
            candidate_limit=self.settings.publication_candidate_limit,
            start_year=cutoff.year - self.settings.publication_lookback_years + 1,
        )
        errors.extend(publication_batch.errors)
        selected = select_recent_publications(
            publication_batch.records,
            cutoff_date=cutoff,
            lookback_years=self.settings.publication_lookback_years,
            maximum=self.settings.max_publications_per_faculty,
        )
        documents = tuple(build_publication_document(applied.faculty_id, item) for item in selected)
        earliest_year = cutoff.year - self.settings.publication_lookback_years + 1
        null_year = sum(item.year is None for item in publication_batch.records)
        outside_window = sum(
            item.year is not None and not earliest_year <= item.year <= cutoff.year
            for item in publication_batch.records
        )
        changed = self.repository.apply_publication_documents(
            applied.faculty_id,
            provider.source_name,
            documents,
            complete_for_request=publication_batch.complete_for_request,
            missing_grace_runs=self.settings.publication_missing_grace_runs,
        )
        return (
            FacultyIngestionTrace(
                applied.faculty_id,
                applied.normalized.source.source_faculty_key,
                ResolutionStatus.RESOLVED.value,
                author_id,
                batch.complete_for_request,
                publication_batch.complete_for_request,
                len(publication_batch.records),
                len(selected),
                tuple(errors),
                {
                    "null_year_excluded": null_year,
                    "outside_window_excluded": outside_window,
                    "deduplicated_or_capacity_excluded": max(
                        0,
                        len(publication_batch.records) - null_year - outside_window - len(selected),
                    ),
                },
            ),
            changed,
        )

    def run(self) -> IngestionReport:
        source_name = self.faculty_source.source_name
        snapshot: FacultySourceSnapshot | None = None
        run_id: UUID | None = None
        with self.repository.source_lock(source_name):
            try:
                run_id = self.repository.start_run(
                    source_name,
                    {
                        "eligibility_policy": self.settings.faculty_eligibility_policy_version,
                        "publication_cutoff_policy": "recent-calendar-years-v1",
                        "publication_lookback_years": self.settings.publication_lookback_years,
                        "publication_candidate_limit": self.settings.publication_candidate_limit,
                        "max_publications_per_faculty": self.settings.max_publications_per_faculty,
                        "resolver_version": RESOLVER_VERSION,
                    },
                )
                snapshot = self.faculty_source.fetch_faculty()
                applied = self.repository.apply_faculty_snapshot(snapshot)
                traces: list[FacultyIngestionTrace] = []
                publication_changes = 0
                for item in applied.applied:
                    trace, changed = self._enrich(item, snapshot.fetched_at.date())
                    traces.append(trace)
                    publication_changes += changed
                errors = tuple(snapshot.errors) + tuple(error for trace in traces for error in trace.errors)
                status = "partial" if errors else "complete"
                counts = {
                    "records_seen": len(snapshot.records),
                    "records_created": applied.created_source_records,
                    "records_updated": max(0, len(snapshot.records) - applied.created_source_records),
                    "records_deactivated": applied.deactivated_source_records,
                    "documents_created": applied.changed_documents + publication_changes,
                }
                self.repository.finish_run(run_id, snapshot, status=status, counts=counts, errors=errors)
                return IngestionReport(
                    run_id,
                    snapshot.snapshot_id,
                    snapshot.membership_complete,
                    status,
                    len(snapshot.records),
                    applied.created_source_records,
                    applied.deactivated_source_records,
                    applied.changed_documents + publication_changes,
                    tuple(traces),
                    errors,
                )
            except Exception as exc:
                if run_id is not None:
                    self.repository.finish_run(run_id, snapshot, status="failed", errors=(str(exc),))
                raise
