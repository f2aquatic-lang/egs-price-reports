#!/usr/bin/env python3
"""Fetch the official Indonesian card catalog (asia.pokemon-card.com/id/card-search/) into catalog.json.

What it reads (all public pages, one request per --delay seconds, resumable):
  home    /id/card-search/?pageNo=N                 the list of expansions (code, series, release date)
  list    /id/card-search/list/?pageNo=N&expansionCodes=CODE   20 cards per page, ids only
  detail  /id/card-search/detail/ID/                name, card number, image, product (set) name

The result is cached in a SQLite file, so a run that stops (--max-requests) simply continues next time, and a
later run only fetches what is new. catalog.json holds, per set, its names and cards (set, number, name, image).

Usage:
  python3 tools/fetch_catalog.py --list-sets                       # the expansions on the home pages (5 requests)
  python3 tools/fetch_catalog.py --sets MA6 --max-requests 60      # a small trial
  python3 tools/fetch_catalog.py --db /opt/egs-raw/catalog.sqlite --out /opt/egs-raw/catalog.json
Needs only the Python standard library.
"""
import argparse
import fcntl
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://asia.pokemon-card.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# main booster sets: MA1..MA6, SV1S..SV11S, SV1A, SV2A, SV4A, SV8A, ... and the AC3 TAG TEAM sets; not decks or promos
DEFAULT_INCLUDE = r"^(MA\d+|SV\d+[A-Z]|AC\d[A-Z])$"


# rarity filter codes of the site (seen on 2026-10-04); the run re-reads them from the home page and falls back to these
RARITY_CODES = {"1": "C", "2": "U", "3": "R", "4": "RR", "5": "RRR", "6": "PR", "7": "TR", "8": "SR", "9": "HR", "10": "UR",
                "11": "Tanpa Tanda", "12": "K", "13": "A", "14": "AR", "15": "SAR", "16": "S", "17": "SSR", "18": "ACE",
                "19": "BWR", "20": "MUR", "21": "MA", "22": "FUR"}
NO_RARITY = "Tanpa Tanda"  # cards without a rarity mark (basic energy, some promos): stored as None


class Budget(Exception):
    """The request budget of this run is spent; what was fetched is already saved."""


def http_get(url, timeout=30):
    """(status, text); never raises. A network failure is status 0."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "id,en;q=0.8"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read(3_000_000).decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, OSError):
        return 0, ""


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


# ---- page parsers (written against the real markup seen on 2026-10-04) ----------------------------

def parse_home(page):
    """Expansion tiles: [{code, series, released (ISO or None), texts}] and the highest page number."""
    tiles = []
    for block in re.findall(r'<li class="expansion">(.*?)</li>', page, re.S):
        code = re.search(r"expansionCodes=([A-Za-z0-9]+)", block)
        if not code:
            continue
        series = re.search(r'<span class="series">(.*?)</span>', block, re.S)
        date = re.search(r'datetime="(\d\d)-(\d\d)-(\d{4})"', block)
        tiles.append({
            "code": code.group(1).upper(),
            "series": text_of(series.group(1)) if series else None,
            "released": f"{date.group(3)}-{date.group(1)}-{date.group(2)}" if date else None,
            "texts": [t for t in (text_of(x) for x in re.split(r"<[^>]+>", block)) if t],
        })
    pages = [int(n) for n in re.findall(r"\?pageNo=(\d+)", page)]
    return tiles, max(pages) if pages else 1


def parse_list(page):
    """(card ids in page order, total cards, total pages); the totals are None if the page does not say."""
    ids = [int(i) for i in re.findall(r'<li class="card">\s*<a href="/id/card-search/detail/(\d+)/"', page)]
    m = re.search(r"Hasil Pencarian\s+(\d+)\s+buah\s+Halaman\s+\d+\s*/\s*Total\s+(\d+)\s+halaman", text_of(page))
    return ids, int(m.group(1)) if m else None, int(m.group(2)) if m else None


def parse_detail(page):
    """{name, number, image, evolve, products} of a card page; missing parts are None."""
    h1 = re.search(r'<h1 class="pageHeader cardDetail">(.*?)</h1>', page, re.S)
    evolve = name = None
    if h1:
        marker = re.search(r'<span class="evolveMarker">(.*?)</span>', h1.group(1), re.S)
        evolve = text_of(marker.group(1)) if marker else None
        name = text_of(re.sub(r'<span class="evolveMarker">.*?</span>', " ", h1.group(1), flags=re.S))
    num = re.search(r'<span class="collectorNumber">\s*(.*?)\s*</span>', page, re.S)
    img = re.search(r'<div class="cardImage">\s*<img[^>]*\ssrc="([^"]+)"', page, re.S)
    section = re.search(r"Ada di produk</h3>(.*?)</section>", page, re.S)
    products = {code.upper(): text_of(label) for code, label in re.findall(
        r'<a href="[^"]*expansionCodes=([A-Za-z0-9]+)"[^>]*>(.*?)</a>', section.group(1) if section else "", re.S)}
    return {"name": name or None, "evolve": evolve or None,
            "number": re.sub(r"\s+", "", text_of(num.group(1))) if num else None,
            "image": img.group(1) if img else None,
            "products": products}  # a card reprinted in several products lists them all: {code: 'Booster Pack "..."'}


def parse_rarity_filter(page):
    """[(code, label, source piece)] of the rarity checkboxes of the search form (label is a best guess)."""
    out = []
    for m in re.finditer(r'<input[^>]*name="rarity(?:\[\]|%5B%5D)"[^>]*>', page):
        tag = m.group(0)
        value = re.search(r'value="([^"]*)"', tag)
        ident = re.search(r'\bid="([^"]+)"', tag)
        piece = page[m.start(): m.end() + 350]
        label = None
        if ident:
            lab = re.search(rf'<label[^>]*for="{re.escape(ident.group(1))}"[^>]*>(.*?)</label>', page, re.S)
            if lab:
                piece = lab.group(0)
                label = text_of(lab.group(1)) or None
                alt = re.search(r'alt="([^"]+)"', lab.group(1))
                label = label or (alt.group(1) if alt else None)
        if label is None:  # the text or the image alt right after the checkbox
            after = page[m.end(): m.end() + 350]
            nxt = re.split(r"<input", after)[0]
            alt = re.search(r'(?:alt|title)="([^"]+)"', nxt)
            label = text_of(nxt)[:30] or (alt.group(1) if alt else None)
        out.append((value.group(1) if value else None, label, re.sub(r"\s+", " ", piece)[:300]))
    return out


def product_name(product):
    """'Booster Pack "Evolusi Mega"' -> 'Evolusi Mega'; anything without quotes is returned as it is."""
    m = re.search(r'["“](.+?)["”]', product or "")
    return m.group(1).strip() if m else (product or None)


# ---- storage ----------------------------------------------------------------------------------------

def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS expansion(code TEXT PRIMARY KEY, series TEXT, released TEXT, product TEXT, name TEXT,
            total INTEGER, pages INTEGER, listed_at TEXT, position INTEGER);
        CREATE TABLE IF NOT EXISTS listed(code TEXT, pos INTEGER, card_id INTEGER, PRIMARY KEY(code, card_id));
        CREATE TABLE IF NOT EXISTS card(id INTEGER PRIMARY KEY, code TEXT, number TEXT, name TEXT, evolve TEXT, image TEXT,
            fetched_at TEXT);
        CREATE TABLE IF NOT EXISTS card_rarity(card_id INTEGER PRIMARY KEY, rarity TEXT);
    """)
    return db


class Fetcher:
    def __init__(self, get, delay, budget, sleep=time.sleep, log=print):
        self.get, self.delay, self.budget, self.sleep, self.log, self.used = get, delay, budget, sleep, log, 0
        self.rarity_codes = dict(RARITY_CODES)

    def page(self, url):
        if self.budget is not None and self.used >= self.budget:
            raise Budget()
        if self.used:
            self.sleep(self.delay)
        self.used += 1
        status, text = self.get(url)
        if status != 200:
            self.log(f"  ! {url} -> status {status}")
            return None
        return text


def crawl_home(fx, db, now):
    """All expansion tiles from the home pages (the order on the site, newest first)."""
    tiles, page_no, last = [], 1, 1
    while page_no <= last:
        text = fx.page(f"{BASE}/id/card-search/" + (f"?pageNo={page_no}" if page_no > 1 else ""))
        if text is None:
            break
        if page_no == 1:
            labels = {c: l for c, l, _ in parse_rarity_filter(text) if c and l}
            if labels:
                fx.rarity_codes = labels
        found, seen_last = parse_home(text)
        tiles += [t for t in found if t["code"] not in {x["code"] for x in tiles}]
        last = max(last, seen_last)
        page_no += 1
    for i, t in enumerate(tiles):
        db.execute("INSERT INTO expansion(code, series, released, position) VALUES(?,?,?,?) "
                   "ON CONFLICT(code) DO UPDATE SET series=excluded.series, released=excluded.released, position=excluded.position",
                   (t["code"], t["series"], t["released"], i))
    db.commit()
    return tiles


def crawl_set(fx, db, code, now, refresh_days):
    """List pages of one set (ids), then the detail page of every card that is not cached yet."""
    db.execute("INSERT OR IGNORE INTO expansion(code, position) VALUES(?, 9999)", (code,))
    row = db.execute("SELECT total, listed_at FROM expansion WHERE code=?", (code,)).fetchone()
    fresh = row and row[1] and (now - datetime.fromisoformat(row[1])).days < refresh_days
    if not fresh:
        ids, total, pages, page_no = [], None, None, 1
        while pages is None or page_no <= pages:
            text = fx.page(f"{BASE}/id/card-search/list/?pageNo={page_no}&expansionCodes={code}")
            if text is None:
                return
            got, total_now, pages_now = parse_list(text)
            ids += got
            total, pages = total_now if total_now is not None else total, pages_now if pages_now is not None else pages
            if not got or pages is None and len(got) < 20:
                break
            page_no += 1
        for pos, cid in enumerate(dict.fromkeys(ids)):
            db.execute("INSERT OR IGNORE INTO listed(code, pos, card_id) VALUES(?,?,?)", (code, pos, cid))
        db.execute("UPDATE expansion SET total=?, pages=?, listed_at=? WHERE code=?", (total or len(set(ids)), pages, now.isoformat(), code))
        db.commit()
    todo = [r[0] for r in db.execute(
        "SELECT card_id FROM listed WHERE code=? AND card_id NOT IN (SELECT id FROM card) ORDER BY pos", (code,))]
    for cid in todo:
        text = fx.page(f"{BASE}/id/card-search/detail/{cid}/")
        if text is None:
            continue
        d = parse_detail(text)
        db.execute("INSERT OR REPLACE INTO card(id, code, number, name, evolve, image, fetched_at) VALUES(?,?,?,?,?,?,?)",
                   (cid, code, d["number"], d["name"], d["evolve"], d["image"], now.isoformat()))
        if code in d["products"]:  # the product of THIS set, not of another one the card was also printed in
            db.execute("UPDATE expansion SET product=?, name=? WHERE code=?",
                       (d["products"][code], product_name(d["products"][code]), code))
        db.commit()


def crawl_rarities(fx, db, code):
    """Rarity of every card of a set: one filtered list per rarity code (the detail pages do not show it).

    A card must come out under exactly one rarity; if the site ignores the filter, cards would show up under
    several, and nothing is stored for the set.
    """
    missing = db.execute("SELECT COUNT(*) FROM listed WHERE code=? AND card_id NOT IN (SELECT card_id FROM card_rarity)",
                         (code,)).fetchone()[0]
    if not missing:
        return
    wanted = {r[0] for r in db.execute("SELECT card_id FROM listed WHERE code=?", (code,))}
    found = {}
    for rcode, label in fx.rarity_codes.items():
        page_no, pages = 1, None
        while pages is None or page_no <= pages:
            text = fx.page(f"{BASE}/id/card-search/list/?pageNo={page_no}&expansionCodes={code}&rarity%5B%5D={rcode}")
            if text is None:
                break  # an empty filter may be answered with an error page, or the request failed: either way nothing for this rarity
            ids, total, pages_now = parse_list(text)
            pages = pages_now if pages_now is not None else (1 if len(ids) < 20 else None)
            for cid in ids:
                found.setdefault(cid, set()).add(label)
            if not ids or pages is None and len(ids) < 20:
                break
            page_no += 1
    clash = [cid for cid, labels in found.items() if len(labels) > 1]
    if clash:
        fx.log(f"  ! {code}: {len(clash)} kartu muncul di lebih dari satu rarity; filter rarity tampaknya diabaikan, tidak disimpan")
        return
    for cid in wanted:
        labels = found.get(cid)
        if labels:
            label = next(iter(labels))
            db.execute("INSERT OR REPLACE INTO card_rarity(card_id, rarity) VALUES(?,?)", (cid, None if label == NO_RARITY else label))
    db.commit()
    unknown = len([cid for cid in wanted if cid not in found])
    if unknown:
        fx.log(f"  {code}: {unknown} kartu tanpa rarity di filter")


def export(db, codes, now):
    sets, cards = [], []
    for code, series, released, product, name, total in db.execute(
            "SELECT code, series, released, product, name, total FROM expansion ORDER BY position"):
        if code not in codes:
            continue
        rows = db.execute("SELECT c.id, c.number, c.name, c.evolve, c.image, r.rarity FROM listed l JOIN card c ON c.id = l.card_id "
                          "LEFT JOIN card_rarity r ON r.card_id = c.id WHERE l.code=? ORDER BY l.pos", (code,)).fetchall()
        sets.append({"code": code, "series": series, "name": name, "product": product, "released": released,
                     "total": total, "fetched": len(rows), "complete": total is not None and len(rows) >= total})
        for cid, number, cname, evolve, image, rarity in rows:
            if not number or not cname:
                continue
            m = re.fullmatch(r"(\d+)/(\d+)", number)
            cards.append({"set": code, "number": number, "num": int(m.group(1)) if m else None,
                          "den": int(m.group(2)) if m else None, "name": cname, "rarity": rarity, "evolve": evolve,
                          "image": image, "id": cid})
    return {"schema_version": 1, "source": "asia.pokemon-card.com/id", "generated_at": now.isoformat(),
            "sets": sets, "cards": cards}


def run(args, get=http_get, now=None, sleep=time.sleep, log=print):
    now = now or datetime.now(timezone.utc)
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    lock = open(args.db + ".lock", "w")  # one fetch at a time per database: two runs would fight over the same SQLite file
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"Sudah ada pengambilan katalog yang berjalan untuk {args.db}. Tunggu selesai, atau hentikan dengan: pkill -f fetch_catalog")
        lock.close()
        return None
    try:
        return _run(args, get, now, sleep, log)
    finally:
        lock.close()


def _run(args, get, now, sleep, log):
    db = open_db(args.db)
    fx = Fetcher(get, args.delay, args.max_requests, sleep, log)
    try:
        try:
            tiles = crawl_home(fx, db, now)
        except Budget:
            tiles = []
        if not tiles:
            tiles = [{"code": r[0], "series": r[1], "released": r[2], "texts": []} for r in
                     db.execute("SELECT code, series, released FROM expansion ORDER BY position")]
        if args.list_rarities:
            text = fx.page(f"{BASE}/id/card-search/")
            for code, label, piece in parse_rarity_filter(text or ""):
                log(f"rarity kode {code!r}: label {label!r}")
            for code, label, piece in parse_rarity_filter(text or "")[:3]:
                log(f"  potongan kode {code!r}: {piece}")
            return None
        if args.list_sets:
            for t in tiles:
                log(f"{t['code']:8} | {t['series']} | {t['released']} | {' / '.join(t['texts'])[:110]}")
            return None
        wanted = [c.upper() for c in args.sets.split(",")] if args.sets else [
            t["code"] for t in tiles if re.search(args.include, t["code"])]
        log(f"{len(tiles)} set di situs, {len(wanted)} dipilih: {', '.join(wanted)}")
        for code in wanted:
            try:
                crawl_set(fx, db, code, now, args.refresh_days)
                if args.rarities:
                    crawl_rarities(fx, db, code)
            except Budget:
                log(f"anggaran {args.max_requests} permintaan habis di set {code}; jalankan lagi untuk melanjutkan")
                break
            row = db.execute("SELECT total, (SELECT COUNT(*) FROM card WHERE code=?) FROM expansion WHERE code=?",
                             (code, code)).fetchone()
            log(f"  {code}: {row[1]} dari {row[0]} kartu")
            if args.rarities:
                dist = db.execute(
                    "SELECT CASE WHEN r.card_id IS NULL THEN 'belum diambil' ELSE COALESCE(r.rarity, 'tanpa tanda') END, COUNT(*) "
                    "FROM listed l LEFT JOIN card_rarity r ON r.card_id = l.card_id WHERE l.code=? GROUP BY 1 ORDER BY 2 DESC", (code,)).fetchall()
                log(f"    rarity: " + ", ".join(f"{label} {n}" for label, n in dist))
        data = export(db, set(wanted), now)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(args.out + ".tmp", args.out)
        log(f"ditulis: {args.out} ({len(data['sets'])} set, {len(data['cards'])} kartu, {fx.used} permintaan)")
        return data
    finally:
        db.commit()
        db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.path.expanduser("~/.egs-raw/catalog.sqlite"))
    ap.add_argument("--out", default="catalog.json")
    ap.add_argument("--delay", type=float, default=1.0, help="detik antar permintaan")
    ap.add_argument("--max-requests", type=int, help="batas permintaan per putaran; lanjut di putaran berikutnya")
    ap.add_argument("--sets", help="kode set dipisah koma (bawaan: semua yang cocok --include)")
    ap.add_argument("--include", default=DEFAULT_INCLUDE, help="ekspresi reguler kode set yang diambil")
    ap.add_argument("--refresh-days", type=int, default=30, help="daftar kartu sebuah set dibaca ulang setelah sekian hari")
    ap.add_argument("--list-sets", action="store_true", help="hanya tampilkan set di situs, lalu selesai")
    ap.add_argument("--no-rarities", dest="rarities", action="store_false", help="jangan ambil rarity (lebih sedikit permintaan)")
    ap.add_argument("--list-rarities", action="store_true", help="hanya tampilkan kode dan nama rarity di filter situs, lalu selesai")
    args = ap.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
