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
        # one row per finished sale; amounts in KHR at the sale's own rate
        f"""CREATE TABLE IF NOT EXISTS sales (
            id          {pk},
            created_at  TEXT    NOT NULL,
            day         TEXT    NOT NULL,
            rate        {num}   NOT NULL,
            items_count INTEGER NOT NULL,
            total_khr   {num}   NOT NULL,
            total_usd   {num}   NOT NULL,
            cost_khr    {num},
            paid_usd    {num}   NOT NULL DEFAULT 0,
            paid_khr    {num}   NOT NULL DEFAULT 0,
            change_khr  {num}   NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sales_day ON sales(day)",
        f"""CREATE TABLE IF NOT EXISTS sale_items (
            id          {pk},
            sale_id     INTEGER NOT NULL,
            barcode     TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            qty         INTEGER NOT NULL,
            price       {num}   NOT NULL,
            currency    TEXT    NOT NULL,
            line_khr    {num}   NOT NULL,
            cost        {num}
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sale_items_sale ON sale_items(sale_id)",
        # extra ways to sell an item (pack, case, wholesale...): `qty` pieces for
        # `price` in the item's currency; a pack/case may have its own barcode
        f"""CREATE TABLE IF NOT EXISTS item_units (
            id            {pk},
            item_barcode  TEXT    NOT NULL,
            unit          TEXT    NOT NULL,
            qty           INTEGER NOT NULL,
            price         {num}   NOT NULL,
            unit_barcode  TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_item_units_item ON item_units(item_barcode)",
        "CREATE INDEX IF NOT EXISTS ix_item_units_barcode ON item_units(unit_barcode)",
        # product photo: either our own small JPEG (data) or a link to an online
        # photo (url, e.g. Open Food Facts) which uses no space here
        f"""CREATE TABLE IF NOT EXISTS item_photos (
            barcode     TEXT PRIMARY KEY,
            url         TEXT,
            mime        TEXT,
            data        {"BYTEA" if is_postgres() else "BLOB"},
            updated_at  TEXT NOT NULL
        )""",
    ]


# Columns added after the first release: (table, column, type)
LATER_COLUMNS = [
    ("sale_items", "unit", "TEXT"),       # selling type, '' = single piece
    ("sale_items", "pieces", "INTEGER"),  # pieces per unit sold (case = 24)
    ("items", "sku", "TEXT"),             # shop's own product code, e.g. DRI-0001
]


def add_missing_columns(conn: "Conn") -> None:
    """Upgrade an existing database in place (safe to run every start)."""
    for table, col, typ in LATER_COLUMNS:
        if conn.pg:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
        else:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # SKUs are unique when set (case-insensitive)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_items_sku ON items (upper(sku)) "
                 "WHERE sku IS NOT NULL")
