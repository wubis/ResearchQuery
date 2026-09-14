#!/usr/bin/env python3
"""Run one serialized Whiting corpus refresh."""

from __future__ import annotations

import argparse
import json
from collections import Counter
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--faculty-only", action="store_true", help="ingest Whiting without Semantic Scholar enrichment"
    )
    parser.add_argument("--summary", action="store_true", help="print aggregate counts instead of per-faculty traces")
    args = parser.parse_args()
    settings = Settings.from_env()
    whiting_transport = CachingHttpTransport(
        RetryingHttpTransport(
            user_agent=settings.http_user_agent,
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            min_interval_seconds=max(5.0, settings.whiting_min_interval_seconds),
        )
    )
    scholarly_transport = CachingHttpTransport(
        RetryingHttpTransport(
            user_agent=settings.http_user_agent,
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            min_interval_seconds=max(1.1, settings.semantic_scholar_min_interval_seconds),
        )
    )
    tokenizer = PinnedTokenizerCounter(
        settings.embedding_model,
        settings.embedding_model_revision,
    )
    database = Database(settings.database_url)
    try:
        repository = PostgresCorpusRepository(database.pool, token_counter=tokenizer)
        faculty_source = WhitingFacultySource(settings.whiting_directory_url, whiting_transport, use_people_feed=True)
        publication_source = None if args.faculty_only else SemanticScholarPublicationSource(
            scholarly_transport, api_key=settings.semantic_scholar_api_key
        )
        report = IngestionPipeline(repository, faculty_source, publication_source, settings).run()
        if args.summary:
            print(
                json.dumps(
                    {
                        "run_id": report.run_id,
                        "snapshot_id": report.snapshot_id,
                        "membership_complete": report.membership_complete,
                        "status": report.status,
                        "faculty_seen": report.faculty_seen,
                        "source_records_created": report.source_records_created,
                        "source_records_deactivated": report.source_records_deactivated,
                        "documents_changed": report.documents_changed,
                        "author_status_counts": Counter(trace.author_status for trace in report.traces),
                        "error_count": len(report.errors),
                        "errors": report.errors[:20],
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(json.dumps(asdict(report), indent=2, default=str))
    finally:
        database.close()


if __name__ == "__main__":
    main()
