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
