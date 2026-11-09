"""ScanPrice · ស្កេនតម្លៃ — grocery barcode price tracker.

A small FastAPI app for a family grocery shop:
  * scan a barcode (phone camera or USB scanner) to look up an item's price
  * save every item with its selling price in USD or KHR (riel)
  * see the price in both currencies using the shop's own exchange rate
  * keep a history of every price change, and export everything to CSV

Run:  uvicorn main:app --reload      (from inside this folder)
Data is stored in Neon/Postgres when DATABASE_URL is set, otherwise in a
local SQLite file (grocery.db). Set APP_PASSWORD to require a login.
"""

from __future__ import annotations

import asyncio
import base64
import re
import csv
import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import httpx

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import db as database
from db import BASE_DIR

DEFAULT_RATE = 4100.0  # KHR per 1 USD; change it in the app's settings

Currency = Literal["USD", "KHR"]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
db = database.connect


def init_db() -> None:
    with db() as conn:
        for stmt in database.schema():
            conn.execute(stmt)
        database.add_missing_columns(conn)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('khr_per_usd', ?) ON CONFLICT DO NOTHING",
            (str(DEFAULT_RATE),),
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_rate(conn) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key='khr_per_usd'").fetchone()
    return float(row["value"]) if row else DEFAULT_RATE


# --------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------
def round_khr(amount: float) -> int:
    """Riel prices are quoted to the nearest 100 KHR (smallest note in use)."""
    return int(round(amount / 100.0)) * 100


def to_both(price: float, currency: str, rate: float) -> tuple[float, int]:
    """Return (price_usd, price_khr) for a price given in either currency."""
    if currency == "USD":
        return round(price, 2), round_khr(price * rate)
    return round(price / rate, 2), round_khr(price)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class UnitIn(BaseModel):
    """Another way to sell the item, e.g. កញ្ជប់ (pack) = 6 pieces for $4."""
    unit: str = Field(min_length=1, max_length=30)
    qty: int = Field(ge=1, le=100000, description="pieces in this unit")
    price: float = Field(gt=0, description="price for the whole unit, item currency")
    barcode: Optional[str] = Field(default=None, max_length=64)

    @field_validator("unit")
    @classmethod
    def tidy_unit(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("barcode")
    @classmethod
    def tidy_barcode(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


class UnitOut(UnitIn):
    price_usd: float
    price_khr: int
    per_piece_usd: float


class ItemIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=100)
    price: float = Field(gt=0, description="Selling price, in `currency`")
    currency: Currency = "USD"
    cost: Optional[float] = Field(default=None, ge=0, description="Buying cost, same currency")
    stock: Optional[int] = Field(default=None, ge=0, description="in single pieces")
    # None = leave the selling types unchanged; [] = remove them all
    units: Optional[list[UnitIn]] = Field(default=None, max_length=10)

    @field_validator("barcode", "name", "category")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


# items joined with their photo (if any); use instead of "SELECT * FROM items"
ITEM_SELECT = (
    "SELECT items.*, p.url AS photo_link, p.updated_at AS photo_at, "
    "CASE WHEN p.data IS NULL THEN 0 ELSE 1 END AS photo_own "
    "FROM items LEFT JOIN item_photos p ON p.barcode = items.barcode"
)


class ItemOut(ItemIn):
    id: int
    price_usd: float
    price_khr: int
    margin_pct: Optional[float] = None
    updated_at: str
    photo_url: Optional[str] = None   # own photo (served here) or online photo link
    units: list[UnitOut] = []         # type: ignore[assignment]
    scanned_unit: Optional[str] = None  # set when a pack/case barcode was scanned


class HistoryOut(BaseModel):
    price: float
    currency: Currency
    changed_at: str


class RateIn(BaseModel):
    khr_per_usd: float = Field(gt=0, lt=100000)


class Summary(BaseModel):
    items: int
    khr_per_usd: float
    stock_value_usd: float
    categories: list[str]


def load_units(conn, barcodes: Optional[list[str]] = None) -> dict[str, list]:
    """Selling types per item barcode (all items if barcodes is None)."""
    sql, args = "SELECT * FROM item_units", []
    if barcodes is not None:
        if not barcodes:
            return {}
        sql += f" WHERE item_barcode IN ({','.join('?' * len(barcodes))})"
        args = barcodes
    out: dict[str, list] = {}
    for r in conn.execute(sql + " ORDER BY qty, id", args).fetchall():
        out.setdefault(r["item_barcode"], []).append(r)
    return out


def unit_out(u, currency: str, rate: float) -> UnitOut:
    usd, khr = to_both(u["price"], currency, rate)
    return UnitOut(unit=u["unit"], qty=u["qty"], price=u["price"], barcode=u["unit_barcode"],
                   price_usd=usd, price_khr=khr, per_piece_usd=round(usd / u["qty"], 3))


def to_out(row, rate: float, units: Optional[list] = None, scanned_unit: Optional[str] = None) -> ItemOut:
    usd, khr = to_both(row["price"], row["currency"], rate)
    margin = None
    if row["cost"] is not None and row["price"] > 0:
        margin = round((row["price"] - row["cost"]) / row["price"] * 100, 1)
    return ItemOut(
        id=row["id"],
        barcode=row["barcode"],
        name=row["name"],
        category=row["category"],
        price=row["price"],
        currency=row["currency"],
        cost=row["cost"],
        stock=row["stock"],
        price_usd=usd,
        price_khr=khr,
        margin_pct=margin,
        updated_at=row["updated_at"],
        photo_url=photo_url_of(row),
        units=[unit_out(u, row["currency"], rate) for u in units or []],
        scanned_unit=scanned_unit,
    )


def _col(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def photo_url_of(row) -> Optional[str]:
    if _col(row, "photo_own"):
        stamp = re.sub(r"\D", "", _col(row, "photo_at") or "")
        return f"/api/items/{row['barcode']}/photo?v={stamp}"
    return _col(row, "photo_link")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
APP_VERSION = "2.0.0"
app = FastAPI(title="ScanPrice", version=APP_VERSION)
# Start even if the database is briefly unreachable (e.g. Neon waking up or a
# network blip): a crash here would take the whole app offline (Render 502).
_db_state = {"ready": False, "error": ""}


def ensure_db() -> bool:
    """Create tables once; remember the last error instead of crashing."""
    if _db_state["ready"]:
        return True
    try:
        init_db()
        _db_state.update(ready=True, error="")
    except Exception as exc:  # noqa: BLE001 - report any driver error
        msg = re.sub(r"postgres(ql)?://\S+", "postgresql://***", str(exc)).strip()
        _db_state["error"] = f"{type(exc).__name__}: {msg.splitlines()[0] if msg else ''}"[:300]
    return _db_state["ready"]


ensure_db()

# Open without login: health check, plus the app name/icons so phones can show
# them on the home screen (they fetch these without the password).
PUBLIC_PATHS = {
    "/healthz",
    "/static/manifest.webmanifest",
    "/static/icon.svg",
    "/static/icon-192.png",
    "/static/icon-512.png",
    "/static/apple-touch-icon.png",
}


@app.middleware("http")
async def require_password(request: Request, call_next):
    """If APP_PASSWORD is set, every page and API call needs it (browser login box).

    The username can be anything (e.g. "shop"); only the password is checked.
    """
    password = os.environ.get("APP_PASSWORD", "")
    if password and request.url.path not in PUBLIC_PATHS:
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(given.encode(), password.encode())
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("Login required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="ScanPrice"'})
    if request.url.path.startswith("/api/") and not _db_state["ready"]:
        if not await run_in_threadpool(ensure_db):
            return JSONResponse({"detail": "Database not reachable: " + _db_state["error"]},
                                status_code=503)
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    db_ok = ensure_db()
    info = {"ok": db_ok, "version": APP_VERSION, "database": database.backend_name()}
    if not db_ok:
        info["database_error"] = _db_state["error"]
    if info["database"] == "sqlite":
        raw = os.environ.get("DATABASE_URL", "").strip()
        info["hint"] = (
            "DATABASE_URL is not set in Render > Environment" if not raw else
            "DATABASE_URL is set but does not contain postgresql:// - copy it again from Neon"
        )
    return info


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # no-cache: phones always check for the newest version of the app
    return FileResponse(BASE_DIR / "static" / "index.html",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/api/items", response_model=list[ItemOut])
def list_items(
    q: str = Query(default="", description="Search name, barcode or category"),
    category: str = "",
    limit: int = Query(default=500, le=5000),
):
    sql = ITEM_SELECT + " WHERE 1=1"
    args: list = []
    if q:
        sql += (" AND (lower(items.name) LIKE ? OR items.barcode LIKE ?"
                " OR lower(items.category) LIKE ?)")
        like = f"%{q.strip().lower()}%"
        args += [like, like, like]
    if category:
        sql += " AND items.category = ?"
        args.append(category)
    sql += " ORDER BY lower(items.name) LIMIT ?"
    args.append(limit)
    with db() as conn:
        rate = get_rate(conn)
        rows = conn.execute(sql, args).fetchall()
        units = load_units(conn, [r["barcode"] for r in rows])
        return [to_out(r, rate, units.get(r["barcode"])) for r in rows]


@app.get("/api/items/{barcode}", response_model=ItemOut)
def get_item(barcode: str):
    """Look up by the item barcode, or by the barcode of one of its packs/cases."""
    barcode = barcode.strip()
    with db() as conn:
        return item_out(conn, barcode)


def item_out(conn, barcode: str) -> ItemOut:
    scanned = None
    row = conn.execute(ITEM_SELECT + " WHERE items.barcode=?", (barcode,)).fetchone()
    if not row:
        u = conn.execute("SELECT item_barcode, unit FROM item_units WHERE unit_barcode=?",
                         (barcode,)).fetchone()
        if u:
            scanned = u["unit"]
            row = conn.execute(ITEM_SELECT + " WHERE items.barcode=?",
                               (u["item_barcode"],)).fetchone()
    if not row:
        raise HTTPException(404, "Item not found")
    units = load_units(conn, [row["barcode"]]).get(row["barcode"])
    return to_out(row, get_rate(conn), units, scanned)


def save_units(conn, barcode: str, units: list[UnitIn]) -> None:
    names, codes = set(), set()
    for u in units:
        if u.unit.lower() in names:
            raise HTTPException(400, f"Selling type '{u.unit}' is listed twice")
        names.add(u.unit.lower())
        if u.barcode:
            if u.barcode == barcode or u.barcode in codes:
                raise HTTPException(400, f"Barcode {u.barcode} is used twice")
            clash = conn.execute(
                "SELECT barcode AS b FROM items WHERE barcode=? UNION ALL "
                "SELECT item_barcode AS b FROM item_units WHERE unit_barcode=? AND item_barcode<>?",
                (u.barcode, u.barcode, barcode)).fetchone()
            if clash:
                raise HTTPException(400, f"Barcode {u.barcode} already belongs to item {clash['b']}")
            codes.add(u.barcode)
    conn.execute("DELETE FROM item_units WHERE item_barcode=?", (barcode,))
    for u in units:
        conn.execute("INSERT INTO item_units(item_barcode,unit,qty,price,unit_barcode)"
                     " VALUES (?,?,?,?,?)", (barcode, u.unit, u.qty, u.price, u.barcode))


@app.put("/api/items/{barcode}", response_model=ItemOut)
def save_item(barcode: str, item: ItemIn):
    """Create the item, or update it if the barcode already exists.

    A price-history row is written whenever the price or currency changes.
    """
    barcode = barcode.strip()
    if item.barcode != barcode:
        raise HTTPException(400, "Barcode in URL and body must match")
    ts = now()
    with db() as conn:
        if conn.execute("SELECT item_barcode FROM item_units WHERE unit_barcode=?",
                        (barcode,)).fetchone():
            raise HTTPException(400, f"Barcode {barcode} is a pack/case of another item")
        old = conn.execute("SELECT * FROM items WHERE barcode=?", (barcode,)).fetchone()
        if old is None:
            conn.execute(
                "INSERT INTO items(barcode,name,category,price,currency,cost,stock,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (barcode, item.name, item.category, item.price, item.currency,
                 item.cost, item.stock, ts),
            )
            price_changed = True
        else:
            conn.execute(
                "UPDATE items SET name=?,category=?,price=?,currency=?,cost=?,stock=?,updated_at=?"
                " WHERE barcode=?",
                (item.name, item.category, item.price, item.currency, item.cost,
                 item.stock, ts, barcode),
            )
            price_changed = old["price"] != item.price or old["currency"] != item.currency
        if price_changed:
            conn.execute(
                "INSERT INTO price_history(barcode,price,currency,changed_at) VALUES (?,?,?,?)",
                (barcode, item.price, item.currency, ts),
            )
        if item.units is not None:
            save_units(conn, barcode, item.units)
        return item_out(conn, barcode)


@app.delete("/api/items/{barcode}", status_code=204)
def delete_item(barcode: str):
    with db() as conn:
        cur = conn.execute("DELETE FROM items WHERE barcode=?", (barcode.strip(),))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found")
        conn.execute("DELETE FROM price_history WHERE barcode=?", (barcode.strip(),))
        conn.execute("DELETE FROM item_photos WHERE barcode=?", (barcode.strip(),))
        conn.execute("DELETE FROM item_units WHERE item_barcode=?", (barcode.strip(),))


@app.get("/api/items/{barcode}/history", response_model=list[HistoryOut])
def price_history(barcode: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT price,currency,changed_at FROM price_history WHERE barcode=?"
            " ORDER BY changed_at DESC, id DESC",
            (barcode.strip(),),
        ).fetchall()
        return [HistoryOut(**dict(r)) for r in rows]


@app.get("/api/settings/rate", response_model=RateIn)
def read_rate():
    with db() as conn:
        return RateIn(khr_per_usd=get_rate(conn))


@app.put("/api/settings/rate", response_model=RateIn)
def update_rate(body: RateIn):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('khr_per_usd',?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(body.khr_per_usd),),
        )
    return body


DEFAULT_SHOP_NAME = "Gech Heng Wing Shop"


def get_shop_name(conn) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='shop_name'").fetchone()
    return row["value"] if row else DEFAULT_SHOP_NAME


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


class ShopIn(BaseModel):
    shop_name: str = Field(min_length=1, max_length=80)

    @field_validator("shop_name")
    @classmethod
    def strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Shop name cannot be empty")
        return v


@app.get("/api/settings/shop", response_model=ShopIn)
def read_shop():
    with db() as conn:
        return ShopIn(shop_name=get_shop_name(conn))


@app.put("/api/settings/shop", response_model=ShopIn)
def update_shop(body: ShopIn):
    with db() as conn:
        set_setting(conn, "shop_name", body.shop_name)
    return body


# --------------------------------------------------------------------------
# Categories (English name is stored on the item; Khmer is shown beside it)
# --------------------------------------------------------------------------
DEFAULT_CATEGORIES = [
    ("Drinks", "ភេសជ្ជៈ"), ("Water", "ទឹកសុទ្ធ"), ("Beer & Alcohol", "ស្រា បៀរ"),
    ("Coffee & Tea", "កាហ្វេ និង តែ"), ("Snacks", "នំចំណី"), ("Noodles", "មី"),
    ("Rice", "អង្ករ"), ("Cooking & Spices", "គ្រឿងទេស"), ("Canned food", "ម្ហូបកំប៉ុង"),
    ("Milk & Dairy", "ទឹកដោះគោ"), ("Bread & Bakery", "នំបុ័ង"), ("Frozen food", "អាហារកក"),
    ("Fruits & Vegetables", "ផ្លែឈើ និង បន្លែ"), ("Meat, Fish & Eggs", "សាច់ ត្រី ពង"),
    ("Personal care", "អនាម័យខ្លួន"), ("Household & Cleaning", "សម្ភារៈផ្ទះ"),
    ("Baby care", "ទារក"), ("Cigarettes", "បារី"), ("Phone cards", "កាតទូរស័ព្ទ"),
    ("Ice", "ទឹកកក"), ("Other", "ផ្សេងៗ"),
]


class Category(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    km: str = Field(default="", max_length=100)

    @field_validator("name", "km")
    @classmethod
    def tidy(cls, v: str) -> str:
        return " ".join(v.split())


class CategoryOut(Category):
    count: int = 0         # how many items use it


def get_categories(conn) -> list[Category]:
    row = conn.execute("SELECT value FROM settings WHERE key='categories'").fetchone()
    if row:
        try:
            return [Category(**c) for c in json.loads(row["value"])]
        except (ValueError, TypeError):
            pass
    return [Category(name=n, km=k) for n, k in DEFAULT_CATEGORIES]


@app.get("/api/categories", response_model=list[CategoryOut])
def list_categories():
    """The shop's category list, plus any other category still used by items."""
    with db() as conn:
        cats = get_categories(conn)
        counts = {r["category"]: r["n"] for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM items WHERE category <> '' GROUP BY category")}
    out = [CategoryOut(name=c.name, km=c.km, count=counts.pop(c.name, 0)) for c in cats]
    out += [CategoryOut(name=n, count=c) for n, c in sorted(counts.items(), key=lambda x: x[0].lower())]
    return out


@app.put("/api/categories", response_model=list[CategoryOut])
def save_categories(cats: list[Category]):
    """Replace the category list (add / remove / reorder). Items keep their category."""
    seen, clean = set(), []
    for c in cats:
        if c.name and c.name.lower() not in seen:
            seen.add(c.name.lower())
            clean.append(c.model_dump())
    if len(clean) > 200:
        raise HTTPException(400, "Too many categories")
    with db() as conn:
        if clean:
            set_setting(conn, "categories", json.dumps(clean, ensure_ascii=False))
        else:  # empty list = back to the default grocery list
            conn.execute("DELETE FROM settings WHERE key='categories'")
    return list_categories()


DEFAULT_UNIT_TYPES = [("ដុំ", "Wholesale"), ("កញ្ជប់", "Pack"), ("គេស", "Case"),
                      ("ឡូ", "Dozen"), ("ប្រអប់", "Box"), ("បាវ", "Sack")]


@app.get("/api/unit-types")
def unit_types():
    """Suggested selling-type names: the usual ones plus any the shop has used."""
    with db() as conn:
        used = [r["unit"] for r in conn.execute(
            "SELECT unit, COUNT(*) AS n FROM item_units GROUP BY unit ORDER BY n DESC").fetchall()]
    out = [{"name": n, "en": e} for n, e in DEFAULT_UNIT_TYPES]
    out += [{"name": u, "en": ""} for u in used if u not in {n for n, _ in DEFAULT_UNIT_TYPES}]
    return out


@app.get("/api/summary", response_model=Summary)
def summary():
    with db() as conn:
        rate = get_rate(conn)
        rows = conn.execute("SELECT * FROM items").fetchall()
        stock_value = 0.0
        for r in rows:
            if r["stock"]:
                usd, _ = to_both(r["cost"] if r["cost"] is not None else r["price"],
                                 r["currency"], rate)
                stock_value += usd * r["stock"]
        cats = sorted({r["category"] for r in rows if r["category"]}, key=str.lower)
        return Summary(items=len(rows), khr_per_usd=rate,
                       stock_value_usd=round(stock_value, 2), categories=cats)


@app.get("/api/export.csv")
def export_csv():
    with db() as conn:
        rate = get_rate(conn)
        units = load_units(conn)
        items = [to_out(r, rate, units.get(r["barcode"]))
                 for r in conn.execute(ITEM_SELECT + " ORDER BY lower(items.name)").fetchall()]
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel shows Khmer text correctly
    w = csv.writer(buf)
    w.writerow(["barcode", "name", "category", "price", "currency", "price_usd",
                "price_khr", "cost", "margin_pct", "stock", "selling_types", "updated_at"])
    for i in items:
        types = "; ".join(f"{u.unit} x{u.qty} = {u.price} {i.currency}"
                          + (f" [{u.barcode}]" if u.barcode else "") for u in i.units)
        w.writerow([i.barcode, i.name, i.category, i.price, i.currency, i.price_usd,
                    i.price_khr, i.cost, i.margin_pct, i.stock, types, i.updated_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=grocery_prices.csv"},
    )


# --------------------------------------------------------------------------
# Backup / restore (used by backup.py to copy everything to Google Drive)
# --------------------------------------------------------------------------
class HistoryRow(HistoryOut):
    barcode: str


class Backup(BaseModel):
    app: str = "grocery-price-tracker"
    version: int = 1
    created_at: str
    khr_per_usd: float
    shop_name: Optional[str] = None   # added in v1.8
    categories: Optional[list[dict]] = None  # added in v1.9
    items: list[ItemIn]
    history: list[HistoryRow]
    updated_at: dict[str, str] = {}
    # added in v1.6; older backup files have none and keep existing sales on restore
    sales: Optional[list[dict]] = None
    sale_items: Optional[list[dict]] = None


SALE_COLS = ["id", "created_at", "day", "rate", "items_count", "total_khr", "total_usd",
             "cost_khr", "paid_usd", "paid_khr", "change_khr"]
SALE_ITEM_COLS = ["sale_id", "barcode", "name", "qty", "price", "currency", "line_khr", "cost",
                  "unit", "pieces"]


@app.get("/api/backup", response_model=Backup)
def backup():
    """Everything in one JSON document: items, price history and the rate."""
    with db() as conn:
        items = conn.execute("SELECT * FROM items ORDER BY barcode").fetchall()
        all_units = load_units(conn)
        hist = conn.execute(
            "SELECT barcode,price,currency,changed_at FROM price_history ORDER BY id"
        ).fetchall()
        return Backup(
            created_at=now(),
            khr_per_usd=get_rate(conn),
            shop_name=get_shop_name(conn),
            categories=[c.model_dump() for c in get_categories(conn)],
            items=[ItemIn(**{k: r[k] for k in ItemIn.model_fields if k != "units"},
                          units=[UnitIn(unit=u["unit"], qty=u["qty"], price=u["price"],
                                        barcode=u["unit_barcode"])
                                 for u in all_units.get(r["barcode"], [])])
                   for r in items],
            history=[HistoryRow(**dict(r)) for r in hist],
            updated_at={r["barcode"]: r["updated_at"] for r in items},
            sales=[{k: r[k] for k in SALE_COLS}
                   for r in conn.execute("SELECT * FROM sales ORDER BY id").fetchall()],
            sale_items=[{k: r[k] for k in SALE_ITEM_COLS}
                        for r in conn.execute("SELECT * FROM sale_items ORDER BY id").fetchall()],
        )


@app.post("/api/restore")
def restore(data: Backup, confirm: bool = False):
    """Replace ALL data with a backup. Needs ?confirm=true as a safety catch."""
    if not confirm:
        raise HTTPException(400, "Restore replaces all data. Call again with ?confirm=true")
    ts = now()
    with db() as conn:  # one transaction: all or nothing
        conn.execute("DELETE FROM price_history")
        conn.execute("DELETE FROM item_units")
        conn.execute("DELETE FROM items")
        for i in data.items:
            conn.execute(
                "INSERT INTO items(barcode,name,category,price,currency,cost,stock,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (i.barcode, i.name, i.category, i.price, i.currency, i.cost, i.stock,
                 data.updated_at.get(i.barcode, ts)),
            )
        for i in data.items:
            if i.units:
                save_units(conn, i.barcode, i.units)
        for h in data.history:
            conn.execute(
                "INSERT INTO price_history(barcode,price,currency,changed_at) VALUES (?,?,?,?)",
                (h.barcode, h.price, h.currency, h.changed_at),
            )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('khr_per_usd',?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(data.khr_per_usd),),
        )
        if data.shop_name:
            set_setting(conn, "shop_name", data.shop_name)
        if data.categories is not None:
            set_setting(conn, "categories", json.dumps(data.categories, ensure_ascii=False))
        if data.sales is not None:
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            new_id = {}
            for r in data.sales:  # let the database number them; keep the links
                vals = [r.get(k) for k in SALE_COLS[1:]]
                new_id[r["id"]] = conn.execute(
                    f"INSERT INTO sales({','.join(SALE_COLS[1:])}) VALUES "
                    f"({','.join('?' * (len(SALE_COLS) - 1))}) RETURNING id", vals
                ).fetchone()["id"]
            for r in data.sale_items or []:
                if r.get("sale_id") in new_id:
                    vals = [new_id[r["sale_id"]]] + [r.get(k) for k in SALE_ITEM_COLS[1:]]
                    conn.execute(
                        f"INSERT INTO sale_items({','.join(SALE_ITEM_COLS)}) VALUES "
                        f"({','.join('?' * len(SALE_ITEM_COLS))})", vals)
    return {"restored_items": len(data.items), "restored_history": len(data.history),
            "restored_sales": len(data.sales or [])}


# --------------------------------------------------------------------------
# Online product-name lookup (Open Food Facts and sister databases)
# --------------------------------------------------------------------------
# Free, open databases built by volunteers; no account or key needed.
# Coverage of local products varies; the user can always correct the name.
LOOKUP_SOURCES = [
    ("Open Food Facts", "https://world.openfoodfacts.org"),
    ("Open Beauty Facts", "https://world.openbeautyfacts.org"),
    ("Open Products Facts", "https://world.openproductsfacts.org"),
]
LOOKUP_FIELDS = "product_name,product_name_en,brands,quantity,categories_tags,image_front_small_url"
USER_AGENT = "ScanPrice/1.4 (family grocery price app)"
_lookup_cache: dict[str, dict] = {}

# first matching tag wins; order matters (beer before generic beverages)
CATEGORY_RULES = [
    ("Beer & Alcohol", ("alcoholic-beverages", "beers", "wines", "spirits")),
    ("Water", ("waters",)),
    ("Coffee & Tea", ("coffees", "teas", "instant-coffees", "coffee-drinks")),
    ("Drinks", ("beverages", "drinks", "juices", "sodas")),
    ("Noodles", ("noodles", "instant-noodles")),
    ("Rice", ("rices",)),
    ("Milk & Dairy", ("dairies", "milks", "yogurts", "cheeses")),
    ("Baby care", ("baby-foods", "baby-milks")),
    ("Bread & Bakery", ("breads", "cakes", "pastries")),
    ("Frozen food", ("frozen-foods",)),
    ("Snacks", ("snacks", "chips", "crisps", "biscuits", "cookies", "candies",
                "confectioneries", "chocolates", "crackers")),
    ("Cooking & Spices", ("sauces", "condiments", "fish-sauces", "seasonings", "oils",
                          "vinegars", "sugars", "salts", "spices", "flours")),
    ("Canned food", ("canned-foods",)),
    ("Fruits & Vegetables", ("fruits", "vegetables", "fresh-foods")),
    ("Meat, Fish & Eggs", ("meats", "fishes", "seafood", "eggs")),
]


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def build_name(p: dict) -> str:
    name = (p.get("product_name_en") or p.get("product_name") or "").strip()
    brands = [b.strip() for b in (p.get("brands") or "").split(",") if b.strip()]
    if brands and not any(_norm(b) and _norm(b) in _norm(name) for b in brands):
        name = f"{brands[0]} {name}".strip()
    qty = (p.get("quantity") or "").strip()
    if qty and _norm(qty) not in _norm(name):
        name = f"{name} {qty}".strip()
    return re.sub(r"\s+", " ", name)[:200]


def guess_category(p: dict, source: str) -> str:
    tags = [t.split(":", 1)[-1] for t in p.get("categories_tags") or []]
    for category, keys in CATEGORY_RULES:
        if any(t in keys for t in tags):
            return category
    if source == "Open Beauty Facts":
        return "Personal care"
    if source == "Open Products Facts":
        return "Household & Cleaning"
    return ""


LOOKUP_ERROR = {"_error": True}  # network problem: try again next time


async def fetch_product(client: httpx.AsyncClient, base: str, barcode: str) -> Optional[dict]:
    """One database: the product dict, None if not listed, LOOKUP_ERROR on failure."""
    try:
        r = await client.get(f"{base}/api/v2/product/{barcode}.json",
                             params={"fields": LOOKUP_FIELDS})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            return LOOKUP_ERROR
        data = r.json()
        return data.get("product") if data.get("status") == 1 else None
    except (httpx.HTTPError, ValueError):
        return LOOKUP_ERROR


class Similar(BaseModel):
    barcode: str
    name: str
    category: str


class LookupOut(BaseModel):
    found: bool
    barcode: str
    name: str = ""
    category: str = ""
    image_url: str = ""
    source: str = ""
    error: bool = False               # online databases could not be reached
    similar: Optional[Similar] = None  # item in this shop from the same company


def same_company_item(barcode: str) -> Optional[Similar]:
    """Saved item whose barcode shares the longest start (company prefix, >= 7 digits).

    GS1 barcodes begin with the maker's company number, so a match usually
    means same brand, e.g. another flavour or size.
    """
    best, best_len = None, 6
    with db() as conn:
        rows = conn.execute(
            "SELECT barcode,name,category FROM items WHERE barcode LIKE ? AND barcode <> ?",
            (barcode[:7] + "%", barcode)).fetchall()
    for r in rows:
        n = 0
        for a, b in zip(r["barcode"], barcode):
            if a != b:
                break
            n += 1
        if n > best_len and len(r["barcode"]) == len(barcode):
            best, best_len = r, n
    return Similar(barcode=best["barcode"], name=best["name"], category=best["category"]) if best else None


@app.get("/api/lookup/{barcode}", response_model=LookupOut)
async def lookup_online(barcode: str):
    """Suggest a product name for a barcode that is not in the shop yet."""
    barcode = barcode.strip()
    if not re.fullmatch(r"\d{6,14}", barcode):  # only real product barcodes
        return LookupOut(found=False, barcode=barcode)
    similar = same_company_item(barcode)
    if barcode in _lookup_cache:
        return LookupOut(**_lookup_cache[barcode], similar=similar)
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(fetch_product(client, base, barcode) for _, base in LOOKUP_SOURCES))
    out = {"found": False, "barcode": barcode,
           "error": any(r is LOOKUP_ERROR for r in results)}
    for (source, _), product in zip(LOOKUP_SOURCES, results):
        if product and product is not LOOKUP_ERROR and build_name(product):
            out = {"found": True, "barcode": barcode, "name": build_name(product),
                   "category": guess_category(product, source),
                   "image_url": product.get("image_front_small_url") or "",
                   "source": source}
            break
    if not out.get("error"):          # never remember a failed lookup
        if len(_lookup_cache) > 2000:
            _lookup_cache.clear()
        _lookup_cache[barcode] = out
    return LookupOut(**out, similar=similar)


# --------------------------------------------------------------------------
# Sales / basket
# --------------------------------------------------------------------------
# Money rule (same in the browser): every line is converted to riel at the
# shop rate, the basket total is rounded to the nearest 100 ៛ once, and the
# dollar total is the exact riel total / rate. Change is given in riel, or as
# whole dollars + riel.
CAMBODIA = timezone(timedelta(hours=7))  # Cambodia has no daylight saving


def local_day(ts: Optional[datetime] = None) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(CAMBODIA).date().isoformat()


def khr_exact(price: float, currency: str, rate: float) -> float:
    return price * rate if currency == "USD" else price


class SaleLineIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)  # item, or its pack/case barcode
    qty: int = Field(ge=1, le=9999)
    unit: str = Field(default="", max_length=30)        # '' = single piece (រាយ)


class SaleIn(BaseModel):
    items: list[SaleLineIn] = Field(min_length=1, max_length=500)
    paid_usd: float = Field(default=0, ge=0)
    paid_khr: float = Field(default=0, ge=0)


class SaleLineOut(BaseModel):
    barcode: str
    name: str
    qty: int
    price: float
    currency: Currency
    line_khr: int
    unit: str = ""        # '' = single piece
    pieces: int = 1       # pieces per unit


class SaleOut(BaseModel):
    id: int
    created_at: str
    day: str
    rate: float
    items_count: int
    total_khr: int
    total_usd: float
    profit_usd: Optional[float] = None
    paid_usd: float
    paid_khr: float
    change_khr: int
    change_usd_part: int = 0       # change as whole dollars ...
    change_khr_part: int = 0       # ... plus riel
    lines: list[SaleLineOut] = []


class DaySummary(BaseModel):
    day: str
    sales: int
    items: int
    total_usd: float
    total_khr: int
    profit_usd: Optional[float]     # only from items that have a cost price
    list: list[SaleOut]


def change_split(change_khr: float, rate: float) -> tuple[int, int]:
    dollars = int(change_khr // rate) if change_khr > 0 else 0
    return dollars, round_khr(change_khr - dollars * rate)


def sale_out(row, lines=None) -> SaleOut:
    dollars, riel = change_split(row["change_khr"], row["rate"])
    profit = None
    if row["cost_khr"] is not None:
        profit = round((row["total_khr"] - row["cost_khr"]) / row["rate"], 2)
    return SaleOut(
        id=row["id"], created_at=row["created_at"], day=row["day"], rate=row["rate"],
        items_count=row["items_count"], total_khr=round_khr(row["total_khr"]),
        total_usd=row["total_usd"], profit_usd=profit, paid_usd=row["paid_usd"],
        paid_khr=row["paid_khr"], change_khr=round_khr(row["change_khr"]),
        change_usd_part=dollars, change_khr_part=riel,
        lines=[SaleLineOut(barcode=l["barcode"], name=l["name"], qty=l["qty"],
                           price=l["price"], currency=l["currency"],
                           line_khr=round_khr(l["line_khr"]), unit=_col(l, "unit") or "",
                           pieces=_col(l, "pieces") or 1) for l in (lines or [])],
    )


@app.post("/api/sales", response_model=SaleOut, status_code=201)
def create_sale(sale: SaleIn):
    """Record a finished sale. Prices come from the database, not the phone."""
    ts = datetime.now(timezone.utc)
    with db() as conn:
        rate = get_rate(conn)
        # resolve each line to (item, unit name, pieces per unit, unit price); a
        # pack/case barcode means that unit. Same item + unit twice -> add up.
        merged: dict[tuple[str, str], list] = {}
        for line in sale.items:
            code, unit = line.barcode.strip(), line.unit.strip()
            it = conn.execute("SELECT * FROM items WHERE barcode=?", (code,)).fetchone()
            if it is None:
                u = conn.execute("SELECT * FROM item_units WHERE unit_barcode=?", (code,)).fetchone()
                if u is None:
                    raise HTTPException(400, f"Unknown barcode {code}: add the item first")
                it = conn.execute("SELECT * FROM items WHERE barcode=?",
                                  (u["item_barcode"],)).fetchone()
                unit = unit or u["unit"]
            if unit:
                u = conn.execute("SELECT * FROM item_units WHERE item_barcode=? AND unit=?",
                                 (it["barcode"], unit)).fetchone()
                if u is None:
                    raise HTTPException(400, f"{it['name']} has no selling type '{unit}'")
                pieces, price = u["qty"], u["price"]
            else:
                pieces, price = 1, it["price"]
            key = (it["barcode"], unit)
            if key in merged:
                merged[key][4] += line.qty
            else:
                merged[key] = [it, unit, pieces, price, line.qty]
        lines, total, cost_total, cost_known = [], 0.0, 0.0, True
        pieces_sold: dict[str, int] = {}
        for it, unit, pieces, price, qty in merged.values():
            line_khr = khr_exact(price, it["currency"], rate) * qty
            total += line_khr
            line_cost = None if it["cost"] is None else it["cost"] * pieces   # cost is per piece
            if line_cost is None:
                cost_known = False
            else:
                cost_total += khr_exact(line_cost, it["currency"], rate) * qty
            pieces_sold[it["barcode"]] = pieces_sold.get(it["barcode"], 0) + qty * pieces
            lines.append((it, unit, pieces, price, qty, line_khr, line_cost))
        paid = sale.paid_usd * rate + sale.paid_khr
        if paid and paid + 1e-6 < round_khr(total):
            raise HTTPException(400, "Customer paid less than the total")
        change = max(0.0, paid - total) if paid else 0.0
        sale_id = conn.execute(
            "INSERT INTO sales(created_at,day,rate,items_count,total_khr,total_usd,cost_khr,"
            "paid_usd,paid_khr,change_khr) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (ts.isoformat(timespec="seconds"), local_day(ts), rate,
             sum(l[4] for l in lines), total, round(total / rate, 2),
             cost_total if cost_known else None, sale.paid_usd, sale.paid_khr, change),
        ).fetchone()["id"]
        stock_of = {}
        for it, unit, pieces, price, qty, line_khr, line_cost in lines:
            conn.execute(
                "INSERT INTO sale_items(sale_id,barcode,name,qty,price,currency,line_khr,cost,"
                "unit,pieces) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sale_id, it["barcode"], it["name"], qty, price, it["currency"],
                 line_khr, line_cost, unit, pieces),
            )
            stock_of[it["barcode"]] = it["stock"]
        for code, sold in pieces_sold.items():  # count down stock in pieces, never below 0
            if stock_of[code] is not None:
                conn.execute("UPDATE items SET stock=? WHERE barcode=?",
                             (max(0, stock_of[code] - sold), code))
        row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        sl = conn.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY id", (sale_id,)).fetchall()
        return sale_out(row, sl)


@app.get("/api/sales", response_model=DaySummary)
def list_sales(day: str = Query(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")):
    """Sales of one day (Cambodia time), newest first, with totals."""
    day = day or local_day()
    with db() as conn:
        rows = conn.execute("SELECT * FROM sales WHERE day=? ORDER BY id DESC", (day,)).fetchall()
        out = []
        for r in rows:
            sl = conn.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY id",
                              (r["id"],)).fetchall()
            out.append(sale_out(r, sl))
    profits = [s.profit_usd for s in out]
    return DaySummary(
        day=day, sales=len(out), items=sum(s.items_count for s in out),
        total_usd=round(sum(r["total_khr"] / r["rate"] for r in rows), 2),
        total_khr=round_khr(sum(r["total_khr"] for r in rows)),
        profit_usd=round(sum(profits), 2) if out and all(p is not None for p in profits) else None,
        list=out,
    )


@app.delete("/api/sales/{sale_id}", status_code=204)
def cancel_sale(sale_id: int):
    """Cancel a mistaken sale and put the items back in stock."""
    with db() as conn:
        if not conn.execute("SELECT id FROM sales WHERE id=?", (sale_id,)).fetchone():
            raise HTTPException(404, "Sale not found")
        for l in conn.execute("SELECT barcode,qty,pieces FROM sale_items WHERE sale_id=?",
                              (sale_id,)).fetchall():
            conn.execute("UPDATE items SET stock=stock+? WHERE barcode=? AND stock IS NOT NULL",
                         (l["qty"] * (l["pieces"] or 1), l["barcode"]))
        conn.execute("DELETE FROM sale_items WHERE sale_id=?", (sale_id,))
        conn.execute("DELETE FROM sales WHERE id=?", (sale_id,))


# --------------------------------------------------------------------------
# Product photos
# --------------------------------------------------------------------------
# The phone shrinks photos to ~400 px JPEG (~20-40 KB) before upload, so even
# thousands fit easily in the free database. Online photos are only linked.
MAX_PHOTO_BYTES = 300_000
PHOTO_LINK_HOSTS = ("images.openfoodfacts.org", "images.openbeautyfacts.org",
                    "images.openproductsfacts.org", "static.openfoodfacts.org")


def image_type(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _need_item(conn, barcode: str) -> None:
    if not conn.execute("SELECT id FROM items WHERE barcode=?", (barcode,)).fetchone():
        raise HTTPException(404, "Item not found")


def _store_photo(conn, barcode: str, url: Optional[str], mime: Optional[str],
                 data: Optional[bytes]) -> None:
    conn.execute("DELETE FROM item_photos WHERE barcode=?", (barcode,))
    conn.execute(
        "INSERT INTO item_photos(barcode,url,mime,data,updated_at) VALUES (?,?,?,?,?)",
        (barcode, url, mime, data, now()))


@app.put("/api/items/{barcode}/photo", response_model=ItemOut)
async def upload_photo(barcode: str, request: Request):
    """Body = the image file itself (JPEG/PNG/WebP, max 300 KB)."""
    barcode = barcode.strip()
    data = await request.body()
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo too large; the app shrinks photos before upload")
    mime = image_type(data)
    if not mime:
        raise HTTPException(415, "Not a JPEG, PNG or WebP image")

    def save():
        with db() as conn:
            _need_item(conn, barcode)
            _store_photo(conn, barcode, None, mime, data)
            row = conn.execute(ITEM_SELECT + " WHERE items.barcode=?", (barcode,)).fetchone()
            return to_out(row, get_rate(conn))
    return await run_in_threadpool(save)


class PhotoLinkIn(BaseModel):
    url: str = Field(max_length=500)


@app.put("/api/items/{barcode}/photo-link", response_model=ItemOut)
def link_photo(barcode: str, body: PhotoLinkIn):
    """Use an online product photo (from the name lookup) without storing it."""
    barcode = barcode.strip()
    m = re.match(r"^https://([^/]+)/", body.url)
    if not m or m.group(1) not in PHOTO_LINK_HOSTS:
        raise HTTPException(400, "Only Open Food Facts photo links are allowed")
    with db() as conn:
        _need_item(conn, barcode)
        _store_photo(conn, barcode, body.url, None, None)
        row = conn.execute(ITEM_SELECT + " WHERE items.barcode=?", (barcode,)).fetchone()
        return to_out(row, get_rate(conn))


@app.get("/api/items/{barcode}/photo")
def get_photo(barcode: str):
    with db() as conn:
        row = conn.execute("SELECT url,mime,data FROM item_photos WHERE barcode=?",
                           (barcode.strip(),)).fetchone()
    if not row:
        raise HTTPException(404, "No photo")
    if row["data"] is None:
        return Response(status_code=307, headers={"Location": row["url"]})
    # ?v=<time> in the link changes when the photo changes, so cache for long
    return Response(bytes(row["data"]), media_type=row["mime"],
                    headers={"Cache-Control": "private, max-age=31536000"})


@app.delete("/api/items/{barcode}/photo", status_code=204)
def delete_photo(barcode: str):
    with db() as conn:
        conn.execute("DELETE FROM item_photos WHERE barcode=?", (barcode.strip(),))


class PhotoInfo(BaseModel):
    barcode: str
    name: str
    kind: Literal["own", "link"]
    url: str              # where to download it (own: this app; link: online)
    updated_at: str


@app.get("/api/photos", response_model=list[PhotoInfo])
def list_photos():
    """All photos, for backup.py to copy into the OneDrive / Google Drive folder."""
    with db() as conn:
        rows = conn.execute(
            "SELECT p.barcode, i.name, p.url, p.updated_at, "
            "CASE WHEN p.data IS NULL THEN 0 ELSE 1 END AS own "
            "FROM item_photos p JOIN items i ON i.barcode = p.barcode ORDER BY p.barcode"
        ).fetchall()
    return [PhotoInfo(barcode=r["barcode"], name=r["name"],
                      kind="own" if r["own"] else "link",
                      url=f"/api/items/{r['barcode']}/photo" if r["own"] else r["url"],
                      updated_at=r["updated_at"]) for r in rows]
