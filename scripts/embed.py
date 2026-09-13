#!/usr/bin/env python3
"""Incrementally build and atomically activate the configured embedding version."""

from __future__ import annotations

import json
from dataclasses import asdict

from research_query.config import Settings
from research_query.data.database import Database
from research_query.data.repositories import PostgresCorpusRepository
from research_query.embeddings.model import EmbeddingIndexer, LocalEmbeddingModel


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        repository = PostgresCorpusRepository(database.pool)
        source_runs = repository.latest_complete_source_runs(settings.enabled_faculty_sources)
        corpus_snapshot_id = repository.create_corpus_snapshot(
            source_runs, description="Embedding activation candidate"
        )
        model = LocalEmbeddingModel(
            settings.embedding_model,
            settings.embedding_model_revision,
        )
        if model.version != settings.embedding_version:
            raise RuntimeError("configured embedding version does not match the loaded model/formatter")
        result = EmbeddingIndexer(repository, model, batch_size=settings.embedding_batch_size).build_and_activate(
            settings.embedding_version, corpus_snapshot_id
        )
        print(json.dumps(asdict(result), indent=2, default=str))
    finally:
        database.close()


if __name__ == "__main__":
    main()
