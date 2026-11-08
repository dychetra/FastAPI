"""Back up the grocery app to a folder synced by Google Drive (or OneDrive).

Run this on the shop PC once a day. It downloads ALL data from the app
(items, prices, price history, exchange rate) and saves two dated files:

    grocery-backup-2026-11-08.json   full backup, can be restored
    grocery-prices-2026-11-08.csv    price list that opens in Excel

into the folder you choose. Put that folder inside Google Drive for desktop
(e.g. "G:\\My Drive\\Grocery backups") and Google uploads it automatically.
Old backups beyond --keep (default 60) are deleted.

Only Python's standard library is needed; nothing to install.

Examples
--------
  python backup.py --url https://my-shop.onrender.com --password 1234 ^
                   --folder "G:\\My Drive\\Grocery backups"

  # restore a backup (REPLACES all data in the app):
  python backup.py --url ... --password ... --restore grocery-backup-2026-11-08.json

You can also put the settings in environment variables instead of options:
GROCERY_URL, APP_PASSWORD, BACKUP_FOLDER.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path


def call(url: str, password: str, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if password:
        token = base64.b64encode(f"shop:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    # Neon / Render free plans may be asleep: allow time to wake up.
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def do_backup(base: str, password: str, folder: Path, keep: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()

    raw = call(f"{base}/api/backup", password)
    data = json.loads(raw)  # make sure it is valid before saving
    json_path = folder / f"grocery-backup-{stamp}.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    csv_path = folder / f"grocery-prices-{stamp}.csv"
    csv_path.write_bytes(call(f"{base}/api/export.csv", password))

    print(f"Saved {len(data['items'])} items, {len(data['history'])} price changes")
    print(f"  {json_path}\n  {csv_path}")

    for pattern in ("grocery-backup-*.json", "grocery-prices-*.csv"):
        old = sorted(folder.glob(pattern))[:-keep] if keep > 0 else []
        for f in old:
            f.unlink()
            print(f"  removed old backup {f.name}")


def do_restore(base: str, password: str, file: Path) -> None:
    data = json.loads(file.read_text(encoding="utf-8"))
    n = len(data.get("items", []))
    answer = input(f"This REPLACES all data in {base} with {n} items from {file.name}. "
                   "Type YES to continue: ")
    if answer.strip() != "YES":
        print("Cancelled.")
        return
    body = json.dumps(data).encode("utf-8")
    print(call(f"{base}/api/restore?confirm=true", password, body).decode())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("GROCERY_URL", "http://localhost:8000"),
                    help="address of the app (default http://localhost:8000)")
    ap.add_argument("--password", default=os.environ.get("APP_PASSWORD", ""),
                    help="the app's APP_PASSWORD, if set")
    ap.add_argument("--folder", default=os.environ.get("BACKUP_FOLDER", "backups"),
                    help="where to save backups (use a Google Drive folder)")
    ap.add_argument("--keep", type=int, default=60, help="how many daily backups to keep")
    ap.add_argument("--restore", metavar="FILE", help="restore this backup .json file")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    try:
        if a.restore:
            do_restore(base, a.password, Path(a.restore))
        else:
            do_backup(base, a.password, Path(a.folder), a.keep)
    except urllib.error.HTTPError as e:
        print(f"ERROR {e.code} from the app: {e.read().decode(errors='replace')[:300]}",
              file=sys.stderr)
        if e.code == 401:
            print("Check the --password.", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach {base} ({e.reason}). Is the app running / online?",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
