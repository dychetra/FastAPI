# Grocery Price Tracker · តម្លៃទំនិញ

A simple app for a family grocery shop. Scan a barcode to check the price; if
the item is new, type its name and price once and it is saved.

- Scan with a **USB/Bluetooth barcode scanner**, the **phone camera**, or type the number
- Prices saved in **USD or KHR**, always shown in **both** (KHR rounded to nearest 100 ៛)
- Your own **exchange rate** (default 4,100), changeable in Settings
- Optional **cost** → shows **margin %**; optional **stock qty** → stock value
- **Price history** for every item; **CSV export** that opens in Excel (Khmer text OK)

The app is in [`grocery/`](grocery/). It uses FastAPI and one SQLite file (`grocery.db`).

## Run it

```bash
cd grocery
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 on the shop computer. API docs: http://localhost:8000/docs

**Easiest setup:** a cheap USB barcode scanner plugged into the shop PC. It types the
number and presses Enter, so the price shows straight away.

## Using a phone camera

Browsers only allow the camera on `https://` or `localhost`. To use phones on the
shop Wi-Fi, run with a self-signed certificate:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=grocery" \
  -keyout key.pem -out cert.pem
uvicorn main:app --host 0.0.0.0 --port 8443 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Then on the phone open `https://<PC-IP-address>:8443` and accept the warning once.
The camera scanner library loads from the internet (unpkg.com).

## Backup

All data is in `grocery/grocery.db` — copy that file to back up. Or use
Settings → Download CSV.

## Tests

```bash
cd grocery && pytest -q
```
