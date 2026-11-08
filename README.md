# ScanPrice · ស្កេនតម្លៃ

**ScanPrice** is a simple app for a family grocery shop. Scan a barcode to check the price; if
the item is new, type its name and price once and it is saved.

- Scan with a **USB/Bluetooth barcode scanner**, the **phone camera**, or type the number
- Prices saved in **USD or KHR**, always shown in **both** (KHR rounded to nearest 100 ៛)
- Your own **exchange rate** (default 4,100), changeable in Settings
- Optional **cost** → shows **margin %**; optional **stock qty** → stock value
- **Price history** for every item; **CSV export** that opens in Excel (Khmer text OK)

The app is in [`grocery/`](grocery/). It uses FastAPI with either:
- **Neon** (free online Postgres) — when `DATABASE_URL` is set, so the family can use it from anywhere, or
- a local **SQLite** file (`grocery.db`) — when running on one shop PC.

Daily backups go to **Google Drive** (or OneDrive) with `backup.py`.

## Option A — run on the shop PC

```bash
cd grocery
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 on the shop computer. API docs: http://localhost:8000/docs

**Easiest setup:** a cheap USB barcode scanner plugged into the shop PC. It types the
number and presses Enter, so the price shows straight away.

## Using phones on the shop Wi-Fi (camera scanning)

Phone browsers only allow the camera on `https://` pages, so start the app with:

```bash
python run_https.py
```

It prints an address like `https://192.168.1.20:8443`. Open it on any phone connected
to the **same Wi-Fi**. The first time, the phone warns "connection not private" (the
certificate is made by your own PC):
- **Android Chrome:** *Advanced* → *Proceed to 192.168.1.20 (unsafe)*
- **iPhone Safari:** *Show Details* → *visit this website*

Tips: tap **Add to Home Screen** to use it like an app. If the phone cannot connect, allow
Python through the Windows Firewall (Windows asks the first time — tick *Private networks*).
The camera scanner library loads from the internet (unpkg.com).

## Option B — put it online (free): Neon + Render

**1. Create the database on Neon**
1. Sign up at https://neon.com (free plan) and create a project, region **Asia Pacific (Singapore)** (closest to Cambodia).
2. Copy the **connection string** — it looks like
   `postgresql://user:password@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   Keep it secret: it is the key to your data.
3. To make the free hours last: *Branches → main → Compute → Edit* and set size to **0.25 CU**
   (min and max) and keep "Scale to zero" **on**.

The app creates its tables by itself the first time it starts.

**2. Run the app on Render**
1. Sign up at https://render.com with your GitHub account.
2. *New + → Blueprint* → choose this repository (it reads [`render.yaml`](render.yaml)).
3. When asked, fill in:
   - `DATABASE_URL` = the Neon connection string
   - `APP_PASSWORD` = a password for the shop (the login box accepts any username, e.g. `shop`)
4. After a few minutes you get a link like `https://grocery-price-tracker.onrender.com`.
   Open it on any phone — the camera scanner works because the link is `https://`.

Free plans sleep when not used: the first scan after a quiet period may take ~30–60 s;
after that it is fast.

**Test with Neon from the shop PC** (optional): set the variable before starting:
```bash
export DATABASE_URL="postgresql://...neon.tech/neondb?sslmode=require"   # Windows: set DATABASE_URL=...
export APP_PASSWORD="your-password"
uvicorn main:app --port 8000
```

## Daily backup to Google Drive

1. Install **Google Drive for desktop** on the shop PC and sign in. Your Drive shows up
   as a drive, e.g. `G:\My Drive`. Create a folder `Grocery backups` in it.
2. Run once to test (Python only, nothing to install):
   ```bash
   cd grocery
   python backup.py --url https://YOUR-APP.onrender.com --password YOUR_APP_PASSWORD --folder "G:\My Drive\Grocery backups"
   ```
   Each run saves `grocery-backup-YYYY-MM-DD.json` (full backup) and
   `grocery-prices-YYYY-MM-DD.csv` (opens in Excel). The last 60 days are kept.
   For Option A use `--url http://localhost:8000`.
3. Make it automatic every evening (Windows):
   - copy `backup-to-drive.example.bat` to `backup-to-drive.bat` and fill in the URL, password and folder
   - open Command Prompt and run (change the path to where the app is):
     ```
     schtasks /create /tn "Grocery backup" /sc daily /st 21:00 /tr "C:\grocery\backup-to-drive.bat"
     ```
   - check `backup-log.txt` next to the .bat file if something goes wrong.

OneDrive works the same way — just use a folder inside your OneDrive.

**Restore** (replaces all data with the backup; it asks you to type YES):
```bash
python backup.py --url https://YOUR-APP.onrender.com --password YOUR_APP_PASSWORD --restore "G:\My Drive\Grocery backups\grocery-backup-2026-11-08.json"
```

The Drive backup is also your safety net if Neon ever pauses the database or you
move to another provider: restore the latest file into the new app.

## Tests

```bash
cd grocery && pytest -q
# also test against Postgres:
TEST_DATABASE_URL=postgresql://user@localhost/grocery_test pytest -q
```
