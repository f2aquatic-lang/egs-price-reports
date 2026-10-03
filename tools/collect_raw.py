#!/usr/bin/env python3
"""Collect RAW (ungraded) Indonesian card listings from toco and write raw.json.

Runs on the VPS (it needs toco.id and an Anthropic credential). See docs/vps-setup.md
and docs/raw-dataset.md. Steps: search toco per tracked card, classify titles
(tools/classify.py), match to the tracked cards, read the card language from the listing
photo with Claude, keep everything in a local SQLite file, write raw.json.

  python3 tools/collect_raw.py --dry-run --save-html ~/egs-raw/html   # first run, no API calls
  python3 tools/collect_raw.py --test-image card.jpg                  # check the API key
  python3 tools/collect_raw.py                                        # real run

The toco page layout was not available when this was written. The parser reads every
link to /listing/ with the text "Sisa N <title> Rp<price> ... <city>", the same text the
EGS bot already records. --dry-run prints what was and was not understood.
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
import validate_raw  # noqa: E402
import watch_queries  # noqa: E402
from classify import classify_title  # noqa: E402
from triage_candidates import parse as parse_card_text  # noqa: E402

BASE = "https://toco.id"
UA = "Mozilla/5.0 (compatible; egs-price-reports/1.0)"
DEFAULT_MODEL = "claude-opus-5-5"
LANG_PROMPT = (
    "This is a photo from a marketplace listing of a Pokemon trading card. "
    "What language is the text printed on the card itself? "
    "Answer with exactly one word: ID (Indonesian), JP (Japanese), EN (English), "
    "OTHER (any other language), or UNCLEAR (cannot tell: blurry, glare, back of the card, "
    "or no card visible).")
LANG_ALIASES = {"ID": "ID", "INDONESIAN": "ID", "INDONESIA": "ID", "JP": "JP", "JAPANESE": "JP",
                "EN": "EN", "ENGLISH": "EN", "OTHER": "OTHER", "UNCLEAR": "UNCLEAR"}
MATCHED = ("nomor", "nama+set")
OG_IMAGE = (re.compile(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]*content=["\']([^"\']+)', re.I),
            re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image', re.I))
STOCK = re.compile(r"^Sisa\s+(\d+)\b")


class FetchError(Exception):
    pass


def fetch(url, timeout=30, limit=6_000_000):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "id,en;q=0.5"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.read(limit), r.headers.get("Content-Type", "")
    except Exception as e:  # network, HTTP status, timeout
        raise FetchError(f"{url}: {e}") from e


def fetch_html(url):
    return fetch(url, limit=3_000_000)[0].decode("utf-8", "replace")


# ---- search page parsing -------------------------------------------------------

class CardLinks(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.cards, self._cur, self._text = base, {}, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "/listing/" in (a.get("href") or ""):
            u = urlsplit(urljoin(self.base, a["href"]))
            self._cur = urlunsplit((u.scheme, u.netloc, u.path, "", ""))
            self._text = []
            self.cards.setdefault(self._cur, {"url": self._cur, "text": "", "image": None})
        elif tag == "img" and self._cur:
            src = a.get("src") or a.get("data-src") or (a.get("srcset") or "").split(" ")[0]
            if src and not src.startswith("data:") and not self.cards[self._cur]["image"]:
                self.cards[self._cur]["image"] = urljoin(self.base, src)

    def handle_data(self, data):
        if self._cur:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._cur:
            text = " ".join(" ".join(self._text).split())
            if len(text) > len(self.cards[self._cur]["text"]):
                self.cards[self._cur]["text"] = text
            self._cur = None


def parse_search_html(html, base=BASE):
    p = CardLinks(base)
    p.feed(html)
    return [c for c in p.cards.values() if c["text"]]


def to_listing(card):
    text = card["text"]
    p = parse_card_text({"url": card["url"], "search_text": text, "grade_hint": ""})
    m = STOCK.match(text)
    title = re.split(r"\s+Rp\s?[\d.]+", re.sub(r"^Sisa\s+\d+\s+", "", text), 1)[0].strip()
    return {"url": card["url"], "text": text, "title": title, "ask": p["ask_idr"], "list": p["list_idr"],
            "city": p["city"], "stock": int(m.group(1)) if m else None, "image": card["image"]}


def crawl(queries, fetch_fn, pages, delay, page_param, save_dir=None, sleep=time.sleep, log=print):
    cards, diag = {}, []
    for q in queries:
        seen, fetched = set(), 0
        for n in range(1, pages + 1):
            url = f"{BASE}/search?q={quote_plus(q)}" + (f"&{page_param}={n}" if n > 1 else "")
            try:
                html = fetch_fn(url)
            except FetchError as e:
                log(f"  ! gagal ambil halaman: {e}")
                break
            fetched += 1
            if save_dir:
                os.makedirs(save_dir, exist_ok=True)
                name = re.sub(r"\W+", "_", q)[:50] + f"_p{n}.html"
                with open(os.path.join(save_dir, name), "w", encoding="utf-8") as fh:
                    fh.write(html)
            new = [c for c in parse_search_html(html) if c["url"] not in seen]
            seen.update(c["url"] for c in new)
            for c in new:
                prev = cards.setdefault(c["url"], c)
                prev["image"] = prev["image"] or c["image"]
            if not new:
                break
            sleep(delay)
        diag.append({"query": q, "pages": fetched, "listings": len(seen)})
    return cards, diag


# ---- language from the photo ---------------------------------------------------

def sniff_media_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return None


def normalize_lang(answer):
    m = re.search(r"\b(ID|INDONESIAN|INDONESIA|JP|JAPANESE|EN|ENGLISH|OTHER|UNCLEAR)\b", answer.upper())
    return LANG_ALIASES[m.group(1)] if m else "UNCLEAR"


def find_og_image(html):
    for rx in OG_IMAGE:
        m = rx.search(html)
        if m:
            return urljoin(BASE, m.group(1))
    return None


def make_vision_classifier(model):
    """Return classify(bytes, media_type) -> 'ID'|'JP'|'EN'|'OTHER'|'UNCLEAR'|None (None = try again later)."""
    import anthropic  # pip install anthropic (Python 3.10+)
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    extra = {} if "haiku" in model else {"output_config": {"effort": "low"}}
    usage = Counter()

    def classify(data, media_type):
        try:
            r = client.messages.create(
                model=model, max_tokens=1024, **extra,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.standard_b64encode(data).decode("ascii")}},
                    {"type": "text", "text": LANG_PROMPT}]}])
        except anthropic.AuthenticationError:
            raise SystemExit("ANTHROPIC_API_KEY salah atau belum diisi.")
        except anthropic.PermissionDeniedError:
            raise SystemExit("Kunci API tidak punya izin untuk model ini.")
        except anthropic.NotFoundError:
            raise SystemExit(f"Model tidak ditemukan: {model}")
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            print(f"  ! panggilan Claude gagal ({type(e).__name__}); dicoba lagi di putaran berikutnya")
            return None
        usage["input_tokens"] += r.usage.input_tokens
        usage["output_tokens"] += r.usage.output_tokens
        if r.stop_reason == "refusal":
            return "UNCLEAR"
        return normalize_lang("".join(b.text for b in r.content if b.type == "text"))

    classify.usage = usage
    return classify


class LangChecker:
    """Language per listing, cached in SQLite so each listing is looked at once."""

    def __init__(self, db, classify_fn, fetch_fn, image_fn, budget, now, delay=1.0, sleep=time.sleep, log=print):
        self.db, self.classify_fn, self.fetch_fn, self.image_fn = db, classify_fn, fetch_fn, image_fn
        self.budget, self.now, self.delay, self.sleep, self.log = budget, now, delay, sleep, log
        self.failures, self.calls = 0, 0

    def cached(self, url):
        row = self.db.execute("SELECT lang, lang_by, checked_at FROM lang WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        age = self.now - datetime.fromisoformat(row[2])
        if row[0] == "UNCLEAR" and age > timedelta(days=7):
            return None
        if row[1] == "title" and age > timedelta(days=1):
            return None
        return row[0]

    def store(self, url, lang, by, image_url):
        self.db.execute(
            "INSERT INTO lang(url, lang, lang_by, image_url, checked_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET lang=excluded.lang, lang_by=excluded.lang_by, "
            "image_url=excluded.image_url, checked_at=excluded.checked_at",
            (url, lang, by, image_url, self.now.isoformat()))

    def photo(self, listing):
        image_url = listing["image"]
        try:
            if not image_url:
                self.sleep(self.delay)
                image_url = find_og_image(self.fetch_fn(listing["url"]))
            if not image_url:
                return None, None, None
            self.sleep(self.delay)
            data, _ = self.image_fn(image_url)
        except FetchError as e:
            self.log(f"  ! foto tidak bisa diambil: {e}")
            return None, None, None
        media = sniff_media_type(data)
        return (data, media, image_url) if media else (None, None, image_url)

    def ensure(self, listing):
        hit = self.cached(listing["url"])
        if hit:
            return hit
        if self.classify_fn is None or self.budget <= 0 or self.failures >= 5:
            return None
        data, media, image_url = self.photo(listing)
        if data is None:  # no usable photo: fall back to an explicit "ID" in the title
            if classify_title(listing["title"])["lang"] == "ID":
                self.store(listing["url"], "ID", "title", image_url)
                return "ID"
            return None
        self.calls += 1
        self.budget -= 1
        lang = self.classify_fn(data, media)
        if lang is None:
            self.failures += 1
            return None
        self.failures = 0
        self.store(listing["url"], lang, "image", image_url)
        return lang


# ---- storage, statistics, output ----------------------------------------------

def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS listing(
            url TEXT PRIMARY KEY, title TEXT, ask_idr INTEGER, list_idr INTEGER, city TEXT, stock INTEGER,
            image_url TEXT, kind TEXT, card_set TEXT, card_number TEXT, conf TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE IF NOT EXISTS lang(
            url TEXT PRIMARY KEY, lang TEXT, lang_by TEXT, image_url TEXT, checked_at TEXT);
    """)
    return db


def quantile(values, p):
    v = sorted(values)
    k = (len(v) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return int(round(v[lo] + (v[hi] - v[lo]) * (k - lo)))


def load_types(latest):
    seen, types = set(), []
    for l in latest["listings"]:
        key = (l["name"], l["set_code"], l["card_number"], l["rarity"])
        if key not in seen:
            seen.add(key)
            types.append({"name": re.sub(r"[<>]", "", l["name"]), "set": l["set_code"],
                          "number": l["card_number"], "rarity": l["rarity"]})
    return types


def build_raw(db, types, now, max_age_hours):
    cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
    rows = db.execute(
        "SELECT l.url, l.title, l.ask_idr, l.city, l.stock, l.card_set, l.card_number, l.conf, l.last_seen, g.lang, g.lang_by "
        "FROM listing l LEFT JOIN lang g ON g.url = l.url WHERE l.last_seen >= ? AND l.kind = 'raw'", (cutoff,)).fetchall()
    by_type = {build_site.card_key(t["set"], t["number"]): [] for t in types}
    unmatched = unverified = 0
    for url, title, ask, city, stock, cset, cnum, conf, seen, lang, lang_by in rows:
        if conf not in MATCHED:
            unmatched += 1
            continue
        if stock is not None and stock <= 0:
            continue
        if lang == "ID":
            by_type[build_site.card_key(cset, cnum)].append((ask, url, title, city, seen, conf, lang_by))
        elif lang in (None, "UNCLEAR"):
            unverified += 1
    cards, newest = [], []
    for t in types:
        found = sorted(by_type[build_site.card_key(t["set"], t["number"])])
        if not found:
            continue
        prices = [f[0] for f in found]
        listings = []
        for ask, url, title, city, seen, conf, lang_by in found[:30]:
            item = {"url": url, "ask_idr": ask, "city": city, "observed_at": seen, "conf": conf,
                    "lang": "ID", "lang_by": lang_by, "title": title}
            cond = classify_title(title)["condition"]
            if cond:
                item["condition"] = cond
            listings.append(item)
            newest.append(seen)
        cards.append({"name": t["name"], "set_code": t["set"], "card_number": t["number"], "rarity": t["rarity"],
                      "stats": {"n": len(prices), "min": prices[0], "p25": quantile(prices, .25),
                                "median": quantile(prices, .5), "p75": quantile(prices, .75), "max": prices[-1]},
                      "listings": listings})
    cards.sort(key=lambda c: -c["stats"]["n"])
    return {"schema_version": 1, "kind": "RAW", "price_type": "OBSERVED_ASKING_ONLY", "language": "ID",
            "source": "toco_public", "generated_at": now.isoformat(),
            "coverage": {"cards": len(cards), "listings_counted": sum(c["stats"]["n"] for c in cards),
                         "last_observed_at": max(newest) if newest else now.isoformat()},
            "cards": cards, "unmatched": {"count": unmatched, "language_unverified": unverified}}


def run(cfg, fetch_fn, image_fn, classify_fn, now=None, sleep=time.sleep, log=print):
    now = now or datetime.now(timezone.utc)
    with open(cfg.latest, encoding="utf-8") as fh:
        types = load_types(json.load(fh))
    queries = list(dict.fromkeys(q for t in types for q in watch_queries.queries(t)["raw"]))
    if cfg.queries_limit:
        queries = queries[:cfg.queries_limit]
    log(f"{len(types)} jenis kartu, {len(queries)} kata kunci")
    cards, diag = crawl(queries, fetch_fn, cfg.pages, cfg.delay, cfg.page_param, cfg.save_html, sleep, log)
    for d in diag:
        log(f"  {d['query']!r}: {d['pages']} halaman, {d['listings']} listing")
    if not cards:
        log("Tidak ada listing yang terbaca. raw.json TIDAK ditulis. Jalankan dengan --dry-run --save-html DIR "
            "dan kirim file HTML-nya supaya parsernya bisa disesuaikan.")
        return None, {"listings": 0}

    db = open_db(cfg.db)
    ctr, to_check = Counter(), []
    for card in cards.values():
        l = to_listing(card)
        if not l["ask"] or l["ask"] <= 0:
            ctr["tanpa_harga"] += 1
            continue
        kind = classify_title(l["text"])["kind"]
        ctr["jenis_" + kind] += 1
        conf = cset = cnum = None
        if kind == "raw":
            m = build_site.match_type(l["text"], types)
            if m:
                conf, cset, cnum = m[1], types[m[0]]["set"], types[m[0]]["number"]
        ctr["cocok_" + (conf or "tidak")] += kind == "raw"
        db.execute(
            "INSERT INTO listing(url,title,ask_idr,list_idr,city,stock,image_url,kind,card_set,card_number,conf,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title, "
            "ask_idr=excluded.ask_idr, list_idr=excluded.list_idr, city=excluded.city, stock=excluded.stock, "
            "image_url=COALESCE(excluded.image_url, listing.image_url), kind=excluded.kind, card_set=excluded.card_set, "
            "card_number=excluded.card_number, conf=excluded.conf, last_seen=excluded.last_seen",
            (l["url"], l["title"], l["ask"], l["list"], l["city"], l["stock"], l["image"], kind, cset, cnum,
             conf, now.isoformat(), now.isoformat()))
        if conf in MATCHED and l["stock"] != 0:
            to_check.append(l)
    db.commit()

    checker = None
    if not cfg.dry_run:
        checker = LangChecker(db, classify_fn, fetch_fn, image_fn, cfg.max_checks, now, cfg.delay / 2, sleep, log)
        to_check.sort(key=lambda l: classify_title(l["title"])["lang"] != "ID")  # explicit "ID" first
        for l in to_check:
            checker.ensure(l)
        db.commit()
    raw = build_raw(db, types, now, cfg.max_age_hours)
    db.close()

    ctr.update({"listing_dibaca": len(cards), "foto_dicek_sekarang": checker.calls if checker else 0})
    return raw, dict(ctr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--latest", default="latest.json", help="daftar kartu yang dipantau")
    ap.add_argument("--out", default="raw.json")
    ap.add_argument("--db", default=os.path.expanduser("~/.egs-raw/raw.sqlite"), help="database di VPS, bukan di repo")
    ap.add_argument("--pages", type=int, default=5, help="halaman maksimal per kata kunci")
    ap.add_argument("--delay", type=float, default=2.0, help="jeda antar permintaan (detik)")
    ap.add_argument("--page-param", default="page", help="nama parameter halaman di URL pencarian (belum terverifikasi)")
    ap.add_argument("--queries-limit", type=int, help="batasi jumlah kata kunci (untuk uji)")
    ap.add_argument("--max-checks", type=int, default=150, help="batas cek foto per putaran (membatasi biaya)")
    ap.add_argument("--max-age-hours", type=float, default=36, help="listing dihitung kalau terlihat dalam rentang ini")
    ap.add_argument("--model", default=os.environ.get("EGS_LANG_MODEL", DEFAULT_MODEL))
    ap.add_argument("--dry-run", action="store_true", help="hanya baca dan cocokkan; tanpa panggilan API, tanpa menulis raw.json")
    ap.add_argument("--save-html", metavar="DIR", help="simpan halaman pencarian mentah untuk diagnosis")
    ap.add_argument("--test-image", metavar="FILE_OR_URL", help="uji pembaca bahasa pada satu foto, lalu selesai")
    cfg = ap.parse_args(argv)

    if cfg.test_image:
        if cfg.test_image.startswith("http"):
            data = fetch(cfg.test_image)[0]
        else:
            with open(cfg.test_image, "rb") as fh:
                data = fh.read()
        media = sniff_media_type(data)
        if not media:
            print("Format gambar tidak didukung (pakai JPEG, PNG, WEBP, atau GIF).")
            return 2
        classify = make_vision_classifier(cfg.model)
        print(f"model {cfg.model}: bahasa kartu = {classify(data, media)}  token={dict(classify.usage)}")
        return 0

    classify = None
    if not cfg.dry_run:
        try:
            classify = make_vision_classifier(cfg.model)
        except ImportError:
            print("Paket 'anthropic' belum terpasang. Jalankan: pip install anthropic (perlu Python 3.10+)")
            return 2
    raw, summary = run(cfg, fetch_html, fetch, classify)
    print(json.dumps(summary, ensure_ascii=False))
    if raw is None:
        return 3
    if classify is not None:
        print("pemakaian token:", dict(classify.usage), "model:", cfg.model)
    print(f"dihitung: {raw['coverage']['listings_counted']} listing di {raw['coverage']['cards']} kartu; "
          f"bahasa belum terbukti: {raw['unmatched']['language_unverified']}; tidak cocok kartu: {raw['unmatched']['count']}")
    if cfg.dry_run:
        print("--dry-run: raw.json tidak ditulis.")
        return 0
    errors, warnings = validate_raw.validate(raw)
    for w in warnings:
        print("WARN:", w)
    if errors:
        print("raw.json TIDAK ditulis karena tidak lolos validasi:")
        for e in errors:
            print("ERROR:", e)
        return 4
    tmp = cfg.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, cfg.out)
    print(f"ditulis: {cfg.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
