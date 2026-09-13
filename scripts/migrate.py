#!/usr/bin/env python3
"""Apply checksum-protected committed PostgreSQL migrations."""

from pathlib import Path

from research_query.config import Settings
from research_query.data.database import Database


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        database.apply_migrations(Path(__file__).parents[1] / "migrations")
    finally:
        database.close()


if __name__ == "__main__":
    main()
