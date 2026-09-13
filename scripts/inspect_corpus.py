#!/usr/bin/env python3
"""Print operational corpus coverage and recent ingestion diagnostics."""

from __future__ import annotations

import json

from research_query.config import Settings
from research_query.data.database import Database
from research_query.data.repositories import PostgresCorpusRepository


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        report = PostgresCorpusRepository(database.pool).inspect_corpus()
        print(json.dumps(report, indent=2, default=str))
    finally:
        database.close()


if __name__ == "__main__":
    main()
