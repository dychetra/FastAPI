"""Database layer: Neon/Postgres online, SQLite on a single PC.

* If the environment variable DATABASE_URL is set (e.g. the connection string
  Neon gives you: postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require)
  the app uses Postgres.
* Otherwise it uses a local SQLite file (GROCERY_DB, default grocery.db).

All SQL in the app is written once, with `?` placeholders; this module
translates it for Postgres. A new connection is opened per request and closed
straight after, so Neon can go to sleep when the shop is quiet (saves free
compute hours).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

BASE_DIR = Path(__file__).resolve().parent


def database_url() -> str:
    """The connection string, cleaned of common copy-paste extras.

    Neon's "Connect" box can copy it as  psql 'postgresql://...'  and people
    often paste it with quotes or spaces; keep only the postgresql://... part.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    for prefix in ("postgresql://", "postgres://"):
        i = url.find(prefix)
        if i > 0:
            url = url[i:]
            break
    return url.strip().strip("'\"").strip()


def is_postgres() -> bool:
    return database_url().startswith(("postgres://", "postgresql://"))


def backend_name() -> str:
    return "postgres" if is_postgres() else "sqlite"


class Conn:
    """Tiny wrapper giving SQLite and Postgres the same interface."""

    def __init__(self, raw: Any, pg: bool):
        self.raw = raw
        self.pg = pg

    def execute(self, sql: str, args: Sequence[Any] = ()):
        if self.pg:
            sql = sql.replace("?", "%s")
            cur = self.raw.cursor()
            cur.execute(sql, args)
            return cur
        return self.raw.execute(sql, args)


@contextmanager
def connect() -> Iterator[Conn]:
    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=15)
        pg = True
    else:
        raw = sqlite3.connect(os.environ.get("GROCERY_DB", BASE_DIR / "grocery.db"))
        raw.row_factory = sqlite3.Row
        pg = False
    try:
        yield Conn(raw, pg)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def schema() -> list[str]:
    pk = "BIGSERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    num = "DOUBLE PRECISION" if is_postgres() else "REAL"
    return [
        f"""CREATE TABLE IF NOT EXISTS items (
            id          {pk},
            barcode     TEXT    NOT NULL UNIQUE,
            name        TEXT    NOT NULL,
            category    TEXT    NOT NULL DEFAULT '',
            price       {num}   NOT NULL,
            currency    TEXT    NOT NULL CHECK (currency IN ('USD','KHR')),
            cost        {num},
            stock       INTEGER,
            updated_at  TEXT    NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS price_history (
            id          {pk},
            barcode     TEXT    NOT NULL,
            price       {num}   NOT NULL,
            currency    TEXT    NOT NULL,
            changed_at  TEXT    NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_history_barcode ON price_history(barcode)",
        """CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
    ]
