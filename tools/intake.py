#!/usr/bin/env python3
"""The inbox: listings that another tool (Dots) saw on marketplaces and drops as files into a folder on the VPS, to be read by the collectors.

The collectors never open these marketplaces themselves; they read files. Nothing in a file is trusted: every row goes through the checks below and
then through the same identity check, language check, dedupe, outlier rule and statistics as a listing read from toco. A row that fails a check
is reported and skipped, never repaired by guessing. See docs/inbox.md for the format that the dropping tool must write.

  folder       /opt/egs-raw/inbox  (EGS_INBOX, or --inbox); files *.json, *.csv, *.tsv; other names are ignored
  a row        url, judul, harga, jenis (raw or slab), seen_at and optionally toko, foto, stok, deskripsi, kota, collector
  freshness    seen_at is when the tool saw the listing: a RAW row counts for max-age-hours (36) after it, a slab row for 48 hours, then it is shown
               as old and not counted, exactly like a toco listing nobody has checked. A file may stay in the folder and be read again and again:
               a row that was already taken (same address, same or older seen_at) changes nothing.
  files        written to a temporary name (.tmp, .part, or a leading dot) and renamed when complete; a file changed in the last
               20 seconds is left for the next run, so a half written file is never read.

Standard library only.
"""
import csv
import ipaddress
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

DEFAULT_FOLDER = "/opt/egs-raw/inbox"
SOURCE_HOSTS = (("toco.id", "toco"), ("shopee.co.id", "shopee"), ("shopee.com", "shopee"), ("shp.ee", "shopee"),
                ("tokopedia.com", "tokopedia"), ("tokopedia.link", "tokopedia"))
# Marketplaces whose addresses are accepted in a file. cardtell.id and pokepedia.id are NOT here: Cardtell's terms require written permission
# (Pasal 13.2) and Pokepedia blocks automated access; add a host only with the site's permission.
ALLOWED_HOSTS = ("toco.id", "shopee.co.id", "shopee.com", "shp.ee", "tokopedia.com", "tokopedia.link", "tiktok.com", "bukalapak.com", "lazada.co.id",
                 "carousell.co.id", "olx.co.id", "kaskus.co.id", "blibli.com", "facebook.com", "fb.com", "instagram.com")
COLUMNS = {"url": ("url", "link", "tautan", "alamat"), "title": ("title", "judul", "nama"), "price": ("price", "harga", "ask"),
           "seller": ("seller", "toko", "penjual", "store"), "photo": ("photo", "foto", "image", "gambar"),
           "stock": ("stock", "stok"), "description": ("description", "deskripsi", "keterangan"), "city": ("city", "kota", "lokasi"),
           "kind": ("jenis", "kind", "type", "tipe"), "seen_at": ("seen_at", "dilihat", "waktu", "observed_at", "diambil"),
           "collector": ("collector", "pengumpul")}
KINDS = {"raw": "raw", "kartu raw": "raw", "slab": "slab", "egs": "slab", "slab egs": "slab", "psa": "slab", "graded": "slab"}
MIN_PRICE, MAX_PRICE = 500, 2_000_000_000
MAX_TITLE, MAX_ROWS, MAX_FILE_BYTES = 400, 50_000, 20_000_000
SETTLE_SECONDS = 20          # a file changed more recently than this is still being written
MAX_PROBLEMS = 200


def source_of(url):
    """Where a listing lives, from its address: toco, shopee, tokopedia, or the host name."""
    host = (urlsplit(url).hostname or "").lower()
    for h, name in SOURCE_HOSTS:
        if host == h or host.endswith("." + h):
            return name
    return host or "lain"


def host_allowed(url):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def parse_rupiah(text):
    """'Rp1.250.000', '1.250.000', '1250000', 1250000 -> 1250000; None for anything else (a decimal comma means the number is not a rupiah price)."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text) if text > 0 else None
    t = re.sub(r"(?i)rp\.?|idr|\s", "", str(text or ""))
    if not re.fullmatch(r"\d{1,3}(\.\d{3})+|\d+", t):
        return None
    value = int(t.replace(".", ""))
    return value if value > 0 else None


def clean_url(url):
    """The address without tracking parameters and fragments: the same page must not become two listings. None unless http(s)."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path}".rstrip("/")


def public_host(url):
    """True for an address that names a public host: a dotted name or a public IP, never localhost, a bare name or a private, loopback or link-local address.
    The collectors download pictures from the addresses in a file, so a picture address must not be able to point at this machine or its network."""
    host = (urlsplit(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return ip.is_global


def parse_time(value):
    """(datetime, None) for an ISO 8601 time with a zone ('2026-10-09T18:30:00+07:00', '...Z'); (None, problem) otherwise. A time without a zone is refused."""
    text = str(value or "").strip()
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None, f"waktu '{text}' bukan ISO 8601 (contoh 2026-10-09T18:30:00+07:00)"
    if t.tzinfo is None:
        return None, f"waktu '{text}' tanpa zona: tulis +07:00 (WIB) atau Z (UTC)"
    return t.astimezone(timezone.utc), None


def read_rows(path):
    """([dict of raw cells], problem or None) from one file."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return [], f"{os.path.basename(path)}: lebih dari {MAX_FILE_BYTES // 1_000_000} MB, dilewati"
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return [], f"{os.path.basename(path)}: tidak terbaca ({e})"
    if text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
        except ValueError as e:
            return [], f"{os.path.basename(path)}: JSON tidak valid ({e})"
        rows = data.get("listings") if isinstance(data, dict) else data
        rows = [r for r in rows or [] if isinstance(r, dict)]
    else:
        first = text.splitlines()[0] if text.strip() else ""
        delimiter = max(",;\t", key=first.count)
        rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    if len(rows) > MAX_ROWS:
        return [], f"{os.path.basename(path)}: {len(rows)} baris, lebih dari {MAX_ROWS}, dilewati"
    return rows, None


def normalize(raw, file_time, name):
    """(row, problem) for one row of a file. row: url, title, price, seller, photo, stock, description, city, kind, seen_at, collector, source."""
    names = {str(k).strip().lower(): v for k, v in raw.items() if k}
    cell = {key: next((names[a] for a in aliases if names.get(a) not in (None, "")), None) for key, aliases in COLUMNS.items()}
    url = clean_url(cell["url"])
    if not url:
        return None, f"{name}: alamat '{str(cell['url'])[:60]}' harus diawali http:// atau https://"
    where = f"{name}: {url[-50:]}"
    if not host_allowed(url):
        return None, f"{where}: situs {urlsplit(url).hostname} tidak diterima di folder masuk"
    title = str(cell["title"] or "").strip()
    if not title:
        return None, f"{where}: judul kosong"
    if len(title) > MAX_TITLE:
        return None, f"{where}: judul lebih dari {MAX_TITLE} karakter"
    price = parse_rupiah(cell["price"])
    if not price or not MIN_PRICE <= price <= MAX_PRICE:
        return None, f"{where}: harga '{cell['price']}' bukan rupiah utuh antara Rp{MIN_PRICE} dan Rp{MAX_PRICE:,}".replace(",", ".")
    kind = KINDS.get(str(cell["kind"] or "").strip().lower())
    if not kind:
        return None, f"{where}: jenis '{cell['kind']}' harus raw atau slab"
    try:
        stock = int(str(cell["stock"] if cell["stock"] is not None else "1").strip())
    except ValueError:
        return None, f"{where}: stok '{cell['stock']}' bukan angka"
    if stock < 0:
        return None, f"{where}: stok negatif"
    if cell["seen_at"] is None:
        seen = file_time
    else:
        seen, problem = parse_time(cell["seen_at"])
        if problem:
            return None, f"{where}: {problem}"
    photo = clean_url(cell["photo"]) if cell["photo"] else None
    if photo and not public_host(photo):
        photo = None      # the row stays, the picture does not
    seller = str(cell["seller"] or "").strip() or None
    return {"url": url, "title": title, "price": price, "seller": seller[:80] if seller else None, "photo": photo, "stock": stock,
            "description": str(cell["description"] or "").strip()[:600], "city": str(cell["city"] or "").strip()[:60] or None, "kind": kind,
            "seen_at": seen, "collector": str(cell["collector"] or "").strip()[:40] or None, "source": source_of(url)}, None


def load(folder, kind, now, max_age_hours, settle_seconds=SETTLE_SECONDS):
    """(rows, problems, info) for the files in the folder: the rows of this kind ('raw' or 'slab') that are not older than max_age_hours, newest
    sighting per address. A folder that does not exist is an empty inbox. problems are lines for the log and the report; info are the counts."""
    info = {"folder": folder, "files": 0, "skipped_files": 0, "rows": 0, "taken": 0, "other_kind": 0, "expired": 0, "bad": 0, "sources": {}}
    if not folder or not os.path.isdir(folder):
        return [], [], info
    problems, best = [], {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if (not os.path.isfile(path) or name.startswith(".") or name.lower().endswith((".tmp", ".part"))
                or not name.lower().endswith((".json", ".csv", ".tsv"))):
            continue
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if now.timestamp() - modified < settle_seconds:
            info["skipped_files"] += 1
            problems.append(f"{name}: masih berubah dalam {settle_seconds} detik terakhir, dibaca di putaran berikutnya")
            continue
        info["files"] += 1
        rows, problem = read_rows(path)
        if problem:
            problems.append(problem)
            continue
        if not rows:
            problems.append(f"{name}: tidak ada baris atau kolomnya tidak dikenal (butuh url, judul, harga, jenis)")
        for i, raw in enumerate(rows, 1):
            info["rows"] += 1
            row, problem = normalize(raw, datetime.fromtimestamp(modified, timezone.utc), f"{name} baris {i}")
            if problem:
                info["bad"] += 1
                problems.append(problem)
                continue
            if row["seen_at"] > now + timedelta(minutes=5):
                info["bad"] += 1
                problems.append(f"{name} baris {i}: waktu {row['seen_at'].isoformat()} ada di masa depan")
                continue
            if row["kind"] != kind:
                info["other_kind"] += 1
                continue
            if now - row["seen_at"] > timedelta(hours=max_age_hours):
                info["expired"] += 1
                continue
            old = best.get(row["url"])
            if old is None or row["seen_at"] >= old["seen_at"]:
                best[row["url"]] = row
    rows = sorted(best.values(), key=lambda r: (r["seen_at"], r["url"]))
    info["taken"] = len(rows)
    for r in rows:
        info["sources"][r["source"]] = info["sources"].get(r["source"], 0) + 1
    return rows, problems[:MAX_PROBLEMS], info


def write_report(folder, consumer, now, info, problems):
    """status/laporan-<consumer>.txt in the folder: what this run took and why rows were refused, for the tool that drops the files. Silent if the folder is read only."""
    lines = [f"laporan {consumer} {now.astimezone(timezone(timedelta(hours=7))):%Y-%m-%d %H:%M} WIB",
             f"file dibaca: {info['files']} (ditunda karena masih berubah: {info['skipped_files']})",
             f"baris: {info['rows']} | diambil untuk {consumer}: {info['taken']} | jenis lain: {info['other_kind']} | kedaluwarsa: {info['expired']} | ditolak: {info['bad']}",
             "sumber: " + (", ".join(f"{k} {v}" for k, v in sorted(info["sources"].items())) or "-")]
    lines += ["", "baris yang ditolak:" if problems else "tidak ada baris yang ditolak"] + [f"  {p}" for p in problems]
    try:
        os.makedirs(os.path.join(folder, "status"), exist_ok=True)
        path = os.path.join(folder, "status", f"laporan-{consumer}.txt")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(path + ".tmp", path)
    except OSError:
        pass
