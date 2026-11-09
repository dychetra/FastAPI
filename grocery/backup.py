"""Back up ScanPrice to a folder synced by OneDrive (or Google Drive).

Run this on the shop PC once a day. It downloads ALL data from the app
(items, prices, price history, exchange rate) and saves two dated files:

    grocery-backup-2026-11-08.json   full backup, can be restored
    grocery-prices-2026-11-08.csv    price list that opens in Excel
    photos/8851959132012 Coca Cola 325 ml.jpg   one file per product photo

into the folder you choose. Put that folder inside OneDrive
(e.g. "C:\\Users\\You\\OneDrive\\ScanPrice backups") or Google Drive and it is
uploaded automatically. Old backups beyond --keep (default 60) are deleted.
Photos are only downloaded when new or changed, and removed when the product
or its photo is deleted in the app.

Only Python's standard library is needed; nothing to install.

Examples
--------
  python backup.py --url https://scan.gechheng.shop --password 1234 ^
                   --folder "C:\\Users\\You\\OneDrive\\ScanPrice backups"

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
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path


def call(url: str, password: str, data: bytes | None = None, method: str = "",
         content_type: str = "application/json") -> bytes:
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("User-Agent", "ScanPrice-backup/1.7")
    if data:
        req.add_header("Content-Type", content_type)
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

    print(f"Saved {len(data['items'])} items, {len(data['history'])} price changes, "
          f"{len(data.get('sales') or [])} sales")
    print(f"  {json_path}\n  {csv_path}")
    sync_photos(base, password, folder / "photos")

    for pattern in ("grocery-backup-*.json", "grocery-prices-*.csv"):
        old = sorted(folder.glob(pattern))[:-keep] if keep > 0 else []
        for f in old:
            f.unlink()
            print(f"  removed old backup {f.name}")


PHOTO_EXT = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


def safe_name(text: str) -> str:
    """File-name-safe text (Windows forbids \\ / : * ? " < > |)."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text).strip().rstrip(".")[:100]


def sync_photos(base: str, password: str, pdir: Path) -> None:
    """Mirror the app's product photos into pdir as '<barcode> <name>.jpg'."""
    try:
        photos = json.loads(call(f"{base}/api/photos", password))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # app older than v1.7: no photos yet
            return
        raise
    pdir.mkdir(parents=True, exist_ok=True)
    index_path = pdir / "index.json"
    try:
        old = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    new, fetched, failed = {}, 0, 0
    for p in photos:
        code, prev = p["barcode"], old.get(p["barcode"])
        stem = safe_name(f"{code} {p['name']}")
        if prev and prev.get("updated_at") == p["updated_at"] and (pdir / prev["file"]).exists():
            if not prev["file"].startswith(stem + "."):  # product renamed: rename file
                renamed = stem + Path(prev["file"]).suffix
                (pdir / prev["file"]).replace(pdir / renamed)
                prev = {**prev, "file": renamed}
            new[code] = prev
            continue
        url = p["url"] if p["url"].startswith("http") else base + p["url"]
        try:  # online photos (Open Food Facts) do not get our password
            img = call(url, password if url.startswith(base) else "")
        except (urllib.error.URLError, OSError):
            failed += 1
            if prev:
                new[code] = prev
            continue
        ext = next((e for magic, e in PHOTO_EXT.items() if img.startswith(magic)), ".jpg")
        (pdir / (stem + ext)).write_bytes(img)
        if prev and prev["file"] != stem + ext:
            (pdir / prev["file"]).unlink(missing_ok=True)
        new[code] = {"file": stem + ext, "updated_at": p["updated_at"], "kind": p["kind"]}
        fetched += 1
    removed = 0
    for code, prev in old.items():
        if code not in new:
            (pdir / prev["file"]).unlink(missing_ok=True)
            removed += 1
    index_path.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Photos: {len(new)} in folder ({fetched} new/changed, {removed} removed"
          + (f", {failed} could not be downloaded - will retry next time" if failed else "")
          + f")\n  {pdir}")


def restore_photos(base: str, password: str, pdir: Path) -> None:
    try:
        index = json.loads((pdir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    ok = 0
    for code, info in index.items():
        f = pdir / info["file"]
        if not f.exists() or f.stat().st_size > 300_000:
            continue
        try:
            call(f"{base}/api/items/{code}/photo", password, f.read_bytes(),
                 method="PUT", content_type="image/jpeg")
            ok += 1
        except urllib.error.HTTPError:
            pass  # product not in this backup
    print(f"Restored {ok} photos")


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
    restore_photos(base, password, file.parent / "photos")


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
