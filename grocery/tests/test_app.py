import base64
import importlib
import os

import pytest
from fastapi.testclient import TestClient

# Set TEST_DATABASE_URL=postgresql://... to also run every test on Postgres.
BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_DATABASE_URL") else [])


def make_client(tmp_path, monkeypatch, backend, password=""):
    monkeypatch.setenv("APP_PASSWORD", password)
    if backend == "postgres":
        url = os.environ["TEST_DATABASE_URL"]
        monkeypatch.setenv("DATABASE_URL", url)
        import psycopg
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS items, price_history, settings, sales, sale_items, item_photos")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("GROCERY_DB", str(tmp_path / "test.db"))
    import main
    importlib.reload(main)
    return TestClient(main.app)


@pytest.fixture(params=BACKENDS)
def client(request, tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch, request.param)


def item(**kw):
    base = {"barcode": "8851234567890", "name": "Coca-Cola 330ml", "category": "Drinks",
            "price": 0.75, "currency": "USD", "cost": 0.5, "stock": 24}
    base.update(kw)
    return base


def test_not_found(client):
    assert client.get("/api/items/123").status_code == 404


def test_create_usd_item_shows_both_currencies(client):
    r = client.put("/api/items/8851234567890", json=item())
    assert r.status_code == 200
    d = r.json()
    assert d["price_usd"] == 0.75
    assert d["price_khr"] == 3100  # 0.75 * 4100 = 3075 -> rounded to 100
    assert d["margin_pct"] == 33.3


def test_khr_item(client):
    d = client.put("/api/items/111", json=item(barcode="111", name="Rice 1kg",
                                              price=4500, currency="KHR", cost=None)).json()
    assert d["price_khr"] == 4500
    assert d["price_usd"] == round(4500 / 4100, 2)
    assert d["margin_pct"] is None


def test_price_history_only_on_change(client):
    client.put("/api/items/8851234567890", json=item())
    client.put("/api/items/8851234567890", json=item(stock=10))        # no price change
    client.put("/api/items/8851234567890", json=item(price=0.8))       # price change
    h = client.get("/api/items/8851234567890/history").json()
    assert [x["price"] for x in h] == [0.8, 0.75]


def test_rate_change_updates_conversion(client):
    client.put("/api/items/8851234567890", json=item(price=1))
    client.put("/api/settings/rate", json={"khr_per_usd": 4000})
    assert client.get("/api/items/8851234567890").json()["price_khr"] == 4000


def test_search_summary_export_delete(client):
    client.put("/api/items/8851234567890", json=item())
    client.put("/api/items/222", json=item(barcode="222", name="Angkor Beer", stock=None))
    assert len(client.get("/api/items", params={"q": "angkor"}).json()) == 1
    s = client.get("/api/summary").json()
    assert s["items"] == 2 and s["categories"] == ["Drinks"]
    assert s["stock_value_usd"] == 12.0  # 24 x $0.50 cost
    csv = client.get("/api/export.csv").text
    assert "Angkor Beer" in csv and "price_khr" in csv
    assert client.delete("/api/items/222").status_code == 204
    assert client.get("/api/items/222").status_code == 404


def test_validation(client):
    assert client.put("/api/items/1", json=item(barcode="1", price=-1)).status_code == 422
    assert client.put("/api/items/1", json=item(barcode="2")).status_code == 400
    assert client.put("/api/items/1", json=item(barcode="1", currency="EUR")).status_code == 422


def test_homepage(client):
    assert "ScanPrice" in client.get("/").text


def test_search_is_case_insensitive(client):
    client.put("/api/items/222", json=item(barcode="222", name="Angkor Beer"))
    assert len(client.get("/api/items", params={"q": "ANGKOR"}).json()) == 1
    assert len(client.get("/api/items", params={"q": "drinks"}).json()) == 1


def test_backup_and_restore_roundtrip(client):
    client.put("/api/items/8851234567890", json=item())
    client.put("/api/items/8851234567890", json=item(price=0.8))
    client.put("/api/items/333", json=item(barcode="333", name="អង្ករ Rice", price=4500,
                                          currency="KHR", cost=None, stock=None))
    client.put("/api/settings/rate", json={"khr_per_usd": 4050})
    snap = client.get("/api/backup").json()
    assert len(snap["items"]) == 2 and len(snap["history"]) == 3

    client.delete("/api/items/333")
    client.put("/api/settings/rate", json={"khr_per_usd": 4100})
    assert client.post("/api/restore", json=snap).status_code == 400  # needs confirm
    r = client.post("/api/restore?confirm=true", json=snap)
    assert r.json() == {"restored_items": 2, "restored_history": 3, "restored_sales": 0}
    assert client.get("/api/items/333").json()["name"] == "អង្ករ Rice"
    assert client.get("/api/settings/rate").json()["khr_per_usd"] == 4050
    assert len(client.get("/api/items/8851234567890/history").json()) == 2
    assert client.get("/api/backup").json()["items"] == snap["items"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_password_required_when_set(tmp_path, monkeypatch, backend):
    c = make_client(tmp_path, monkeypatch, backend, password="s3cret")
    assert c.get("/").status_code == 401
    assert c.get("/api/items").status_code == 401
    assert c.get("/api/items", auth=("shop", "wrong")).status_code == 401
    assert c.get("/api/items", auth=("anyone", "s3cret")).status_code == 200
    assert c.get("/healthz").json()["database"] == backend  # open for health checks
    assert c.get("/static/manifest.webmanifest").json()["short_name"] == "ScanPrice"
    assert c.get("/static/apple-touch-icon.png").status_code == 200
    assert c.get("/static/index.html").status_code == 401  # the app itself stays locked


@pytest.mark.parametrize("pasted", [
    "psql 'postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require'",
    '  "postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require"  ',
    "postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require\n",
])
def test_database_url_cleans_copy_paste(monkeypatch, pasted):
    import db
    monkeypatch.setenv("DATABASE_URL", pasted)
    assert db.database_url() == "postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require"
    assert db.is_postgres()


def test_healthz_hint_when_not_postgres(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, "sqlite")
    assert "not set" in c.get("/healthz").json()["hint"]


# ---------------- online name lookup (network faked) ----------------
COKE_TH = {"product_name": "Coca Cola", "product_name_en": "Coca Cola",
           "brands": "Coke, Coca-Cola, โค๊ก", "quantity": "325 ml",
           "categories_tags": ["en:beverages", "en:sodas"],
           "image_front_small_url": "https://images.example/coke.jpg"}


def fake_sources(monkeypatch, answers):
    import main
    calls = []

    async def fake_fetch(client, base, barcode):
        calls.append(base)
        return answers.get(base)
    monkeypatch.setattr(main, "fetch_product", fake_fetch)
    main._lookup_cache.clear()
    return calls


def test_lookup_found_on_open_food_facts(client, monkeypatch):
    fake_sources(monkeypatch, {"https://world.openfoodfacts.org": COKE_TH})
    r = client.get("/api/lookup/8851959132012").json()
    assert r["found"] and r["source"] == "Open Food Facts"
    assert r["name"] == "Coca Cola 325 ml"          # brand already in name, size added
    assert r["category"] == "Drinks"
    assert r["image_url"].endswith("coke.jpg")


def test_lookup_brand_prefix_and_other_sources(client, monkeypatch):
    shampoo = {"product_name": "Shampoo Anti-dandruff", "brands": "Head & Shoulders",
               "quantity": "170 ml", "categories_tags": []}
    fake_sources(monkeypatch, {"https://world.openbeautyfacts.org": shampoo})
    r = client.get("/api/lookup/4902430401234").json()
    assert r["name"] == "Head & Shoulders Shampoo Anti-dandruff 170 ml"
    assert r["category"] == "Personal care" and r["source"] == "Open Beauty Facts"


def test_lookup_not_found_cached_and_skips_non_barcodes(client, monkeypatch):
    calls = fake_sources(monkeypatch, {})
    assert client.get("/api/lookup/8850999320045").json()["found"] is False
    client.get("/api/lookup/8850999320045")
    assert len(calls) == 3                            # 3 sources, second call cached
    assert client.get("/api/lookup/veg01").json()["found"] is False
    assert len(calls) == 3                            # own codes never sent online


def test_category_rules():
    import main
    beer = {"categories_tags": ["en:beverages", "en:alcoholic-beverages", "en:beers"]}
    assert main.guess_category(beer, "Open Food Facts") == "Beer & Alcohol"
    assert main.guess_category({"categories_tags": ["en:instant-noodles"]}, "x") == "Noodles"
    assert main.guess_category({}, "Open Products Facts") == "Household & Cleaning"
    assert main.guess_category({"categories_tags": ["en:instant-coffees"]}, "x") == "Coffee & Tea"


def test_lookup_network_error_is_not_cached(client, monkeypatch):
    import main
    calls = fake_sources(monkeypatch, {"https://world.openfoodfacts.org": main.LOOKUP_ERROR})
    r = client.get("/api/lookup/8851959132012").json()
    assert r["found"] is False and r["error"] is True
    fake_sources(monkeypatch, {"https://world.openfoodfacts.org": COKE_TH})  # internet back
    assert client.get("/api/lookup/8851959132012").json()["found"] is True


def test_lookup_suggests_same_company_item(client, monkeypatch):
    fake_sources(monkeypatch, {})
    client.put("/api/items/8841234500017", json=item(barcode="8841234500017",
               name="Kampot Pepper 50g", category="Cooking"))
    r = client.get("/api/lookup/8841234500024").json()     # same company, other product
    assert r["found"] is False
    assert r["similar"]["name"] == "Kampot Pepper 50g" and r["similar"]["category"] == "Cooking"
    assert client.get("/api/lookup/8859999999994").json()["similar"] is None


def test_healthz_version_and_page_not_cached(client):
    assert client.get("/healthz").json()["version"]
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_app_starts_and_reports_when_database_is_down(tmp_path, monkeypatch):
    # unreachable Postgres: the app must still start, explain, and recover later
    monkeypatch.setenv("APP_PASSWORD", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secretpw@127.0.0.1:1/x?connect_timeout=1")
    import main
    importlib.reload(main)
    c = TestClient(main.app)
    h = c.get("/healthz").json()
    assert h["ok"] is False and "OperationalError" in h["database_error"]
    assert "secretpw" not in h["database_error"]
    assert c.get("/").status_code == 200                   # page still loads
    r = c.get("/api/items")
    assert r.status_code == 503 and "Database not reachable" in r.json()["detail"]
    monkeypatch.setenv("DATABASE_URL", "")                  # database back
    monkeypatch.setenv("GROCERY_DB", str(tmp_path / "back.db"))
    assert c.get("/api/items").status_code == 200
    assert c.get("/healthz").json()["ok"] is True



# ---------------- sales / basket ----------------
def stock_shop(client):
    client.put("/api/items/111", json=item(barcode="111", name="Coke", price=0.75,
                                          currency="USD", cost=0.5, stock=10))
    client.put("/api/items/222", json=item(barcode="222", name="Noodles", price=1000,
                                          currency="KHR", cost=700, stock=5))


def test_sale_totals_mixed_currencies_and_change(client):
    stock_shop(client)
    r = client.post("/api/sales", json={"items": [{"barcode": "111", "qty": 2},
                                                  {"barcode": "222", "qty": 3}],
                                        "paid_usd": 5, "paid_khr": 0})
    assert r.status_code == 201
    s = r.json()
    # 2 x $0.75 = 6,150 ៛ ; 3 x 1,000 ៛ = 3,000 ៛ ; total 9,150 -> 9,200 ៛
    assert s["total_khr"] == 9200 and s["total_usd"] == round(9150 / 4100, 2)
    assert s["items_count"] == 5
    # paid $5 = 20,500 ៛ ; change 11,350 -> 11,400 ៛ = $2 + 3,200 ៛
    assert s["change_khr"] == 11400
    assert (s["change_usd_part"], s["change_khr_part"]) == (2, 3200)
    # profit: (9,150 - (2x0.5x4100 + 3x700)) / 4100 = 2,950 / 4100
    assert s["profit_usd"] == round(2950 / 4100, 2)
    assert [(l["name"], l["qty"]) for l in s["lines"]] == [("Coke", 2), ("Noodles", 3)]


def test_sale_updates_stock_and_cancel_restores_it(client):
    stock_shop(client)
    sid = client.post("/api/sales", json={"items": [{"barcode": "222", "qty": 4},
                                                    {"barcode": "222", "qty": 3}]}).json()["id"]
    assert client.get("/api/items/222").json()["stock"] == 0        # 5 - 7, floored at 0
    assert client.delete(f"/api/sales/{sid}").status_code == 204
    assert client.get("/api/items/222").json()["stock"] == 7
    assert client.get("/api/sales").json()["sales"] == 0


def test_sale_rejects_unknown_item_and_underpayment(client):
    stock_shop(client)
    r = client.post("/api/sales", json={"items": [{"barcode": "999", "qty": 1}]})
    assert r.status_code == 400 and "999" in r.json()["detail"]
    r = client.post("/api/sales", json={"items": [{"barcode": "111", "qty": 1}], "paid_khr": 1000})
    assert r.status_code == 400
    assert client.post("/api/sales", json={"items": []}).status_code == 422
    assert client.get("/api/sales").json()["sales"] == 0           # nothing half-saved


def test_day_summary(client):
    stock_shop(client)
    client.post("/api/sales", json={"items": [{"barcode": "111", "qty": 1}]})
    client.post("/api/sales", json={"items": [{"barcode": "222", "qty": 2}]})
    d = client.get("/api/sales").json()
    assert d["sales"] == 2 and d["items"] == 3
    assert d["total_khr"] == 5100                                   # 3,075 + 2,000 -> 5,100
    assert d["list"][0]["lines"][0]["name"] == "Noodles"            # newest first
    assert client.get("/api/sales", params={"day": "2000-01-01"}).json()["sales"] == 0


def test_sales_survive_backup_and_restore(client):
    stock_shop(client)
    client.post("/api/sales", json={"items": [{"barcode": "111", "qty": 2}]})
    snap = client.get("/api/backup").json()
    assert len(snap["sales"]) == 1 and len(snap["sale_items"]) == 1
    client.delete(f"/api/sales/{snap['sales'][0]['id']}")
    r = client.post("/api/restore?confirm=true", json=snap).json()
    assert r["restored_sales"] == 1
    d = client.get("/api/sales", params={"day": snap["sales"][0]["day"]}).json()
    assert d["sales"] == 1 and d["list"][0]["lines"][0]["qty"] == 2


# ---------------- photos ----------------
# an 8x8 red JPEG
TINY_JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDCooorzj7E/9k="


def small_jpeg() -> bytes:
    return base64.b64decode(TINY_JPEG)


def test_upload_show_replace_and_delete_photo(client):
    client.put("/api/items/111", json=item(barcode="111", name="Coke"))
    assert client.get("/api/items/111").json()["photo_url"] is None
    jpg = small_jpeg()
    r = client.put("/api/items/111/photo", content=jpg, headers={"content-type": "image/jpeg"})
    assert r.status_code == 200
    url = r.json()["photo_url"]
    assert url.startswith("/api/items/111/photo?v=")
    got = client.get(url)
    assert got.status_code == 200 and got.content == jpg
    assert got.headers["content-type"] == "image/jpeg"
    assert client.get("/api/items", params={"q": "coke"}).json()[0]["photo_url"] == url
    # replace with an online link
    link = "https://images.openfoodfacts.org/images/products/885/195/913/2012/front_en.36.200.jpg"
    r = client.put("/api/items/111/photo-link", json={"url": link})
    assert r.json()["photo_url"] == link
    red = client.get("/api/items/111/photo", follow_redirects=False)
    assert red.status_code == 307 and red.headers["location"] == link
    assert client.delete("/api/items/111/photo").status_code == 204
    assert client.get("/api/items/111").json()["photo_url"] is None


def test_photo_validation(client):
    client.put("/api/items/111", json=item(barcode="111"))
    assert client.put("/api/items/111/photo", content=b"hello").status_code == 415
    assert client.put("/api/items/111/photo", content=b"\xff\xd8\xff" + b"0" * 400_000).status_code == 413
    assert client.put("/api/items/999/photo", content=small_jpeg()).status_code == 404
    bad = client.put("/api/items/111/photo-link", json={"url": "https://evil.example/x.jpg"})
    assert bad.status_code == 400


def test_photo_list_for_backup_and_item_delete(client):
    client.put("/api/items/111", json=item(barcode="111", name="Coke"))
    client.put("/api/items/222", json=item(barcode="222", name="Rice"))
    client.put("/api/items/111/photo", content=small_jpeg())
    client.put("/api/items/222/photo-link",
               json={"url": "https://images.openfoodfacts.org/images/x.jpg"})
    ph = {p["barcode"]: p for p in client.get("/api/photos").json()}
    assert ph["111"]["kind"] == "own" and ph["111"]["url"] == "/api/items/111/photo"
    assert ph["222"]["kind"] == "link" and ph["222"]["name"] == "Rice"
    client.delete("/api/items/111")
    assert [p["barcode"] for p in client.get("/api/photos").json()] == ["222"]


def test_shop_name_default_update_and_backup(client):
    assert client.get("/api/settings/shop").json()["shop_name"] == "Gech Heng Wing Shop"
    r = client.put("/api/settings/shop", json={"shop_name": "  Gech   Heng Wing Shop 2 "})
    assert r.json()["shop_name"] == "Gech Heng Wing Shop 2"
    assert client.put("/api/settings/shop", json={"shop_name": "   "}).status_code == 422
    snap = client.get("/api/backup").json()
    assert snap["shop_name"] == "Gech Heng Wing Shop 2"
    client.put("/api/settings/shop", json={"shop_name": "Other"})
    client.post("/api/restore?confirm=true", json=snap)
    assert client.get("/api/settings/shop").json()["shop_name"] == "Gech Heng Wing Shop 2"


def test_categories_default_counts_add_remove_reset(client):
    cats = client.get("/api/categories").json()
    names = [c["name"] for c in cats]
    assert "Drinks" in names and "Phone cards" in names and len(names) >= 20
    assert next(c for c in cats if c["name"] == "Rice")["km"] == "អង្ករ"
    client.put("/api/items/1", json=item(barcode="1", category="Drinks"))
    client.put("/api/items/2", json=item(barcode="2", category="Drinks"))
    client.put("/api/items/3", json=item(barcode="3", category="Lottery"))   # not in the list
    cats = client.get("/api/categories").json()
    assert next(c for c in cats if c["name"] == "Drinks")["count"] == 2
    assert cats[-1] == {"name": "Lottery", "km": "", "count": 1}             # unlisted, still shown
    # replace list: keep two, add one (duplicates ignored)
    r = client.put("/api/categories", json=[{"name": "Drinks", "km": "ភេសជ្ជៈ"},
                                           {"name": " Ice  cream ", "km": "ការ៉េម"},
                                           {"name": "drinks"}])
    assert [c["name"] for c in r.json()] == ["Drinks", "Ice cream", "Lottery"]
    snap = client.get("/api/backup").json()
    assert [c["name"] for c in snap["categories"]] == ["Drinks", "Ice cream"]
    client.put("/api/categories", json=[])                                     # back to defaults
    assert len(client.get("/api/categories").json()) >= 21
    client.post("/api/restore?confirm=true", json=snap)
    assert [c["name"] for c in client.get("/api/categories").json()][:2] == ["Drinks", "Ice cream"]
