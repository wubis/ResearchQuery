from pathlib import Path


def test_phase1_migration_contains_required_tables_constraints_and_pgvector() -> None:
    sql = (Path(__file__).parents[1] / "migrations" / "0001_phase1_corpus.sql").read_text()
    for table in (
        "faculty",
        "faculty_source_records",
        "faculty_affiliations",
        "scholarly_author_links",
        "faculty_redirects",
        "ingestion_runs",
        "research_documents",
        "document_embeddings",
        "search_index_state",
        "corpus_snapshots",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "embedding vector(768)" in sql
    assert "WHERE is_active AND is_primary" in sql
    assert "WHERE resolution_status = 'resolved'" in sql
    assert "tsvector GENERATED ALWAYS" in sql
