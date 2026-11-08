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
import os
import secrets
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
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
class ItemIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=100)
    price: float = Field(gt=0, description="Selling price, in `currency`")
    currency: Currency = "USD"
    cost: Optional[float] = Field(default=None, ge=0, description="Buying cost, same currency")
    stock: Optional[int] = Field(default=None, ge=0)

    @field_validator("barcode", "name", "category")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


class ItemOut(ItemIn):
    id: int
    price_usd: float
    price_khr: int
    margin_pct: Optional[float] = None
    updated_at: str


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


def to_out(row, rate: float) -> ItemOut:
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
    )


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
APP_VERSION = "1.4.0"
app = FastAPI(title="ScanPrice", version=APP_VERSION)
init_db()

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
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    info = {"ok": True, "version": APP_VERSION, "database": database.backend_name()}
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
    sql = "SELECT * FROM items WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (lower(name) LIKE ? OR barcode LIKE ? OR lower(category) LIKE ?)"
        like = f"%{q.strip().lower()}%"
        args += [like, like, like]
    if category:
        sql += " AND category = ?"
        args.append(category)
    sql += " ORDER BY lower(name) LIMIT ?"
    args.append(limit)
    with db() as conn:
        rate = get_rate(conn)
        return [to_out(r, rate) for r in conn.execute(sql, args)]


@app.get("/api/items/{barcode}", response_model=ItemOut)
def get_item(barcode: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE barcode=?", (barcode.strip(),)).fetchone()
        if not row:
            raise HTTPException(404, "Item not found")
        return to_out(row, get_rate(conn))


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
        row = conn.execute("SELECT * FROM items WHERE barcode=?", (barcode,)).fetchone()
        return to_out(row, get_rate(conn))


@app.delete("/api/items/{barcode}", status_code=204)
def delete_item(barcode: str):
    with db() as conn:
        cur = conn.execute("DELETE FROM items WHERE barcode=?", (barcode.strip(),))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found")
        conn.execute("DELETE FROM price_history WHERE barcode=?", (barcode.strip(),))


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
        items = [to_out(r, rate) for r in conn.execute("SELECT * FROM items ORDER BY lower(name)")]
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel shows Khmer text correctly
    w = csv.writer(buf)
    w.writerow(["barcode", "name", "category", "price", "currency", "price_usd",
                "price_khr", "cost", "margin_pct", "stock", "updated_at"])
    for i in items:
        w.writerow([i.barcode, i.name, i.category, i.price, i.currency, i.price_usd,
                    i.price_khr, i.cost, i.margin_pct, i.stock, i.updated_at])
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
    items: list[ItemIn]
    history: list[HistoryRow]
    updated_at: dict[str, str] = {}


@app.get("/api/backup", response_model=Backup)
def backup():
    """Everything in one JSON document: items, price history and the rate."""
    with db() as conn:
        items = conn.execute("SELECT * FROM items ORDER BY barcode").fetchall()
        hist = conn.execute(
            "SELECT barcode,price,currency,changed_at FROM price_history ORDER BY id"
        ).fetchall()
        return Backup(
            created_at=now(),
            khr_per_usd=get_rate(conn),
            items=[ItemIn(**{k: r[k] for k in ItemIn.model_fields}) for r in items],
            history=[HistoryRow(**dict(r)) for r in hist],
            updated_at={r["barcode"]: r["updated_at"] for r in items},
        )


@app.post("/api/restore")
def restore(data: Backup, confirm: bool = False):
    """Replace ALL data with a backup. Needs ?confirm=true as a safety catch."""
    if not confirm:
        raise HTTPException(400, "Restore replaces all data. Call again with ?confirm=true")
    ts = now()
    with db() as conn:  # one transaction: all or nothing
        conn.execute("DELETE FROM price_history")
        conn.execute("DELETE FROM items")
        for i in data.items:
            conn.execute(
                "INSERT INTO items(barcode,name,category,price,currency,cost,stock,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (i.barcode, i.name, i.category, i.price, i.currency, i.cost, i.stock,
                 data.updated_at.get(i.barcode, ts)),
            )
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
    return {"restored_items": len(data.items), "restored_history": len(data.history)}


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
    ("Drinks", ("beverages", "drinks", "juices", "sodas", "teas", "coffees")),
    ("Noodles", ("noodles", "instant-noodles")),
    ("Rice", ("rices",)),
    ("Milk & Dairy", ("dairies", "milks", "yogurts", "cheeses")),
    ("Snacks", ("snacks", "chips", "crisps", "biscuits", "cookies", "candies",
                "confectioneries", "chocolates", "crackers")),
    ("Cooking", ("sauces", "condiments", "fish-sauces", "seasonings", "oils",
                 "vinegars", "sugars", "salts", "spices")),
    ("Canned food", ("canned-foods",)),
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
        return "Household"
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
