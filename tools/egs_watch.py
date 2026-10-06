#!/usr/bin/env python3
"""Watch EGS-graded Pokemon slab listings on toco: discover, verify identity, keep the evidence, export egs_watch.json.

Runs on the VPS next to the other bots and touches none of their files: it has its own SQLite database and its own
output file (docs/egs-watch.md). Public pages only; it stops when the site answers with a block, a login or a
captcha, and never works around one.

What a run does
  1. discovery   toco's own search (the request the website makes), many queries (tools/egs_hits.json: EGS, slab EGS,
                 EGS 10/9.5/9, every hit card name, every tracked card), each paged until the results run out, a page
                 repeats, or the page stops being about EGS. Request budget, a delay, a cache, graded retries.
  2. identity    tools/egs_identity.py: company, grade, Indonesian language, set, number, rarity and name, all checked
                 against the official catalog. Only consistent evidence is accepted onto the watchlist; the doubtful
                 stay in a review queue with photo and reasons and are in no price. Nothing is filled in by guessing.
  3. evidence    every observation (price, stock, time, source URL, photo) is kept; a listing that is unavailable,
                 one that could not be checked, and one that dropped out of the search results are three different
                 statuses, and none of them is a sale.
  4. statistics  per card and grade: listings, unique sellers, mean, median, range. Repeated offers (same certificate,
                 or the same seller with the same card and grade) count once. One listing is shown as "data terbatas".
  5. export      egs_watch.json (+ a text report). A run that could not reach the site never overwrites the last good file.

Usage
  python3 tools/egs_watch.py --dry-run                       # read-only: discover and judge, print the report
  python3 tools/egs_watch.py --db /opt/egs-raw/egs_watch.sqlite --out /opt/egs-raw/egs_watch.json
  python3 tools/egs_watch.py --report /opt/egs-raw/egs_watch.json     # print the report of an export
  python3 tools/egs_watch.py --validate egs_watch.json
Standard library only (a vision reader for photos is optional: --photo-mode openai|anthropic).
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_raw  # noqa: E402
import egs_identity as ident  # noqa: E402
import egs_worksheet  # noqa: E402
import watch_queries  # noqa: E402

SITE = collect_raw.SITE
UA = collect_raw.UA
WIB = timezone(timedelta(hours=7))
EGS_WORD = re.compile(r"\begs(?![a-z])|\be\.g\.s\b", re.I)
RETRY_WAITS = (3, 8, 20)        # seconds before the 2nd, 3rd and 4th try of one request
CACHE_TTL = 20 * 60             # a search page is reused for 20 minutes
RECHECK_DAYS = 45               # a listing not seen for this long is no longer looked up one by one
COUNT_STALE_HOURS = 48          # a listing whose last good check is older than this is shown but not counted
LIMITED_BELOW = 3               # fewer listings than this: "data terbatas"
OUTLIER_LOW, OUTLIER_HIGH = 0.25, 4.0   # against the median of the other offers of the same card and grade
PHOTO_PROMPT = (
    "This is a marketplace photo of a graded Pokemon card slab. Read only what is visible; use null when you cannot read "
    "something and never guess. Answer with one JSON object and nothing else, with these keys: "
    "is_slab (true or false), company (grading company on the label, e.g. EGS, PSA, BGS, CGC, or null), "
    "grade (the grade on the label, e.g. \"10\" or \"9.5\", or null), cert (certificate number on the label, or null), "
    "card_name (or null), number (printed card number such as \"234/193\", or null), rarity (printed rarity code such as SAR, or null), "
    "language (language of the text printed on the card: ID, JP, EN, OTHER or UNCLEAR).")


class Blocked(Exception):
    """The site answered with something that is not the data: a block, a login or a captcha. Never worked around."""


class SearchFailed(Exception):
    pass


# ---- search ----------------------------------------------------------------------

def make_post(url=None, timeout=30):
    url = url or (SITE + "/api/toco-api/search")

    def post(body):
        req = Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA,
            "Origin": SITE, "Referer": SITE + "/search"})
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.status, r.read(8_000_000).decode("utf-8", "replace"), r.headers.get("Retry-After")
        except HTTPError as e:
            return e.code, e.read(3000).decode("utf-8", "replace"), e.headers.get("Retry-After") if e.headers else None
        except Exception as e:  # network, timeout
            return 0, f"{type(e).__name__}: {e}", None
    return post


def fetch_status(url, timeout=20):
    """HTTP status of a listing page (0 when it could not be reached). 404 and 410 mean the listing is gone."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urlopen(req, timeout=timeout) as r:
            r.read(2000)
            return r.status
    except HTTPError as e:
        return e.code
    except Exception:
        return 0


BLOCK_MARKERS = re.compile(r"captcha|are you a robot|verify you are human|cf-chl|just a moment|access denied|akses ditolak", re.I)


class Toco:
    """Search client: delay between requests is the caller's job; this adds cache, graded retries and block detection."""

    def __init__(self, post, cache_dir=None, ttl=CACHE_TTL, retries=RETRY_WAITS, sleep=time.sleep, log=print, clock=time.time):
        self.post, self.cache_dir, self.ttl, self.retries = post, cache_dir, ttl, retries
        self.sleep, self.log, self.clock = sleep, log, clock
        self.requests = self.ok = self.failed = self.cached = self.retried = 0
        self.blocked = None

    def _cache_path(self, body):
        key = hashlib.sha1(json.dumps({k: v for k, v in body["body"].items() if k != "aggregations"}, sort_keys=True).encode()).hexdigest()
        return os.path.join(self.cache_dir, key + ".json") if self.cache_dir else None

    def search(self, query, offset, size):
        body = collect_raw.search_body(query, offset, size)
        path = self._cache_path(body)
        if path and os.path.exists(path) and self.clock() - os.path.getmtime(path) < self.ttl:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.cached += 1
                return data
            except (OSError, ValueError):
                pass
        last, asked = "", 0
        for attempt, wait in enumerate((0,) + tuple(self.retries)):
            if wait:
                self.retried += 1
                self.sleep(max(wait, asked))   # the graded wait, or what the site asked for if that is longer
            status, text, retry_after = self.post(body)
            self.requests += 1
            if status in (401, 403, 451) or (status == 200 and BLOCK_MARKERS.search(text[:3000]) and not text.lstrip().startswith("{")):
                self.blocked = f"status {status}: pembatasan akses; tidak dilewati"
                raise Blocked(self.blocked)
            if status == 200:
                try:
                    data = json.loads(text)
                except ValueError:
                    self.blocked = "jawaban bukan JSON (mungkin halaman pembatasan); tidak dilewati"
                    raise Blocked(self.blocked)
                if isinstance(data, dict) and isinstance(data.get("results"), list):
                    self.ok += 1
                    if path:
                        os.makedirs(self.cache_dir, exist_ok=True)
                        with open(path + ".tmp", "w", encoding="utf-8") as fh:
                            json.dump(data, fh, ensure_ascii=False)
                        os.replace(path + ".tmp", path)
                    return data
                last = "jawaban tanpa daftar hasil"
            elif status == 429 or status >= 500 or status == 0:
                last = f"status {status}"
                asked = min(int(retry_after), 60) if retry_after and retry_after.isdigit() else 0
            else:
                last = f"status {status}"
                break
        self.failed += 1
        raise SearchFailed(f"{query!r} dari {offset}: {last}")


# ---- queries and discovery -----------------------------------------------------------

def load_config(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def title_of(search_text):
    """The listing title inside the bot's search_text ('Sisa 1 Arceus V SR AA S9 ID EGS 9.5 Rp600.000 Kota Jambi')."""
    t = re.sub(r"^Sisa \d+ ", "", (search_text or "").strip())
    return re.split(r"\s+Rp\s?\d", t)[0].strip()


def build_queries(cfg, tracked_types=(), accepted_names=(), seed_titles=()):
    """[(query, max pages)] in priority order, without repeats (case ignored)."""
    out, seen = [], set()

    def add(q, pages):
        k = q.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append((q.strip(), pages))
    for q in cfg["base_queries"]:
        add(q, cfg["base_pages"])
    for t in tracked_types:
        for q in watch_queries.queries(t)["egs"]:
            add(q, cfg["tracked_pages"])
    for name in cfg["hits"]:
        add(f"{name} EGS", cfg["hit_pages"])
        add(f"{name} slab EGS", cfg["hit_pages"])
    for name in cfg.get("deep_hits", []):
        for g in cfg["grades"][:3]:
            add(f"{name} EGS {g}", cfg["hit_pages"])
    for code in cfg.get("set_codes", []):
        add(f"{code} EGS", cfg["hit_pages"])
    for name in accepted_names:
        add(f"{name} EGS", cfg["tracked_pages"])
    for title in seed_titles:
        add(title, 1)
    return out


def discover(toco, queries, size, delay, budget, min_share, sleep=time.sleep, log=print):
    """Run the queries page by page. Returns ({slug: item}, [per query diagnostics], stopped_by_block)."""
    items, diag, start, blocked = {}, [], toco.requests, False
    for q, pages in queries:
        seen, page, total, why = set(), 0, None, "halaman maksimal"
        while page < pages:
            if toco.requests - start >= budget:
                why = "anggaran permintaan habis"
                break
            try:
                data = toco.search(q, page * size, size)
            except Blocked as e:
                log(f"  ! {e}")
                why, blocked = "dibatasi", True
                break
            except SearchFailed as e:
                log(f"  ! pencarian gagal: {e}")
                why = "gagal"
                break
            results = data.get("results") or []
            total = data.get("total", total)
            new = [r for r in results if r.get("slug") and r["slug"] not in seen]
            seen.update(r["slug"] for r in new)
            for r in new:
                items.setdefault(r["slug"], r)
            page += 1
            share = (sum(1 for r in results if EGS_WORD.search(f"{r.get('title') or ''} {r.get('description') or ''}")) / len(results)) if results else 0
            if not results:
                why = "hasil habis"
                break
            if not new:
                why = "halaman berulang"
                break
            if total is not None and page * size >= total:
                why = "hasil habis"
                break
            if page >= 1 and share < min_share and not (q.lower().strip() in ("egs",) and page < 2):
                why = "halaman tidak lagi tentang EGS"
                break
            sleep(delay)
        diag.append({"query": q, "pages": page, "listings": len(seen), "total": total, "stopped": why})
        if blocked or toco.requests - start >= budget:
            break
    return items, diag, blocked


# ---- the database --------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS listing(
  url TEXT PRIMARY KEY, slug TEXT, store_id TEXT, store_name TEXT, city TEXT, title TEXT, description TEXT,
  photo TEXT, photos TEXT, ask INTEGER, list_price INTEGER, stock INTEGER, active INTEGER,
  first_seen TEXT, last_seen TEXT, last_ok TEXT, status TEXT, status_at TEXT,
  verdict TEXT, reasons TEXT, identity TEXT, evidence TEXT, suggestions TEXT, origin TEXT,
  cert TEXT, card_key TEXT, grade TEXT, photo_read TEXT, listed_at TEXT, updated_at_src TEXT);
CREATE TABLE IF NOT EXISTS observation(url TEXT, observed_at TEXT, ask INTEGER, stock INTEGER, status TEXT, PRIMARY KEY(url, observed_at));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, ok INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def open_db(path, copy_only=False):
    """The database; copy_only=True works on an in-memory copy so that nothing is written (dry run)."""
    if copy_only:
        db = sqlite3.connect(":memory:")
        if path and os.path.exists(path):
            src = sqlite3.connect(path)
            src.backup(db)
            src.close()
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def meta_get(db, key):
    r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def meta_set(db, key, value):
    db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def slug_of(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def seed(db, latest, now, log=print):
    """Import the bot's watchlist from latest.json once: the listings with their daily history. Returns the pending candidate titles."""
    if meta_get(db, "seeded"):
        return []
    for l in latest.get("listings", []):
        daily = sorted(l.get("daily_latest") or [], key=lambda d: d["observed_at"])
        newest = l["latest"]
        identity = {"company": "EGS", "grade": l["grade"], "cert": None, "language": "ID", "language_by": "dipantau bot",
                    "name": l["name"], "set_code": l["set_code"], "card_number": l["card_number"], "rarity": l["rarity"] or None, "variant": ""}
        db.execute(
            "INSERT OR IGNORE INTO listing(url, slug, title, ask, stock, active, first_seen, last_seen, last_ok, status, status_at, verdict, reasons, identity, "
            "evidence, suggestions, origin, card_key, grade) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (l["url"], slug_of(l["url"]), f"{l['name']} {l['card_number']} {l['rarity']} EGS {l['grade']}", newest["asking_idr"],
             1 if newest["in_stock"] else 0, 1 if newest["in_stock"] else 0,
             (daily[0] if daily else newest)["observed_at"], newest["observed_at"], newest["observed_at"], "ok", newest["observed_at"],
             "accepted", "[]", json.dumps(identity, ensure_ascii=False), "{}", "[]", "bot", ident.card_key(identity), l["grade"]))
        for d in daily or [newest]:
            db.execute("INSERT OR IGNORE INTO observation(url, observed_at, ask, stock, status) VALUES(?,?,?,?,?)",
                       (l["url"], d["observed_at"], d["asking_idr"], 1 if d["in_stock"] else 0, "ok"))
    meta_set(db, "seeded", now.isoformat())
    db.commit()
    titles = [title_of(c["search_text"]) for c in (latest.get("discovery") or {}).get("candidates", []) if not c.get("tracked")]
    log(f"data awal diimpor dari latest.json: {len(latest.get('listings', []))} listing dipantau, {len(titles)} kandidat menunggu")
    return [t for t in titles if t]


def normalize(item):
    """A toco search result as one flat record (the page address uses the /id/listing/ form the bot already uses)."""
    base = collect_raw.to_listing(item)
    store = item.get("store") or {}
    photos = [p for p in (item.get("photos") or []) if isinstance(p, str)][:6]
    return {**base, "slug": item.get("slug"), "url": f"{SITE}/id/listing/{item.get('slug')}",
            "store_id": item.get("store_id") or store.get("id"), "store_name": item.get("store_name") or store.get("toco_name"),
            "photos": photos, "listed_at": item.get("created_at"), "updated_at_src": item.get("updated_at"),
            "description": base["description"][:600], "active": bool(base["stock"] and base["stock"] > 0)}


# ---- listings the owner collected by hand (Shopee, Tokopedia, anything else) ------------------------
# The bot never opens these sites: a person looks at a listing, writes down what is on the page, and this file goes through the same
# identity check, dedupe and statistics as a toco listing. Nothing here logs in, solves a challenge or reads another seller's pages.

SOURCE_HOSTS = (("toco.id", "toco"), ("shopee.co.id", "shopee"), ("shopee.com", "shopee"), ("shp.ee", "shopee"),
                ("tokopedia.com", "tokopedia"), ("tokopedia.link", "tokopedia"))
IMPORT_COLUMNS = {"url": ("url", "link", "tautan", "alamat"), "title": ("title", "judul", "nama"), "price": ("price", "harga", "ask"),
                  "seller": ("seller", "toko", "penjual", "store"), "photo": ("photo", "foto", "image", "gambar"),
                  "stock": ("stock", "stok"), "description": ("description", "deskripsi", "keterangan"), "city": ("city", "kota", "lokasi")}


def source_of(url):
    """Where a listing lives, from its address: toco, shopee, tokopedia, or the host name."""
    host = (urlsplit(url).hostname or "").lower()
    for h, name in SOURCE_HOSTS:
        if host == h or host.endswith("." + h):
            return name
    return host or "lain"


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
    """The address without tracking parameters and fragments: the same page must not become two listings."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path}".rstrip("/")


def normalize_import(row):
    """One hand-collected row (already keyed by the names in IMPORT_COLUMNS) as the flat record normalize() makes; (record, None) or (None, problem)."""
    url = clean_url(row.get("url"))
    if not url:
        return None, "alamat (url) harus diawali http:// atau https://"
    title = str(row.get("title") or "").strip()
    if not title:
        return None, f"{url[-50:]}: judul kosong"
    ask = parse_rupiah(row.get("price"))
    if not ask:
        return None, f"{url[-50:]}: harga '{row.get('price')}' bukan angka rupiah"
    try:
        stock = int(str(row.get("stock") or "1").strip())
    except ValueError:
        return None, f"{url[-50:]}: stok '{row.get('stock')}' bukan angka"
    seller = str(row.get("seller") or "").strip() or None
    photo = clean_url(row.get("photo")) if row.get("photo") else None
    source = source_of(url)
    return {"url": url, "slug": slug_of(url), "title": title, "description": str(row.get("description") or "").strip()[:600],
            "ask": ask, "list": None, "city": str(row.get("city") or "").strip() or "?", "stock": max(stock, 0), "image": photo,
            "photos": [photo] if photo else [], "store_id": hashlib.sha1(f"{source}:{seller.lower()}".encode()).hexdigest()[:12] if seller else None,
            "store_name": seller, "listed_at": None, "updated_at_src": None, "active": stock > 0}, None


def read_import(path):
    """(records, problems) from a CSV (comma, semicolon or tab separated, first line = column names) or a JSON list. Bad rows are reported, never guessed."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as e:
        return [], [f"berkas impor tidak terbaca: {e}"]
    if text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
        except ValueError as e:
            return [], [f"JSON tidak valid: {e}"]
        rows = data.get("listings") if isinstance(data, dict) else data
        rows = [r for r in rows or [] if isinstance(r, dict)]
    else:
        first = text.splitlines()[0] if text.strip() else ""
        delimiter = max(",;\t", key=first.count)
        rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    records, problems, seen = [], [], {}
    for i, raw in enumerate(rows, 1):
        names = {str(k).strip().lower(): v for k, v in raw.items() if k}
        row = {key: next((names[a] for a in aliases if names.get(a) not in (None, "")), None) for key, aliases in IMPORT_COLUMNS.items()}
        rec, problem = normalize_import(row)
        if problem:
            problems.append(f"baris {i}: {problem}")
        else:
            seen[rec["url"]] = rec     # the same page twice in one file: the later line wins
    records = list(seen.values())
    if not rows:
        problems.append("berkas impor kosong atau kolomnya tidak dikenali (butuh: url, judul, harga)")
    return records, problems


def verdict_cols(v):
    """The columns that hold what the identity check concluded about a listing."""
    identity, verdict = v["identity"], v["verdict"]
    return {
        "verdict": verdict, "reasons": json.dumps(v["reasons"], ensure_ascii=False), "identity": json.dumps(identity, ensure_ascii=False),
        "evidence": json.dumps({k: v["evidence"][k] for k in ("companies", "grades_title", "grades_desc", "numbers", "rarities", "bundle", "holo")}, ensure_ascii=False),
        "suggestions": json.dumps(v["suggestions"], ensure_ascii=False), "cert": identity.get("cert"),
        "card_key": ident.card_key(identity) if verdict == "accepted" else None,
        "grade": identity.get("grade") if verdict == "accepted" else None}


def upsert(db, n, v, now, photo_read=None, origin="search"):
    """Store one listing seen in a search together with this run's verdict, and record the observation. True when it is new."""
    stamp = now.isoformat()
    status = "ok" if n["active"] else "unavailable"
    cols = {
        "slug": n["slug"], "store_id": n["store_id"], "store_name": n["store_name"], "city": n["city"], "title": n["title"],
        "description": n["description"], "photo": n["image"], "photos": json.dumps(n["photos"]), "ask": n["ask"], "list_price": n["list"],
        "stock": n["stock"], "active": 1 if n["active"] else 0, "last_seen": stamp, "last_ok": stamp, "status": status, "status_at": stamp,
        **verdict_cols(v), "listed_at": n["listed_at"], "updated_at_src": n["updated_at_src"]}
    if photo_read:
        cols["photo_read"] = json.dumps(photo_read, ensure_ascii=False)
    exists = db.execute("SELECT 1 FROM listing WHERE url=?", (n["url"],)).fetchone()
    if exists:
        db.execute("UPDATE listing SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE url=?", (*cols.values(), n["url"]))
    else:
        cols.update(url=n["url"], first_seen=stamp, origin=origin)
        db.execute("INSERT INTO listing(" + ", ".join(cols) + ") VALUES(" + ",".join("?" * len(cols)) + ")", tuple(cols.values()))
    if n["ask"]:
        db.execute("INSERT OR REPLACE INTO observation(url, observed_at, ask, stock, status) VALUES(?,?,?,?,?)", (n["url"], stamp, n["ask"], n["stock"], status))
    return not exists


def tracked_by_url(db):
    out = {}
    for r in db.execute("SELECT url, identity FROM listing WHERE origin='bot'"):
        i = json.loads(r["identity"])
        out[r["url"]] = {"name": i["name"], "set_code": i["set_code"], "card_number": i["card_number"], "rarity": i["rarity"], "grade": i["grade"]}
    return out


# ---- judging and re-checking ------------------------------------------------------------

NEEDS_PHOTO = {"number_missing", "language_unproven", "grade_missing", "no_egs"}


def judge(db, n, catalog, tracked, photo_fn, photo_budget):
    """Verdict of one listing; the photo is read only for an ambiguous one that a photo could settle, once per listing."""
    row = db.execute("SELECT photo_read FROM listing WHERE url=?", (n["url"],)).fetchone()
    cached = json.loads(row["photo_read"]) if row and row["photo_read"] else None
    # the address-versus-title check is about toco, whose address keeps the first title of a re-used listing; other sites' addresses are not evidence of that
    listing = {"title": n["title"], "description": n["description"], "ask": n["ask"], "stock": n["stock"], "slug": n["slug"] if source_of(n["url"]) == "toco" else None}
    v = ident.verify(listing, catalog, cached, tracked.get(n["url"]))
    if cached and cached.get("reject"):     # the owner threw this listing out in the worksheet: it stays out
        return {**v, "verdict": "rejected", "reasons": [ident.reason("manual_reject", "Ditolak manual lewat lembar kerja")]}, cached
    read = cached
    if (v["verdict"] == "ambiguous" and photo_fn and not cached and photo_budget[0] > 0 and n["image"]
            and NEEDS_PHOTO & {r["code"] for r in v["reasons"]}):
        photo_budget[0] -= 1
        read = photo_fn(n["image"])
        if read:
            v = ident.verify(listing, catalog, read, tracked.get(n["url"]))
    return v, read


def apply_manual(db, reads, catalog, tracked, log):
    """Apply what the owner filled in the worksheet to listings already in the database: store the reading like a photo reading
    (marked manual) and judge again. The check time of the listing is not touched: nobody looked at the listing page just now."""
    done = Counter()
    for url, read in reads.items():
        row = db.execute("SELECT * FROM listing WHERE url=?", (url,)).fetchone()
        if not row:
            done["unknown"] += 1
            log(f"  ! lembar kerja: listing tidak ada di database: {url[-60:]}")
            continue
        db.execute("UPDATE listing SET photo_read=? WHERE url=?", (json.dumps(read, ensure_ascii=False), url))
        n = {"title": row["title"], "description": row["description"] or "", "ask": row["ask"], "stock": row["stock"], "slug": row["slug"], "url": url, "image": row["photo"]}
        v, _ = judge(db, n, catalog, tracked, None, [0])
        cols = verdict_cols(v)
        db.execute("UPDATE listing SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE url=?", (*cols.values(), url))
        done[v["verdict"]] += 1
    db.commit()
    return done


def recheck_query(row):
    """What to search for to find one known listing again: its title, or for the bot's own listings (whose real title we never
    stored) the words of their page address, which are the title they were first posted with."""
    if row["origin"] == "bot":
        parts = slug_of(row["url"]).split("-")
        while parts and re.fullmatch(r"\d{10,}|[0-9a-f]{3,6}", parts[-1]) and len(parts) > 2:
            parts.pop()
        return " ".join(parts)
    return row["title"]


def recheck(db, toco, status_fn, found, catalog, tracked, photo_fn, photo_budget, now, limit, delay, sleep, log):
    """Look up, one by one, the listings the crawl did not return: tell unavailable, unreachable, gone and 'only missing from the
    search' apart. None of these is a sale. Returns counters; stops at the first block."""
    cutoff = (now - timedelta(days=RECHECK_DAYS)).isoformat()
    rows = [r for r in db.execute(
        "SELECT url, slug, title, status, origin FROM listing WHERE verdict IN ('accepted','ambiguous') AND status != 'gone' AND origin != 'import' "
        "AND COALESCE(last_seen, first_seen) >= ? ORDER BY (origin='bot') DESC, status_at", (cutoff,)) if r["slug"] not in found]
    count = Counter()
    for r in rows[:limit]:
        try:
            data = toco.search(recheck_query(r), 0, 45)
        except Blocked as e:
            log(f"  ! {e}")
            count["blocked"] += 1
            break
        except SearchFailed as e:
            db.execute("UPDATE listing SET status='fetch_failed', status_at=? WHERE url=?", (now.isoformat(), r["url"]))
            count["fetch_failed"] += 1
            log(f"  ! {e}")
            continue
        item = next((x for x in data.get("results") or [] if x.get("slug") == r["slug"]), None)
        if item:
            n = normalize(item)
            v, read = judge(db, n, catalog, tracked, photo_fn, photo_budget)
            upsert(db, n, v, now, read)
            count["ok" if n["active"] else "unavailable"] += 1
        else:
            code = status_fn(r["url"])
            status = "gone" if code in (404, 410) else "missing_from_search" if code == 200 else "fetch_failed"
            db.execute("UPDATE listing SET status=?, status_at=? WHERE url=?", (status, now.isoformat(), r["url"]))
            count[status] += 1
        sleep(delay)
    count["skipped"] = max(0, len(rows) - limit)
    db.commit()
    return count


# ---- statistics, dedupe and export -------------------------------------------------------

def wib_date(iso):
    return (datetime.fromisoformat(iso) + timedelta(hours=7)).date().isoformat()


def daily_history(observations):
    """Latest observation of each WIB day, newest first, in the shape latest.json uses."""
    by_day = {}
    for at, ask, stock in observations:
        by_day[wib_date(at)] = (at, ask, stock)
    return [{"date_wib": d, "observed_at": at, "asking_idr": ask, "in_stock": bool(stock)} for d, (at, ask, stock) in sorted(by_day.items(), reverse=True)]


def counts_now(r, now):
    """Does this accepted listing count in the prices right now?"""
    if not (r["verdict"] == "accepted" and r["card_key"] and r["grade"] and r["active"] and r["ask"]):
        return False
    if r["status"] in ("ok", "missing_from_search", "fetch_failed") and r["last_ok"]:   # whatever the status, the last good check must be recent
        return now - datetime.fromisoformat(r["last_ok"]) <= timedelta(hours=COUNT_STALE_HOURS)
    return False


def median_int(values):
    return int(round(statistics.median(values)))


def analyze(db, now):
    """Dedupe, outliers and statistics. Returns (rows by url with an 'excluded' mark, groups)."""
    rows = {}
    for r in db.execute("SELECT * FROM listing"):
        d = dict(r)
        d["reasons"], d["identity"] = json.loads(d["reasons"] or "[]"), json.loads(d["identity"] or "{}")
        d["suggestions"] = json.loads(d["suggestions"] or "[]")
        d["excluded"], d["counted"] = None, False
        rows[d["url"]] = d
    groups = defaultdict(list)
    for d in rows.values():
        if d["verdict"] == "accepted":
            if not counts_now(d, now):
                d["excluded"] = ("unavailable" if d["status"] == "unavailable" or not d["active"] else
                                "stale" if d["status"] == "ok" else "status:" + str(d["status"]))
            elif d["card_key"] and d["grade"]:
                groups[(d["card_key"], d["grade"])].append(d)
    out = {}
    for key, members in groups.items():
        # One certificate is one slab. A seller's listing without a certificate next to one with a certificate (same card and grade) is taken
        # for the same slab; sellers without any certificate have all their offers of this card and grade counted once.
        cert_of_seller = {}
        for d in members:
            if d["cert"] and d["store_id"]:
                cert_of_seller.setdefault(d["store_id"], d["cert"])

        def slab(d):
            if d["cert"]:
                return ("cert", d["cert"])
            if d["store_id"]:
                return ("cert", cert_of_seller[d["store_id"]]) if d["store_id"] in cert_of_seller else ("seller", d["store_id"])
            return ("url", d["url"])
        keep = {}
        for d in sorted(members, key=lambda d: (d["last_ok"] or "", -(d["ask"] or 0)), reverse=True):   # the freshest, then the cheapest, stays
            who = slab(d)
            if who in keep and who[0] != "url":
                d["excluded"] = "duplicate:" + keep[who]["url"]
            else:
                keep[who] = d
        canon = [d for d in members if not d["excluded"]]
        if len(canon) >= 4:  # an offer far from every other offer of the same card and grade is shown, not counted
            for d in canon:
                others = [o["ask"] for o in canon if o is not d]
                med = statistics.median(others)
                if d["ask"] < OUTLIER_LOW * med or d["ask"] > OUTLIER_HIGH * med:
                    d["excluded"] = "outlier"
        used = [d for d in members if not d["excluded"]]
        for d in used:
            d["counted"] = True
        if used:
            prices = sorted(d["ask"] for d in used)
            sellers = {d["store_id"] or d["url"] for d in used}
            out[key] = {"n": len(prices), "sellers": len(sellers), "min": prices[0], "median": median_int(prices),
                        "mean": int(round(sum(prices) / len(prices))), "max": prices[-1], "limited": len(prices) < LIMITED_BELOW,
                        "urls": [d["url"] for d in sorted(used, key=lambda d: d["ask"])]}
    return rows, out


def listing_entry(d, obs, now):
    hist = daily_history(obs)
    latest = hist[0] if hist else None
    prev = hist[1] if len(hist) > 1 else None
    photos = json.loads(d["photos"]) if d.get("photos") else []
    return {
        "url": d["url"], "source": source_of(d["url"]), "seller": d["store_name"], "seller_id": (d["store_id"] or "")[:8] or None, "city": d["city"],
        "title": d["title"], "photo": d["photo"], "photos": photos, "asking_idr": d["ask"], "list_idr": d["list_price"], "stock": d["stock"],
        "status": d["status"], "status_at": d["status_at"], "last_ok": d["last_ok"], "first_seen": d["first_seen"], "last_seen_in_search": d["last_seen"],
        "origin": d["origin"], "verdict": d["verdict"], "identity": d["identity"], "cert": d["cert"],
        "counted": d["counted"], "excluded": d["excluded"],
        "latest": {"observed_at": latest["observed_at"], "asking_idr": latest["asking_idr"], "in_stock": latest["in_stock"]} if latest else None,
        "previous_day": {"observed_at": prev["observed_at"], "asking_idr": prev["asking_idr"], "in_stock": prev["in_stock"]} if prev else None,
        "change_from_previous_day_idr": (latest["asking_idr"] - prev["asking_idr"]) if latest and prev else None,
        "daily_latest": hist[:30],
    }


def build_export(db, now, run_info):
    rows, groups = analyze(db, now)
    obs = defaultdict(list)
    for o in db.execute("SELECT url, observed_at, ask, stock FROM observation ORDER BY observed_at"):
        obs[o["url"]].append((o["observed_at"], o["ask"], o["stock"]))
    cutoff = now - timedelta(days=RECHECK_DAYS)
    live = [d for d in rows.values() if d["verdict"] in ("accepted", "ambiguous") and datetime.fromisoformat(d["last_seen"] or d["first_seen"]) >= cutoff]
    accepted = sorted((d for d in live if d["verdict"] == "accepted"), key=lambda d: (d["identity"].get("name") or "", d["grade"] or "", d["ask"] or 0))
    ambiguous = [d for d in live if d["verdict"] == "ambiguous"]
    cards = {}
    for (ckey, grade), g in groups.items():
        sample = next(d for d in accepted if d["card_key"] == ckey)
        i = sample["identity"]
        c = cards.setdefault(ckey, {"key": ckey, "name": i["name"], "set_code": i["set_code"], "card_number": i["card_number"], "rarity": i["rarity"],
                                    "variant": i.get("variant") or "", "grades": []})
        c["grades"].append({"grade": grade, **g, "label": "data terbatas" if g["limited"] else None})
    for c in cards.values():
        c["grades"].sort(key=lambda g: -float(g["grade"]))
    changes = []
    for d in accepted:
        o = obs[d["url"]]
        if len(o) >= 2 and o[-1][1] != o[-2][1]:
            changes.append({"url": d["url"], "name": d["identity"]["name"], "grade": d["grade"], "from": o[-2][1], "to": o[-1][1], "at": o[-1][0]})
    day_ago = (now - timedelta(hours=24)).isoformat()
    ages = [(now - datetime.fromisoformat(d["last_ok"])).total_seconds() / 3600 for d in accepted if d["last_ok"]]
    status_counts = Counter(d["status"] for d in live)
    sellers = {d["store_id"] for d in accepted if d["counted"] and d["store_id"]}
    summary = {
        "candidates_found": run_info.get("candidates", 0),
        "accepted_total": len(accepted), "accepted_existing": sum(1 for d in accepted if d["origin"] == "bot"),
        "accepted_auto": sum(1 for d in accepted if d["origin"] != "bot"), "accepted_new_this_run": run_info.get("accepted_new", 0),
        "ambiguous": len(ambiguous), "rejected_this_run": run_info.get("rejected", 0), "rejected_reasons": run_info.get("rejected_reasons", {}),
        "listings_counted": sum(1 for d in accepted if d["counted"]), "cards": len(cards), "sellers_unique": len(sellers),
        "status": dict(status_counts),
        "by_source": {src: {"accepted": sum(1 for d in accepted if source_of(d["url"]) == src),
                            "counted": sum(1 for d in accepted if d["counted"] and source_of(d["url"]) == src)}
                      for src in sorted({source_of(d["url"]) for d in accepted})},
        "checked_ok_this_run": run_info.get("checked_ok", 0), "check_failed_this_run": run_info.get("check_failed", 0),
        "price_changes_24h": sum(1 for c in changes if c["at"] >= day_ago), "price_changes": changes[:20],
        "freshness": {"fresh_8h": sum(1 for a in ages if a <= 8), "fresh_24h": sum(1 for a in ages if 8 < a <= 24), "older": sum(1 for a in ages if a > 24),
                      "newest_ok": max((d["last_ok"] for d in accepted if d["last_ok"]), default=None),
                      "oldest_ok": min((d["last_ok"] for d in accepted if d["last_ok"]), default=None)}}
    queue = [{"url": d["url"], "photo": d["photo"], "title": d["title"], "asking_idr": d["ask"], "stock": d["stock"], "seller": d["store_name"],
              "city": d["city"], "status": d["status"], "first_seen": d["first_seen"], "last_ok": d["last_ok"],
              "reasons": d["reasons"], "suggestions": d["suggestions"], "identity": {k: v for k, v in d["identity"].items() if v}}
             for d in sorted(ambiguous, key=lambda d: -(d["ask"] or 0))]
    return {
        "schema_version": 1, "kind": "EGS_WATCH", "price_type": "OBSERVED_ASKING_ONLY", "source": "toco_public_search",
        "generated_at": now.isoformat(), "run": run_info, "summary": summary,
        "cards": sorted(cards.values(), key=lambda c: (c["name"], c["card_number"])),
        "listings": [listing_entry(d, obs[d["url"]], now) for d in accepted], "queue": queue,
        "notes": ["Harga penawaran, bukan harga transaksi.",
                  "Listing yang hilang dari hasil pencarian bukan bukti terjual.",
                  "Kandidat ambigu tidak masuk hitungan harga apa pun."]}


def validate(d):
    """Errors in an export (empty list = fine). The exporter calls this before it replaces the last good file."""
    errors = []
    for k in ("schema_version", "kind", "price_type", "generated_at", "run", "summary", "cards", "listings", "queue"):
        if k not in d:
            errors.append(f"kunci {k} tidak ada")
    if errors:
        return errors
    if d["schema_version"] != 1 or d["kind"] != "EGS_WATCH" or d["price_type"] != "OBSERVED_ASKING_ONLY":
        errors.append("schema_version, kind atau price_type salah")
    urls = [l["url"] for l in d["listings"]]
    if len(urls) != len(set(urls)):
        errors.append("url listing ganda")
    qurls = [q["url"] for q in d["queue"]]
    if len(qurls) != len(set(qurls)) or set(qurls) & set(urls):
        errors.append("antrean ganda atau tumpang tindih dengan listing diterima")
    s = d["summary"]
    if s["accepted_total"] != len(d["listings"]) or s["ambiguous"] != len(d["queue"]):
        errors.append("ringkasan tidak cocok dengan jumlah listing atau antrean")
    counted = [l for l in d["listings"] if l["counted"]]
    if s["listings_counted"] != len(counted):
        errors.append("listings_counted tidak cocok")
    if sum(g["n"] for c in d["cards"] for g in c["grades"]) != len(counted):
        errors.append("jumlah listing di statistik kartu tidak sama dengan listing yang dihitung")
    for l in d["listings"]:
        if l["verdict"] != "accepted" or not l["identity"].get("grade") or not l["identity"].get("card_number"):
            errors.append(f"listing diterima tanpa identitas lengkap: {l['url'][-40:]}")
        if l["counted"] and (not l["asking_idr"] or l["asking_idr"] <= 0 or l["status"] not in ("ok", "missing_from_search", "fetch_failed") or not l["last_ok"]):
            errors.append(f"listing dihitung tanpa bukti yang cukup: {l['url'][-40:]}")
        if not str(l["url"]).startswith("http"):
            errors.append("url bukan alamat http")
    for c in d["cards"]:
        for g in c["grades"]:
            if not (g["min"] <= g["median"] <= g["max"]) or g["n"] < 1 or g["sellers"] < 1 or g["sellers"] > g["n"]:
                errors.append(f"statistik tidak masuk akal: {c['name']} grade {g['grade']}")
            if g["limited"] != (g["n"] < LIMITED_BELOW):
                errors.append(f"label data terbatas salah: {c['name']} grade {g['grade']}")
    for q in d["queue"]:
        if not q["reasons"]:
            errors.append(f"antrean tanpa alasan: {q['url'][-40:]}")
    return errors


def rp(n):
    return "Rp" + f"{n:,}".replace(",", ".") if n is not None else "-"


def format_report(d, limit=12):
    s, r = d["summary"], d["run"]
    when = datetime.fromisoformat(d["generated_at"]).astimezone(WIB)
    lines = [f"# Pemantauan slab EGS ({when:%Y-%m-%d %H:%M} WIB)", ""]
    lines.append(f"Sumber: {r.get('requests', 0)} permintaan ({r.get('ok_requests', 0)} berhasil, {r.get('failed_requests', 0)} gagal, "
                 f"{r.get('cached_requests', 0)} dari cache, {r.get('retries', 0)} diulang)" + (f"; DIBATASI: {r['blocked']}" if r.get("blocked") else "")
                 + ("" if r.get("source_ok", True) else "; sumber tidak sehat, data lama dipertahankan"))
    lines += [f"- kandidat ditemukan (menyebut EGS): {s['candidates_found']}",
              f"- diterima: {s['accepted_total']} ({s['accepted_existing']} dari bot lama, {s['accepted_auto']} otomatis, {s['accepted_new_this_run']} baru putaran ini)",
              f"- ambigu (antrean, tanpa harga): {s['ambiguous']}",
              f"- ditolak putaran ini: {s['rejected_this_run']}" + ("".join(f"; {k} {v}" for k, v in s["rejected_reasons"].items())),
              f"- dihitung ke harga: {s['listings_counted']} listing, {s['cards']} kartu, {s['sellers_unique']} seller unik",
              f"- pemeriksaan putaran ini: {s['checked_ok_this_run']} berhasil, {s['check_failed_this_run']} gagal; status: " + ", ".join(f"{k} {v}" for k, v in sorted(s["status"].items())),
              f"- perubahan harga 24 jam: {s['price_changes_24h']}",
              *([f"- per sumber: " + ", ".join(f"{k} {v['accepted']} diterima/{v['counted']} dihitung" for k, v in s["by_source"].items())] if len(s.get("by_source") or {}) > 1 else []),
              *([f"- lembar kerja: {r['worksheet']['filled_rows']} baris antrean diisi ({r['worksheet']['accepted']} diterima, {r['worksheet']['ambiguous']} masih ragu, "
                 f"{r['worksheet']['rejected']} ditolak), {r['worksheet']['market_rows']} harga pasar dicatat" + (f", {len(r['worksheet']['problems'])} baris bermasalah" if r['worksheet']['problems'] else "")]
                if r.get("worksheet") else []),
              *([f"- impor manual: {r['imported']['rows']} baris, {r['imported']['accepted']} diterima, {r['imported']['ambiguous']} ambigu, "
                 f"{r['imported']['rejected']} ditolak" + (f", {len(r['imported']['problems'])} baris bermasalah" if r['imported']['problems'] else "")] if r.get("imported") else []),
              f"- kesegaran: {s['freshness']['fresh_8h']} dalam 8 jam, {s['freshness']['fresh_24h']} dalam 24 jam, {s['freshness']['older']} lebih lama", ""]
    lines.append("## Harga per kartu dan grade")
    for c in d["cards"]:
        for g in c["grades"]:
            lines.append(f"- {c['name']} {c['card_number']} {c['set_code']} {c['rarity'] or ''} EGS {g['grade']}: {rp(g['min'])} - {rp(g['max'])}, median {rp(g['median'])}, "
                         f"rata-rata {rp(g['mean'])}, {g['n']} listing, {g['sellers']} seller" + (f" [{g['label']}]" if g["label"] else ""))
    lines += ["", f"## Antrean ambigu (maks {limit})"]
    for q in d["queue"][:limit]:
        lines.append(f"- {rp(q['asking_idr'])} {q['title'][:70]}")
        lines.append(f"    alasan: " + "; ".join(x["text"] for x in q["reasons"]))
        lines.append(f"    foto: {q['photo']}  tautan: {q['url']}")
    if not d["queue"]:
        lines.append("(kosong)")
    return "\n".join(lines)


# ---- reading the photo (optional, a vision model; off by default) ------------------------------------

def parse_photo_answer(text):
    """The JSON object a vision model answered with, as the dict egs_identity.verify expects; None when it is not usable."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    clean = lambda v: None if v in (None, "", "null", "None") else str(v).strip()
    out = {"is_slab": data.get("is_slab") if isinstance(data.get("is_slab"), bool) else None,
           "company": clean(data.get("company")), "grade": clean(data.get("grade")), "cert": clean(data.get("cert")),
           "name": clean(data.get("card_name")), "number": clean(data.get("number")), "rarity": clean(data.get("rarity"))}
    lang = clean(data.get("language"))
    out["language"] = collect_raw.normalize_lang(lang) if lang else None
    return out


def make_photo_reader(provider, model, fetch=collect_raw.fetch):
    """photo_fn(image_url) -> dict | None, with an official SDK (pip install openai | anthropic and the key in the environment).
    None means 'could not read; try again in a later run'. Needs an API balance of its own: it is only used with --photo-mode."""
    import base64
    ask = _ask_openai(model) if provider == "openai" else _ask_anthropic(model)

    def read(image_url):
        try:
            data, _ = fetch(image_url)
        except collect_raw.FetchError:
            return None
        media = collect_raw.sniff_media_type(data)
        if not media:
            return None
        answer = ask(base64.standard_b64encode(data).decode("ascii"), media)
        return parse_photo_answer(answer) if answer else None
    return read


def _ask_openai(model):
    import openai
    client = openai.OpenAI(max_retries=2)

    def ask(b64, media):
        try:
            r = client.chat.completions.create(model=model, max_completion_tokens=2000, messages=[{"role": "user", "content": [
                {"type": "text", "text": PHOTO_PROMPT}, {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}}]}])
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError, openai.BadRequestError) as e:
            raise SystemExit(f"OpenAI menolak permintaan ({type(e).__name__}): periksa kunci, model dan saldo API")
        except openai.RateLimitError as e:
            if "insufficient_quota" in str(e):
                raise SystemExit("Saldo API OpenAI habis (insufficient_quota).")
            return None
        except (openai.APIStatusError, openai.APIConnectionError):
            return None
        return r.choices[0].message.content
    return ask


def _ask_anthropic(model):
    import anthropic
    client = anthropic.Anthropic()

    def ask(b64, media):
        try:
            r = client.messages.create(model=model, max_tokens=1024, messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}, {"type": "text", "text": PHOTO_PROMPT}]}])
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise SystemExit(f"Claude menolak permintaan ({type(e).__name__}): periksa kunci dan model")
        except (anthropic.APIStatusError, anthropic.APIConnectionError):
            return None
        return "".join(b.text for b in r.content if b.type == "text")
    return ask


# ---- one run -----------------------------------------------------------------------------

def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def tracked_types(latest):
    seen, out = set(), []
    for l in (latest or {}).get("listings", []):
        key = (l["name"], l["set_code"], l["card_number"], l["rarity"])
        if key not in seen:
            seen.add(key)
            out.append({"name": l["name"], "set": l["set_code"], "number": l["card_number"], "rarity": l["rarity"]})
    return out


def run(cfg, post, status_fn=fetch_status, photo_fn=None, now=None, sleep=time.sleep, log=print):
    """One full run. Returns (export or None, exit code): 0 fine, 2 the source was not healthy, 3 the export failed its own validation."""
    explicit_clock = now is not None
    now = now or datetime.now(timezone.utc)
    started = now
    hits = load_config(cfg.hits)
    catalog = ident.Catalog(read_json(cfg.catalog))
    latest = read_json(cfg.latest)
    db = open_db(cfg.db, copy_only=cfg.dry_run)
    seed_titles = seed(db, latest, now, log) if latest and not cfg.no_seed else []
    if seed_titles:  # the candidates the old bot was waiting on are searched until one run gets through
        meta_set(db, "pending_seed", json.dumps(seed_titles, ensure_ascii=False))
        db.commit()
    pending = json.loads(meta_get(db, "pending_seed") or "[]")
    accepted_names = [r[0] for r in db.execute("SELECT DISTINCT json_extract(identity, '$.name') FROM listing WHERE verdict='accepted' AND origin != 'bot'") if r[0]]
    queries = build_queries(hits, tracked_types(latest), accepted_names, pending or ())
    toco = Toco(post, cfg.cache_dir, cfg.cache_ttl, sleep=sleep, log=log)
    no_search = getattr(cfg, "no_search", False)
    imports, import_problems = read_import(cfg.import_file) if getattr(cfg, "import_file", None) else ([], [])
    manual, ws_info = Counter(), None
    if getattr(cfg, "worksheet", None):
        reads, ws_rows, ws_problems = egs_worksheet.read(cfg.worksheet, collect_raw.fetch)
        manual = apply_manual(db, reads, catalog, tracked_by_url(db), log)
        seen = {n["url"]: n for n in imports}
        for row in ws_rows:
            rec, problem = normalize_import(row)
            if problem:
                ws_problems.append(problem)
            else:
                seen[rec["url"]] = rec
        imports = list(seen.values())
        ws_info = {"filled_rows": len(reads), "accepted": manual["accepted"], "ambiguous": manual["ambiguous"], "rejected": manual["rejected"],
                   "unknown": manual["unknown"], "market_rows": len(ws_rows), "problems": ws_problems[:30]}
        import_problems = import_problems + ws_problems
    for problem in import_problems:
        log(f"  ! impor: {problem}")
    if no_search:
        items, diag, blocked = {}, [], False
        log(f"tanpa pencarian toco; {len(imports)} baris impor")
    else:
        log(f"{len(queries)} kata kunci, anggaran {cfg.budget} permintaan, jeda {cfg.delay} detik")
        items, diag, blocked = discover(toco, queries, cfg.size, cfg.delay, cfg.budget, cfg.min_share, sleep, log)
    for d in diag:
        log(f"  {d['query']!r}: {d['pages']} halaman, {d['listings']} listing dari total {d['total']} [{d['stopped']}]")

    tracked = tracked_by_url(db)
    photo_budget = [cfg.photo_budget]
    cand = accepted_new = ignored = 0
    rejected = Counter()
    for slug, item in items.items():
        n = normalize(item)
        if not EGS_WORD.search(f"{n['title']} {n['description']}") and n["url"] not in tracked:
            ignored += 1
            continue
        cand += 1
        v, read = judge(db, n, catalog, tracked, photo_fn, photo_budget)
        new = upsert(db, n, v, now, read)
        if v["verdict"] == "accepted" and new and n["url"] not in tracked:
            accepted_new += 1
        elif v["verdict"] == "rejected":
            rejected[v["reasons"][0]["code"]] += 1
    imported = Counter()
    for n in imports:
        v, read = judge(db, n, catalog, tracked, photo_fn, photo_budget)
        new = upsert(db, n, v, now, read, origin="import")
        imported[v["verdict"]] += 1
        cand += 1
        if v["verdict"] == "accepted" and new:
            accepted_new += 1
        elif v["verdict"] == "rejected":
            rejected[v["reasons"][0]["code"]] += 1
    db.commit()
    healthy = no_search or (not blocked and toco.ok + toco.cached >= 1 and (toco.ok + toco.cached) >= 0.5 * (toco.ok + toco.cached + toco.failed))
    rc = Counter()
    if healthy and not no_search:
        rc = recheck(db, toco, status_fn, set(items), catalog, tracked, photo_fn, photo_budget, now, cfg.recheck_limit, cfg.delay, sleep, log)
        if pending:
            meta_set(db, "pending_seed", "[]")
    else:
        log("! sumber tidak sehat: status listing tidak diubah dan tidak ada yang dianggap hilang")
    db.commit()
    run_info = {"started_at": started.isoformat(), "queries": len(queries), "requests": toco.requests, "ok_requests": toco.ok,
                "failed_requests": toco.failed, "cached_requests": toco.cached, "retries": toco.retried, "blocked": toco.blocked,
                "source_ok": healthy, "results_seen": len(items), "ignored_not_egs": ignored, "candidates": cand, "accepted_new": accepted_new,
                "rejected": sum(rejected.values()), "rejected_reasons": dict(rejected),
                "checked_ok": cand - sum(rejected.values()) + rc["ok"] + rc["unavailable"],
                "check_failed": rc["fetch_failed"] + rc["blocked"], "recheck": dict(rc), "photo_reads": cfg.photo_budget - photo_budget[0],
                "imported": ({"rows": len(imports), "accepted": imported["accepted"], "ambiguous": imported["ambiguous"], "rejected": imported["rejected"],
                              "problems": import_problems[:20]} if getattr(cfg, "import_file", None) or ws_info else None),
                "worksheet": ws_info,
                "dry_run": bool(cfg.dry_run), "finished_at": (now if explicit_clock else datetime.now(timezone.utc)).isoformat()}
    if not items and not imports and not healthy:
        log("Tidak ada hasil pencarian dan sumber tidak sehat. egs_watch.json TIDAK diubah.")
        return None, 2
    export = build_export(db, now, run_info)
    errors = validate(export)
    if errors:
        log("Hasil tidak lolos validasi, berkas lama dipertahankan:")
        for e in errors[:10]:
            log("  - " + e)
        return None, 3
    if not cfg.dry_run:
        db.execute("INSERT INTO run(started_at, finished_at, ok, summary) VALUES(?,?,?,?)",
                   (started.isoformat(), run_info["finished_at"], 1 if healthy else 0, json.dumps(export["summary"], ensure_ascii=False)))
        db.commit()
        os.makedirs(os.path.dirname(os.path.abspath(cfg.out)), exist_ok=True)
        with open(cfg.out + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(export, fh, ensure_ascii=False, indent=1)
        os.replace(cfg.out + ".tmp", cfg.out)
        log(f"ditulis: {cfg.out}")
    db.close()
    return export, (0 if healthy else 2)


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.path.expanduser("~/.egs-raw/egs_watch.sqlite"))
    ap.add_argument("--out", default="egs_watch.json")
    ap.add_argument("--latest", default="latest.json", help="laporan bot lama: hanya dibaca, untuk data awal dan daftar kartu dipantau")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--hits", default=os.path.join(here, "egs_hits.json"))
    ap.add_argument("--size", type=int, default=45)
    ap.add_argument("--delay", type=float, default=1.5, help="jeda antar permintaan (detik)")
    ap.add_argument("--budget", type=int, default=320, help="batas permintaan pencarian per putaran")
    ap.add_argument("--min-share", type=float, default=0.2, help="berhenti menelusuri sebuah kata kunci kalau kurang dari bagian ini yang menyebut EGS")
    ap.add_argument("--cache-dir", help="folder cache hasil pencarian (bawaan: di samping database)")
    ap.add_argument("--cache-ttl", type=int, default=CACHE_TTL, help="detik sebuah halaman pencarian dipakai ulang")
    ap.add_argument("--recheck-limit", type=int, default=80, help="listing yang diperiksa satu per satu bila tidak muncul di pencarian")
    ap.add_argument("--import", dest="import_file", metavar="BERKAS",
                    help="CSV atau JSON listing yang dikumpulkan sendiri (kolom: url, judul, harga, toko, foto, stok): dinilai dan dihitung seperti listing toco; tanpa membuka situsnya")
    ap.add_argument("--worksheet", metavar="BERKAS_ATAU_ALAMAT",
                    help="lembar kerja yang sudah Anda isi (CSV, atau alamat 'publikasikan ke web' dari Google Sheets): foto antrean yang Anda baca dan harga Tokopedia/Shopee yang Anda catat")
    ap.add_argument("--make-worksheet", metavar="CSV", help="tulis lembar kerja (foto + kolom isian) dari --out dan --catalog, lalu selesai")
    ap.add_argument("--no-search", action="store_true", help="lewati pencarian toco (berguna bersama --import)")
    ap.add_argument("--no-seed", action="store_true", help="jangan impor latest.json ke database")
    ap.add_argument("--dry-run", action="store_true", help="baca dan nilai saja: database disalin ke memori dan tidak ada berkas hasil ditulis (cache pencarian tetap dipakai)")
    ap.add_argument("--photo-mode", choices=("off", "openai", "anthropic"), default=os.environ.get("EGS_PHOTO_MODE", "off"),
                    help="baca foto kandidat ambigu dengan model visi (butuh saldo API sendiri; bawaan mati)")
    ap.add_argument("--photo-model", default=os.environ.get("EGS_PHOTO_MODEL"))
    ap.add_argument("--photo-budget", type=int, default=30, help="batas pembacaan foto per putaran")
    ap.add_argument("--read-photo", metavar="URL", help="uji pembaca foto pada satu foto (butuh --photo-mode dan kunci API), lalu selesai")
    ap.add_argument("--report", metavar="EXPORT", help="cetak laporan teks dari egs_watch.json, lalu selesai")
    ap.add_argument("--validate", metavar="EXPORT", help="periksa egs_watch.json, lalu selesai")
    a = ap.parse_args(argv)
    if a.make_worksheet:
        export, catalog = read_json(a.out), read_json(a.catalog)
        if export is None:
            print(f"{a.out} tidak terbaca: jalankan pemantau dulu")
            return 1
        counts = egs_worksheet.make(export, catalog, a.make_worksheet)
        print(f"ditulis: {a.make_worksheet} ({counts['pasar']} baris pasar, {counts['antrean']} baris antrean)")
        return 0
    if a.report or a.validate:
        d = read_json(a.report or a.validate)
        if d is None:
            print("berkas tidak terbaca")
            return 1
        errors = validate(d)
        if a.validate:
            print("\n".join(errors) if errors else "OK")
            return 1 if errors else 0
        print(format_report(d))
        return 0
    if a.read_photo:
        if a.photo_mode == "off":
            print("Pakai --photo-mode openai (atau anthropic) bersama --read-photo.")
            return 1
        reader = make_photo_reader(a.photo_mode, a.photo_model or collect_raw.DEFAULT_MODELS[a.photo_mode])
        answer = reader(a.read_photo)
        print(json.dumps(answer, ensure_ascii=False, indent=1) if answer else "foto tidak terbaca atau jawabannya tidak bisa dipakai")
        return 0 if answer else 1
    a.cache_dir = a.cache_dir or os.path.join(os.path.dirname(os.path.abspath(a.db)), "egs_cache")
    photo_fn = None
    if a.photo_mode != "off":
        photo_fn = make_photo_reader(a.photo_mode, a.photo_model or collect_raw.DEFAULT_MODELS[a.photo_mode])
    export, code = run(a, make_post(), fetch_status, photo_fn)
    if export:
        print(format_report(export))
    return code


if __name__ == "__main__":
    sys.exit(main())
