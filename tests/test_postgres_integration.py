from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from research_query.data.models import (
    AuthorResolution,
    FacultySourceSnapshot,
    RawAffiliation,
    RawFacultyRecord,
    ResolutionStatus,
)


@pytest.mark.postgres
def test_migrations_idempotence_and_snapshot_safety_against_pgvector() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL+pgvector database")
    psycopg = pytest.importorskip("psycopg")
    schema = f"rq_test_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as bootstrap:
        bootstrap.execute(f'CREATE SCHEMA "{schema}"')
    try:
        conninfo = psycopg.conninfo.make_conninfo(database_url, options=f"-c search_path={schema},public")
        from research_query.data.database import Database
        from research_query.data.repositories import (
            FacultyMergeConflict,
            IncompleteEmbeddingIndexError,
            PostgresCorpusRepository,
        )

        database = Database(conninfo)
        database.apply_migrations(Path(__file__).parents[1] / "migrations")
        database.apply_migrations(Path(__file__).parents[1] / "migrations")
        repository = PostgresCorpusRepository(database.pool)
        now = datetime(2026, 9, 13, tzinfo=UTC)
        record = RawFacultyRecord(
            "whiting",
            "key-1",
            "https://engineering.jhu.edu/faculty",
            "Jane Doe",
            "Professor",
            (RawAffiliation("Whiting School of Engineering", "Materials Science"),),
            "https://engineering.jhu.edu/faculty/jane-doe",
            research_summary="Machine learning for materials.",
            source_role_category="Faculty",
        )
        complete = FacultySourceSnapshot("whiting", "one", now, (record,), True)
        first = repository.apply_faculty_snapshot(complete)
        second = repository.apply_faculty_snapshot(complete)
        assert first.applied[0].faculty_id == second.applied[0].faculty_id
        assert second.created_source_records == 0
        partial_empty = FacultySourceSnapshot("whiting", "partial", now, (), False, ("failed page",))
        repository.apply_faculty_snapshot(partial_empty)
        with database.pool.connection() as connection:
            state = connection.execute("SELECT is_active, missing_run_count FROM faculty_source_records").fetchone()
            assert state == (True, 0)
        repository.apply_faculty_snapshot(FacultySourceSnapshot("whiting", "miss-1", now, (), True))
        with database.pool.connection() as connection:
            state = connection.execute("SELECT is_active, missing_run_count FROM faculty_source_records").fetchone()
            assert state == (True, 1)
        repository.apply_faculty_snapshot(FacultySourceSnapshot("whiting", "miss-2", now, (), True))
        with database.pool.connection() as connection:
            assert connection.execute("SELECT is_active FROM faculty").fetchone()[0] is False
            assert connection.execute("SELECT is_active FROM research_documents").fetchone()[0] is False
        reappeared = repository.apply_faculty_snapshot(complete)
        assert reappeared.applied[0].faculty_id == first.applied[0].faculty_id
        with database.pool.connection() as connection:
            assert connection.execute("SELECT is_active FROM faculty").fetchone()[0] is True
            assert connection.execute("SELECT count(*) FROM research_documents").fetchone()[0] == 1

        second_record = RawFacultyRecord(
            "whiting",
            "key-2",
            "https://engineering.jhu.edu/faculty",
            "John Roe",
            "Professor",
            (RawAffiliation("Whiting School of Engineering", "Computer Science"),),
            "https://engineering.jhu.edu/faculty/john-roe",
            research_summary="Distributed systems and networks.",
            source_role_category="Faculty",
        )
        two_faculty = repository.apply_faculty_snapshot(
            FacultySourceSnapshot("whiting", "two", now, (record, second_record), True)
        )
        second_faculty_id = next(
            item.faculty_id for item in two_faculty.applied if item.normalized.source.source_faculty_key == "key-2"
        )
        first_faculty_id = first.applied[0].faculty_id
        repository.save_author_resolution(
            first_faculty_id,
            "semantic_scholar",
            AuthorResolution(ResolutionStatus.RESOLVED, "author-1", 0.9, "manual", {}),
        )
        repository.save_author_resolution(
            second_faculty_id,
            "semantic_scholar",
            AuthorResolution(ResolutionStatus.RESOLVED, "author-2", 0.9, "manual", {}),
        )
        with pytest.raises(FacultyMergeConflict):
            repository.merge_faculty(first_faculty_id, second_faculty_id, "must not merge")

        run_id = repository.start_run("whiting", {})
        repository.finish_run(run_id, complete, status="complete")
        snapshot_id = repository.create_corpus_snapshot({"whiting": run_id})
        with pytest.raises(IncompleteEmbeddingIndexError):
            repository.activate_embedding_version("test-v1", snapshot_id)
        with database.pool.connection() as connection:
            document_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT document_id FROM research_documents WHERE is_active ORDER BY faculty_id"
                ).fetchall()
            ]
            fts_count = connection.execute(
                """
                SELECT count(*) FROM research_documents
                WHERE search_vector @@ plainto_tsquery('english', 'machine materials')
                """
            ).fetchone()[0]
            assert fts_count == 1
        first_vector = [1.0, *([0.0] * 767)]
        second_vector = [0.0, 1.0, *([0.0] * 766)]
        repository.upsert_embedding(document_ids[0], "test-v1", first_vector, "", "input-1")
        repository.upsert_embedding(document_ids[1], "test-v1", second_vector, "", "input-2")
        with pytest.raises(IncompleteEmbeddingIndexError):
            repository.activate_embedding_version("test-v1", snapshot_id)
        with database.pool.connection() as connection:
            hashes = connection.execute(
                "SELECT document_id, content_hash FROM research_documents WHERE is_active"
            ).fetchall()
        for document_id, content_hash in hashes:
            vector = first_vector if document_id == document_ids[0] else second_vector
            repository.upsert_embedding(document_id, "test-v1", vector, content_hash, "input")
        repository.activate_embedding_version("test-v1", snapshot_id)
        query_vector = "[" + ",".join(str(value) for value in first_vector) + "]"
        with database.pool.connection() as connection:
            ordered = [
                row[0]
                for row in connection.execute(
                    """
                SELECT document_id FROM document_embeddings
                WHERE embedding_version = 'test-v1'
                ORDER BY embedding <#> %s::vector, document_id
                """,
                    (query_vector,),
                ).fetchall()
            ]
            assert ordered[0] == document_ids[0]
            state = connection.execute("SELECT embedding_version FROM search_index_state").fetchone()
            assert state == ("test-v1",)
        coverage = repository.inspect_corpus()
        assert coverage["faculty"]["eligible_with_documents"] == 2
        assert coverage["faculty"]["eligible_without_documents"] == 0
        database.close()
    finally:
        with psycopg.connect(database_url, autocommit=True) as bootstrap:
            bootstrap.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
