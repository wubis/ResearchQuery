"""PostgreSQL connection lifecycle and explicit migration application."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 4) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - installation boundary
            raise RuntimeError("install psycopg[pool] to use PostgreSQL persistence") from exc
        self.pool: Any = ConnectionPool(database_url, min_size=min_size, max_size=max_size, open=True)

    def close(self) -> None:
        self.pool.close()

    def apply_migrations(self, migrations_dir: Path) -> None:
        paths = sorted(migrations_dir.glob("*.sql"))
        if not paths:
            raise ValueError(f"no SQL migrations found in {migrations_dir}")
        with self.pool.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            connection.commit()
            for path in paths:
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                row = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = %s",
                    (path.name,),
                ).fetchone()
                if row:
                    if row[0] != checksum:
                        raise RuntimeError(f"applied migration {path.name} has been modified")
                    connection.rollback()
                    continue
                connection.rollback()
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )
                connection.commit()
