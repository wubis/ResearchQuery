#!/usr/bin/env python3
"""Run one serialized Whiting corpus refresh."""

from __future__ import annotations

import json
from dataclasses import asdict

from research_query.config import Settings
from research_query.data.database import Database
from research_query.data.repositories import PostgresCorpusRepository
from research_query.embeddings.model import PinnedTokenizerCounter
from research_query.ingestion.faculty.whiting import WhitingFacultySource
from research_query.ingestion.http import CachingHttpTransport, RetryingHttpTransport
from research_query.ingestion.pipeline import IngestionPipeline
from research_query.ingestion.publications.semantic_scholar import SemanticScholarPublicationSource


def main() -> None:
    settings = Settings.from_env()
    transport = CachingHttpTransport(
        RetryingHttpTransport(
            user_agent=settings.http_user_agent,
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
        )
    )
    tokenizer = PinnedTokenizerCounter(
        settings.embedding_model,
        settings.embedding_model_revision,
    )
    database = Database(settings.database_url)
    try:
        repository = PostgresCorpusRepository(database.pool, token_counter=tokenizer)
        faculty_source = WhitingFacultySource(settings.whiting_directory_url, transport)
        publication_source = SemanticScholarPublicationSource(
            transport,
            api_key=settings.semantic_scholar_api_key,
        )
        report = IngestionPipeline(repository, faculty_source, publication_source, settings).run()
        print(json.dumps(asdict(report), indent=2, default=str))
    finally:
        database.close()


if __name__ == "__main__":
    main()
