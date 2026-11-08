import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GROCERY_DB", str(tmp_path / "test.db"))
    import main
    importlib.reload(main)
    return TestClient(main.app)


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
    assert "Grocery Price Tracker" in client.get("/").text
