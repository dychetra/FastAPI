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
            c.execute("DROP TABLE IF EXISTS items, price_history, settings")
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
    assert r.json() == {"restored_items": 2, "restored_history": 3}
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
    assert main.guess_category({}, "Open Products Facts") == "Household"


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
