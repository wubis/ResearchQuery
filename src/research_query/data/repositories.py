"""Transactional PostgreSQL corpus persistence."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from research_query.data.models import (
    AuthorResolution,
    EmbeddingInput,
    FacultySourceSnapshot,
    NormalizedFaculty,
    ResearchDocument,
    ResolutionStatus,
)
from research_query.documents.builder import TokenCounter, build_hopkins_documents
from research_query.embeddings.model import embedding_input_hash, l2_normalize
from research_query.ingestion.normalize import (
    canonicalize_url,
    clean_text,
    normalize_faculty,
    normalize_unit,
)


class ConcurrentIngestionError(RuntimeError):
    """Raised when another run owns the source-scoped advisory lock."""


class IncompleteEmbeddingIndexError(RuntimeError):
    """Raised when candidate index activation would expose missing or stale vectors."""


class FacultyMergeConflict(RuntimeError):
    """Raised when a manual canonical merge needs human identity review."""


@dataclass(frozen=True, slots=True)
class AppliedFaculty:
    faculty_id: UUID
    source_record_id: UUID
    normalized: NormalizedFaculty


@dataclass(frozen=True, slots=True)
class SnapshotApplyResult:
    applied: tuple[AppliedFaculty, ...]
    created_faculty: int
    created_source_records: int
    changed_documents: int
    deactivated_source_records: int


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list | tuple) else []


def _provider_ids(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _string_list(ids) for key, ids in value.items()}


def _record_payload(normalized: NormalizedFaculty) -> dict[str, object]:
    payload = asdict(normalized.source)
    source = normalized.source
    payload["_normalized"] = {
        "name": clean_text(source.name) or source.name,
        "normalized_name": normalized.normalized_name,
        "title": clean_text(source.title),
        "profile_url": normalized.profile_url,
        "lab_url": canonicalize_url(source.lab_url) if source.lab_url else None,
        "research_summary": clean_text(source.research_summary),
        "eligibility_status": normalized.eligibility_status.value,
        "eligibility_reason": normalized.eligibility_reason,
        "affiliations": [
            {
                "school": normalize_unit(item.school) or item.school,
                "department": normalize_unit(item.department),
                "title": clean_text(item.title),
                "source_url": canonicalize_url(item.source_url) if item.source_url else None,
            }
            for item in source.affiliations
        ],
    }
    return payload


class PostgresCorpusRepository:
    def __init__(self, pool: Any, *, token_counter: TokenCounter | None = None) -> None:
        self.pool = pool
        self.token_counter = token_counter

    @contextmanager
    def source_lock(self, source_name: str) -> Iterator[None]:
        """Serialize a complete fetch/apply run and always release the session lock."""
        with self.pool.connection() as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (source_name,)
            ).fetchone()[0]
            if not acquired:
                raise ConcurrentIngestionError(f"source {source_name!r} is already ingesting")
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (source_name,))

    def start_run(self, source_name: str, configuration: Mapping[str, object]) -> UUID:
        run_id = uuid4()
        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO ingestion_runs (run_id, source_name, status, configuration)
                VALUES (%s, %s, 'running', %s::jsonb)
                """,
                (run_id, source_name, _json(configuration)),
            )
        return run_id

    def finish_run(
        self,
        run_id: UUID,
        snapshot: FacultySourceSnapshot | None,
        *,
        status: str,
        counts: Mapping[str, int] | None = None,
        errors: Sequence[str] = (),
    ) -> None:
        counts = counts or {}
        with self.pool.connection() as connection:
            connection.execute(
                """
                UPDATE ingestion_runs SET
                    snapshot_id = %s,
                    finished_at = %s,
                    membership_complete = %s,
                    status = %s,
                    records_seen = %s,
                    records_created = %s,
                    records_updated = %s,
                    records_deactivated = %s,
                    documents_created = %s,
                    documents_updated = %s,
                    error_summary = %s::jsonb,
                    configuration = configuration || %s::jsonb
                WHERE run_id = %s
                """,
                (
                    snapshot.snapshot_id if snapshot else None,
                    datetime.now(UTC),
                    snapshot.membership_complete if snapshot else False,
                    status,
                    counts.get("records_seen", 0),
                    counts.get("records_created", 0),
                    counts.get("records_updated", 0),
                    counts.get("records_deactivated", 0),
                    counts.get("documents_created", 0),
                    counts.get("documents_updated", 0),
                    _json(list(errors)),
                    _json({"publication_cutoff_date": snapshot.fetched_at.date().isoformat()} if snapshot else {}),
                    run_id,
                ),
            )

    def _find_or_create_faculty(self, connection: Any, normalized: NormalizedFaculty) -> tuple[UUID, bool]:
        rows = connection.execute(
            """
            SELECT DISTINCT faculty_id
            FROM faculty_source_records
            WHERE lower(regexp_replace(COALESCE(profile_url, source_url), '/+$', '')) = %s
            LIMIT 2
            """,
            (normalized.profile_url.casefold(),),
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0], False
        faculty_id = uuid4()
        canonical = _record_payload(normalized)["_normalized"]
        if not isinstance(canonical, dict):
            raise TypeError("normalized source payload is malformed")
        connection.execute(
            """
            INSERT INTO faculty (
                faculty_id, name, normalized_name, title, profile_url, lab_url,
                research_summary, eligibility_status, eligibility_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                faculty_id,
                canonical["name"],
                normalized.normalized_name,
                canonical["title"],
                normalized.profile_url,
                canonical["lab_url"],
                canonical["research_summary"],
                normalized.eligibility_status.value,
                normalized.eligibility_reason,
            ),
        )
        return faculty_id, True

    def _upsert_document(self, connection: Any, document: ResearchDocument) -> bool:
        existing = connection.execute(
            """
            SELECT document_id, content_hash FROM research_documents
            WHERE document_id = %s
            """,
            (document.document_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO research_documents (
                document_id, faculty_id, document_type, title, text, publication_year,
                source_url, source_name, external_id, chunk_key, content_hash, metadata, is_active,
                missing_run_count
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, true, 0)
            ON CONFLICT (document_id) DO UPDATE SET
                title = EXCLUDED.title,
                text = EXCLUDED.text,
                publication_year = EXCLUDED.publication_year,
                source_url = EXCLUDED.source_url,
                source_name = EXCLUDED.source_name,
                content_hash = EXCLUDED.content_hash,
                metadata = EXCLUDED.metadata,
                is_active = true,
                missing_run_count = 0
            """,
            (
                document.document_id,
                document.faculty_id,
                document.document_type.value,
                document.title,
                document.text,
                document.publication_year,
                document.source_url,
                document.source_name,
                document.external_id,
                document.chunk_key,
                document.content_hash,
                _json(document.metadata),
            ),
        )
        return existing is None or existing[1] != document.content_hash

    def _rebuild_faculty(self, connection: Any, faculty_id: UUID) -> None:
        rows = connection.execute(
            """
            SELECT source_record_id, source_name, raw_payload,
                   CASE WHEN raw_payload->>'research_summary' IS NOT NULL THEN 0 ELSE 1 END AS richness
            FROM faculty_source_records
            WHERE faculty_id = %s AND is_active
            ORDER BY richness, updated_at DESC, source_name, source_faculty_key
            """,
            (faculty_id,),
        ).fetchall()
        if not rows:
            connection.execute("UPDATE faculty SET is_active = false WHERE faculty_id = %s", (faculty_id,))
            connection.execute(
                "UPDATE faculty_affiliations SET is_active = false WHERE faculty_id = %s",
                (faculty_id,),
            )
            return
        preferred = rows[0][2]
        norm = preferred["_normalized"]
        connection.execute(
            """
            UPDATE faculty SET name = %s, normalized_name = %s, title = %s,
                profile_url = %s, lab_url = %s, research_summary = %s, is_active = true,
                eligibility_status = %s, eligibility_reason = %s
            WHERE faculty_id = %s
            """,
            (
                norm["name"],
                norm["normalized_name"],
                norm.get("title"),
                norm["profile_url"],
                norm.get("lab_url"),
                norm.get("research_summary"),
                norm["eligibility_status"],
                norm["eligibility_reason"],
                faculty_id,
            ),
        )
        desired: dict[tuple[str, str], tuple[UUID, Mapping[str, object]]] = {}
        for source_record_id, _source_name, payload, _richness in rows:
            normalized_payload = payload.get("_normalized", {})
            for affiliation in normalized_payload.get("affiliations", []):
                school = str(affiliation.get("school") or "").strip()
                department = str(affiliation.get("department") or "").strip()
                if school and (school, department.casefold()) not in desired:
                    desired[(school, department.casefold())] = (source_record_id, affiliation)
        connection.execute(
            "UPDATE faculty_affiliations SET is_active = false, is_primary = false WHERE faculty_id = %s",
            (faculty_id,),
        )
        for index, ((school, _department_key), (source_record_id, affiliation)) in enumerate(
            sorted(desired.items()), start=0
        ):
            department_value = affiliation.get("department")
            stored_department = str(department_value) if department_value else None
            existing = connection.execute(
                """
                SELECT affiliation_id FROM faculty_affiliations
                WHERE faculty_id = %s AND school = %s AND COALESCE(department, '') = COALESCE(%s, '')
                ORDER BY created_at LIMIT 1
                """,
                (faculty_id, school, stored_department),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE faculty_affiliations SET title = %s, source_record_id = %s,
                        is_active = true, is_primary = %s WHERE affiliation_id = %s
                    """,
                    (affiliation.get("title"), source_record_id, index == 0, existing[0]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO faculty_affiliations (
                        affiliation_id, faculty_id, school, department, title,
                        is_primary, source_record_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        faculty_id,
                        school,
                        stored_department,
                        affiliation.get("title"),
                        index == 0,
                        source_record_id,
                    ),
                )

    def apply_faculty_snapshot(self, snapshot: FacultySourceSnapshot) -> SnapshotApplyResult:
        normalized_records = tuple(normalize_faculty(record) for record in snapshot.records)
        applied: list[AppliedFaculty] = []
        created_faculty = 0
        created_records = 0
        changed_documents = 0
        affected: set[UUID] = set()
        seen_keys: list[str] = []
        deactivated = 0
        with self.pool.connection() as connection, connection.transaction():
            for normalized in normalized_records:
                source = normalized.source
                seen_keys.append(source.source_faculty_key)
                existing = connection.execute(
                    """
                    SELECT source_record_id, faculty_id FROM faculty_source_records
                    WHERE source_name = %s AND source_faculty_key = %s
                    """,
                    (source.source_name, source.source_faculty_key),
                ).fetchone()
                if existing:
                    source_record_id, faculty_id = existing
                else:
                    faculty_id, is_created_faculty = self._find_or_create_faculty(connection, normalized)
                    created_faculty += int(is_created_faculty)
                    source_record_id = uuid4()
                    created_records += 1
                payload = _record_payload(normalized)
                connection.execute(
                    """
                    INSERT INTO faculty_source_records (
                        source_record_id, faculty_id, source_name, source_faculty_key,
                        source_url, profile_url, raw_payload, content_hash, last_seen_at,
                        missing_run_count, is_active
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 0, true)
                    ON CONFLICT (source_name, source_faculty_key) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        profile_url = EXCLUDED.profile_url,
                        raw_payload = EXCLUDED.raw_payload,
                        content_hash = EXCLUDED.content_hash,
                        last_seen_at = CASE WHEN %s THEN EXCLUDED.last_seen_at
                                            ELSE faculty_source_records.last_seen_at END,
                        missing_run_count = 0,
                        is_active = true
                    """,
                    (
                        source_record_id,
                        faculty_id,
                        source.source_name,
                        source.source_faculty_key,
                        canonicalize_url(source.source_url),
                        normalized.profile_url,
                        _json(payload),
                        normalized.content_hash,
                        snapshot.fetched_at,
                        snapshot.membership_complete,
                    ),
                )
                hopkins_documents = build_hopkins_documents(
                    faculty_id,
                    source_record_id,
                    source,
                    token_counter=self.token_counter,
                )
                for document in hopkins_documents:
                    changed_documents += int(self._upsert_document(connection, document))
                if "profile_error" not in source.source_payload:
                    active_document_ids = [document.document_id for document in hopkins_documents]
                    connection.execute(
                        """
                        UPDATE research_documents SET is_active = false
                        WHERE metadata->>'source_record_id' = %s
                          AND document_type <> 'publication'
                          AND NOT (document_id = ANY(%s::uuid[]))
                          AND is_active
                        """,
                        (str(source_record_id), active_document_ids),
                    )
                applied.append(AppliedFaculty(faculty_id, source_record_id, normalized))
                affected.add(faculty_id)
            if snapshot.membership_complete:
                missing_rows = connection.execute(
                    """
                    UPDATE faculty_source_records
                    SET missing_run_count = missing_run_count + 1,
                        is_active = CASE WHEN missing_run_count + 1 >= 2 THEN false ELSE is_active END
                    WHERE source_name = %s AND NOT (source_faculty_key = ANY(%s::text[])) AND is_active
                    RETURNING source_record_id, faculty_id, is_active
                    """,
                    (snapshot.source_name, seen_keys),
                ).fetchall()
                for source_record_id, faculty_id, is_active in missing_rows:
                    affected.add(faculty_id)
                    if not is_active:
                        deactivated += 1
                        connection.execute(
                            """
                            UPDATE research_documents SET is_active = false
                            WHERE metadata->>'source_record_id' = %s
                            """,
                            (str(source_record_id),),
                        )
            for faculty_id in sorted(affected, key=str):
                self._rebuild_faculty(connection, faculty_id)
        return SnapshotApplyResult(tuple(applied), created_faculty, created_records, changed_documents, deactivated)

    def get_author_link(self, faculty_id: UUID, source_name: str) -> Mapping[str, object] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT external_author_id, resolution_status, confidence, resolver_version,
                       evidence, validated_at
                FROM scholarly_author_links WHERE faculty_id = %s AND source_name = %s
                """,
                (faculty_id, source_name),
            ).fetchone()
        if row is None:
            return None
        return {
            "external_author_id": row[0],
            "resolution_status": row[1],
            "confidence": row[2],
            "resolver_version": row[3],
            "evidence": row[4],
            "validated_at": row[5],
        }

    def save_author_resolution(self, faculty_id: UUID, source_name: str, resolution: AuthorResolution) -> bool:
        with self.pool.connection() as connection, connection.transaction():
            existing = connection.execute(
                """
                SELECT resolver_version FROM scholarly_author_links
                WHERE faculty_id = %s AND source_name = %s FOR UPDATE
                """,
                (faculty_id, source_name),
            ).fetchone()
            if existing and existing[0] == "manual":
                return False
            connection.execute(
                """
                INSERT INTO scholarly_author_links (
                    author_link_id, faculty_id, source_name, external_author_id,
                    resolution_status, confidence, resolver_version, evidence, validated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (faculty_id, source_name) DO UPDATE SET
                    external_author_id = EXCLUDED.external_author_id,
                    resolution_status = EXCLUDED.resolution_status,
                    confidence = EXCLUDED.confidence,
                    resolver_version = EXCLUDED.resolver_version,
                    evidence = EXCLUDED.evidence,
                    validated_at = EXCLUDED.validated_at
                """,
                (
                    uuid4(),
                    faculty_id,
                    source_name,
                    resolution.external_author_id,
                    resolution.status.value,
                    resolution.confidence,
                    resolution.resolver_version,
                    _json(resolution.evidence),
                    datetime.now(UTC) if resolution.status == ResolutionStatus.RESOLVED else None,
                ),
            )
        return True

    def apply_publication_documents(
        self,
        faculty_id: UUID,
        source_name: str,
        documents: Sequence[ResearchDocument],
        *,
        complete_for_request: bool,
        missing_grace_runs: int,
    ) -> int:
        if any(document.faculty_id != faculty_id for document in documents):
            raise ValueError("publication document owner mismatch")
        changed = 0
        active_ids: list[UUID] = []
        with self.pool.connection() as connection, connection.transaction():
            for document in documents:
                if document.document_type.value != "publication":
                    raise ValueError("only publication documents are accepted")
                aliases = sorted(
                    set(
                        [
                            document.external_id,
                            *_string_list(document.metadata.get("identity_aliases", [])),
                        ]
                    )
                )
                existing = connection.execute(
                    """
                    SELECT document_id, external_id, metadata
                    FROM research_documents
                    WHERE faculty_id = %s AND document_type = 'publication'
                      AND (
                        external_id = ANY(%s::text[])
                        OR (metadata->'identity_aliases') ?| %s::text[]
                      )
                    ORDER BY is_active DESC, created_at
                    LIMIT 1
                    """,
                    (faculty_id, aliases, aliases),
                ).fetchone()
                if existing and existing[0] != document.document_id:
                    old_document_id, immutable_external_id, old_metadata = existing
                    merged_metadata = dict(old_metadata)
                    merged_metadata.update(document.metadata)
                    merged_metadata["identity_aliases"] = sorted(
                        set(_string_list(old_metadata.get("identity_aliases", [])))
                        | set(_string_list(document.metadata.get("identity_aliases", [])))
                        | {immutable_external_id, document.external_id}
                    )
                    old_provider_ids = _provider_ids(old_metadata.get("provider_ids", {}))
                    new_provider_ids = _provider_ids(document.metadata.get("provider_ids", {}))
                    provider_ids: dict[str, list[str]] = {}
                    for provider in set(old_provider_ids) | set(new_provider_ids):
                        provider_ids[provider] = sorted(
                            set(old_provider_ids.get(provider, [])) | set(new_provider_ids.get(provider, []))
                        )
                    merged_metadata["provider_ids"] = provider_ids
                    document = replace(
                        document,
                        document_id=old_document_id,
                        external_id=immutable_external_id,
                        metadata=merged_metadata,
                    )
                active_ids.append(document.document_id)
                changed += int(self._upsert_document(connection, document))
            if complete_for_request:
                rows = connection.execute(
                    """
                    UPDATE research_documents
                    SET missing_run_count = missing_run_count + 1,
                        is_active = CASE WHEN missing_run_count + 1 > %s THEN false ELSE is_active END
                    WHERE faculty_id = %s AND document_type = 'publication' AND source_name = %s
                      AND NOT (document_id = ANY(%s::uuid[])) AND is_active
                    RETURNING document_id
                    """,
                    (missing_grace_runs, faculty_id, source_name, active_ids),
                ).fetchall()
                changed += len(rows)
        return changed

    def quarantine_publications(self, faculty_id: UUID, source_name: str) -> int:
        """Immediately remove evidence when a persisted author identity fails validation."""
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                UPDATE research_documents SET is_active = false,
                    metadata = metadata || '{"quarantine_reason":"author_validation_failed"}'::jsonb
                WHERE faculty_id = %s AND document_type = 'publication'
                  AND source_name = %s AND is_active
                RETURNING document_id
                """,
                (faculty_id, source_name),
            ).fetchall()
        return len(rows)

    def create_corpus_snapshot(self, source_runs: Mapping[str, UUID], *, description: str | None = None) -> UUID:
        if not source_runs:
            raise ValueError("a corpus snapshot requires at least one completed source run")
        snapshot_id = uuid4()
        run_ids = list(source_runs.values())
        with self.pool.connection() as connection, connection.transaction():
            valid_rows = connection.execute(
                """
                SELECT source_name, run_id FROM ingestion_runs
                WHERE run_id = ANY(%s::uuid[])
                  AND membership_complete
                  AND status IN ('complete', 'partial')
                """,
                (run_ids,),
            ).fetchall()
            valid = {(str(source_name), run_id) for source_name, run_id in valid_rows}
            expected = set(source_runs.items())
            if valid != expected:
                raise ValueError("corpus snapshots may reference only complete membership runs")
            connection.execute(
                """
                INSERT INTO corpus_snapshots (corpus_snapshot_id, source_runs, description)
                VALUES (%s, %s::jsonb, %s)
                """,
                (snapshot_id, _json({key: str(value) for key, value in source_runs.items()}), description),
            )
        return snapshot_id

    def latest_complete_source_runs(self, source_names: Sequence[str]) -> dict[str, UUID]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ON (source_name) source_name, run_id
                FROM ingestion_runs
                WHERE source_name = ANY(%s::text[])
                  AND membership_complete
                  AND status IN ('complete', 'partial')
                ORDER BY source_name, finished_at DESC
                """,
                (list(source_names),),
            ).fetchall()
        found = {str(source_name): run_id for source_name, run_id in rows}
        missing = sorted(set(source_names) - set(found))
        if missing:
            raise ValueError(f"no complete membership run exists for sources: {missing}")
        return found

    def inspect_corpus(self) -> dict[str, object]:
        """Return compact operational health data without exposing raw provider payloads."""
        with self.pool.connection() as connection:
            faculty = connection.execute(
                """
                SELECT count(*) FILTER (WHERE is_active),
                       count(*) FILTER (WHERE NOT is_active),
                       count(*) FILTER (WHERE is_active AND eligibility_status = 'eligible'),
                       count(*) FILTER (WHERE is_active AND eligibility_status = 'review'),
                       count(*) FILTER (WHERE is_active AND eligibility_status = 'excluded'),
                       count(*) FILTER (WHERE is_active AND research_summary IS NOT NULL)
                FROM faculty
                """
            ).fetchone()
            source_records = connection.execute(
                """
                SELECT source_name,
                       count(*) FILTER (WHERE is_active),
                       count(*) FILTER (WHERE NOT is_active),
                       count(*) FILTER (WHERE is_active AND missing_run_count > 0)
                FROM faculty_source_records GROUP BY source_name ORDER BY source_name
                """
            ).fetchall()
            resolutions = connection.execute(
                """
                SELECT source_name, resolution_status, count(*)
                FROM scholarly_author_links
                GROUP BY source_name, resolution_status
                ORDER BY source_name, resolution_status
                """
            ).fetchall()
            document_rows = connection.execute(
                """
                SELECT document_type, count(*) FILTER (WHERE is_active),
                       count(*) FILTER (WHERE NOT is_active),
                       count(*) FILTER (WHERE source_url = '')
                FROM research_documents GROUP BY document_type ORDER BY document_type
                """
            ).fetchall()
            index_state = connection.execute(
                "SELECT embedding_version, activated_at, corpus_snapshot_id FROM search_index_state"
            ).fetchone()
            missing_embeddings = None
            if index_state:
                missing_embeddings = connection.execute(
                    """
                    SELECT count(*) FROM research_documents d
                    JOIN faculty f ON f.faculty_id = d.faculty_id
                    LEFT JOIN document_embeddings e
                      ON e.document_id = d.document_id AND e.embedding_version = %s
                     AND e.document_content_hash = d.content_hash
                    WHERE d.is_active AND f.is_active AND f.eligibility_status = 'eligible'
                      AND e.document_id IS NULL
                    """,
                    (index_state[0],),
                ).fetchone()[0]
            recent_runs = connection.execute(
                """
                SELECT source_name, status, membership_complete, records_seen, error_summary,
                       started_at, finished_at
                FROM ingestion_runs
                ORDER BY started_at DESC LIMIT 20
                """
            ).fetchall()
        return {
            "faculty": {
                "active": faculty[0],
                "inactive": faculty[1],
                "eligible": faculty[2],
                "review": faculty[3],
                "excluded": faculty[4],
                "with_research_summary": faculty[5],
            },
            "source_records": [
                {"source": row[0], "active": row[1], "inactive": row[2], "stale": row[3]} for row in source_records
            ],
            "author_resolutions": [{"source": row[0], "status": row[1], "count": row[2]} for row in resolutions],
            "documents": [
                {"type": row[0], "active": row[1], "inactive": row[2], "missing_url": row[3]} for row in document_rows
            ],
            "active_index": None
            if index_state is None
            else {
                "embedding_version": index_state[0],
                "activated_at": index_state[1],
                "corpus_snapshot_id": index_state[2],
                "missing_or_stale_embeddings": missing_embeddings,
            },
            "recent_runs": [
                {
                    "source": row[0],
                    "status": row[1],
                    "membership_complete": row[2],
                    "records_seen": row[3],
                    "errors": row[4],
                    "started_at": row[5],
                    "finished_at": row[6],
                }
                for row in recent_runs
            ],
        }

    def missing_embedding_inputs(self, embedding_version: str) -> Sequence[EmbeddingInput]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT d.document_id, d.content_hash, d.title, d.text
                FROM research_documents d
                JOIN faculty f ON f.faculty_id = d.faculty_id
                LEFT JOIN document_embeddings e
                  ON e.document_id = d.document_id AND e.embedding_version = %s
                WHERE d.is_active AND f.is_active AND f.eligibility_status = 'eligible'
                  AND (e.document_id IS NULL OR e.document_content_hash <> d.content_hash)
                ORDER BY d.document_id
                """,
                (embedding_version,),
            ).fetchall()
        result: list[EmbeddingInput] = []
        for document_id, content_hash, title, text in rows:
            formatted = f"Title: {title}\nText: {text}"
            result.append(EmbeddingInput(document_id, content_hash, formatted, embedding_input_hash(formatted)))
        return result

    def upsert_embedding(
        self,
        document_id: UUID,
        embedding_version: str,
        vector: Sequence[float],
        document_content_hash: str,
        embedding_input_hash_value: str,
    ) -> None:
        if len(vector) != 768:
            raise ValueError("pgvector column requires exactly 768 dimensions")
        normalized_vector = l2_normalize(vector)
        vector_literal = "[" + ",".join(f"{value:.9g}" for value in normalized_vector) + "]"
        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO document_embeddings (
                    document_id, embedding_version, embedding, document_content_hash,
                    embedding_input_hash
                ) VALUES (%s, %s, %s::vector, %s, %s)
                ON CONFLICT (document_id, embedding_version) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    document_content_hash = EXCLUDED.document_content_hash,
                    embedding_input_hash = EXCLUDED.embedding_input_hash,
                    created_at = now()
                """,
                (
                    document_id,
                    embedding_version,
                    vector_literal,
                    document_content_hash,
                    embedding_input_hash_value,
                ),
            )

    def mark_embedding_truncated(self, document_id: UUID, truncated: bool) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                """
                UPDATE research_documents
                SET metadata = jsonb_set(metadata, '{embedding_truncated}', %s::jsonb, true)
                WHERE document_id = %s
                """,
                (_json(truncated), document_id),
            )

    def activate_embedding_version(self, embedding_version: str, corpus_snapshot_id: UUID) -> None:
        with self.pool.connection() as connection, connection.transaction():
            missing = connection.execute(
                """
                SELECT count(*)
                FROM research_documents d
                JOIN faculty f ON f.faculty_id = d.faculty_id
                LEFT JOIN document_embeddings e
                  ON e.document_id = d.document_id AND e.embedding_version = %s
                 AND e.document_content_hash = d.content_hash
                WHERE d.is_active AND f.is_active AND f.eligibility_status = 'eligible'
                  AND e.document_id IS NULL
                """,
                (embedding_version,),
            ).fetchone()[0]
            if missing:
                raise IncompleteEmbeddingIndexError(
                    f"cannot activate {embedding_version!r}: {missing} active documents are missing/stale"
                )
            exists = connection.execute(
                "SELECT 1 FROM corpus_snapshots WHERE corpus_snapshot_id = %s",
                (corpus_snapshot_id,),
            ).fetchone()
            if not exists:
                raise ValueError("unknown corpus snapshot")
            connection.execute(
                """
                INSERT INTO search_index_state (
                    singleton, embedding_version, activated_at, corpus_snapshot_id
                ) VALUES (true, %s, %s, %s)
                ON CONFLICT (singleton) DO UPDATE SET
                    embedding_version = EXCLUDED.embedding_version,
                    activated_at = EXCLUDED.activated_at,
                    corpus_snapshot_id = EXCLUDED.corpus_snapshot_id
                """,
                (embedding_version, datetime.now(UTC), corpus_snapshot_id),
            )

    def resolve_redirect(self, faculty_id: UUID) -> UUID:
        seen: set[UUID] = set()
        current = faculty_id
        with self.pool.connection() as connection:
            while current not in seen:
                seen.add(current)
                row = connection.execute(
                    "SELECT to_faculty_id FROM faculty_redirects WHERE from_faculty_id = %s",
                    (current,),
                ).fetchone()
                if row is None:
                    return current
                current = row[0]
        raise FacultyMergeConflict("faculty redirect cycle detected")

    def merge_faculty(self, from_faculty_id: UUID, to_faculty_id: UUID, reason: str) -> None:
        if from_faculty_id == to_faculty_id:
            raise ValueError("cannot merge a faculty identity into itself")
        with self.pool.connection() as connection, connection.transaction():
            links = connection.execute(
                """
                SELECT faculty_id, source_name, external_author_id, resolution_status
                FROM scholarly_author_links
                WHERE faculty_id IN (%s, %s) AND resolution_status = 'resolved'
                """,
                (from_faculty_id, to_faculty_id),
            ).fetchall()
            resolved: dict[str, set[str]] = {}
            for _owner, source_name, author_id, _status in links:
                resolved.setdefault(source_name, set()).add(author_id)
            conflicts = {provider: ids for provider, ids in resolved.items() if len(ids) > 1}
            if conflicts:
                raise FacultyMergeConflict(f"conflicting scholarly identities: {conflicts}")
            documents = connection.execute(
                """
                SELECT document_id, document_type, external_id, chunk_key, metadata
                FROM research_documents WHERE faculty_id = %s AND is_active FOR UPDATE
                """,
                (from_faculty_id,),
            ).fetchall()
            for document_id, document_type, external_id, chunk_key, metadata in documents:
                collision = connection.execute(
                    """
                    SELECT document_id, metadata FROM research_documents
                    WHERE faculty_id = %s AND document_type = %s AND external_id = %s
                      AND chunk_key = %s AND is_active FOR UPDATE
                    """,
                    (to_faculty_id, document_type, external_id, chunk_key),
                ).fetchone()
                if collision:
                    survivor_id, survivor_metadata = collision
                    merged_ids = set(survivor_metadata.get("merged_document_ids", []))
                    merged_ids.add(str(document_id))
                    aliases = set(survivor_metadata.get("identity_aliases", []))
                    aliases.update(metadata.get("identity_aliases", []))
                    survivor_metadata["merged_document_ids"] = sorted(merged_ids)
                    survivor_metadata["identity_aliases"] = sorted(aliases)
                    connection.execute(
                        "UPDATE research_documents SET metadata = %s::jsonb WHERE document_id = %s",
                        (_json(survivor_metadata), survivor_id),
                    )
                    connection.execute(
                        "UPDATE research_documents SET is_active = false WHERE document_id = %s",
                        (document_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE research_documents SET faculty_id = %s WHERE document_id = %s",
                        (to_faculty_id, document_id),
                    )
            connection.execute(
                "UPDATE faculty_source_records SET faculty_id = %s WHERE faculty_id = %s",
                (to_faculty_id, from_faculty_id),
            )
            connection.execute(
                "UPDATE faculty_affiliations SET is_active = false, is_primary = false WHERE faculty_id = %s",
                (from_faculty_id,),
            )
            connection.execute(
                "UPDATE faculty_affiliations SET faculty_id = %s WHERE faculty_id = %s",
                (to_faculty_id, from_faculty_id),
            )
            from_links = connection.execute(
                "SELECT author_link_id, source_name FROM scholarly_author_links WHERE faculty_id = %s",
                (from_faculty_id,),
            ).fetchall()
            for link_id, provider in from_links:
                target = connection.execute(
                    "SELECT author_link_id FROM scholarly_author_links WHERE faculty_id = %s AND source_name = %s",
                    (to_faculty_id, provider),
                ).fetchone()
                if target:
                    connection.execute("DELETE FROM scholarly_author_links WHERE author_link_id = %s", (link_id,))
                else:
                    connection.execute(
                        "UPDATE scholarly_author_links SET faculty_id = %s WHERE author_link_id = %s",
                        (to_faculty_id, link_id),
                    )
            connection.execute("UPDATE faculty SET is_active = false WHERE faculty_id = %s", (from_faculty_id,))
            connection.execute(
                """
                INSERT INTO faculty_redirects (from_faculty_id, to_faculty_id, reason)
                VALUES (%s, %s, %s)
                """,
                (from_faculty_id, to_faculty_id, reason),
            )
            self._rebuild_faculty(connection, to_faculty_id)
