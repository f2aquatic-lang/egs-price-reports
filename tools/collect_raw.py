#!/usr/bin/env python3
"""Collect RAW (ungraded) Indonesian card listings from toco and write raw.json.

Runs on the VPS (needs toco.id and an Anthropic credential). See docs/vps-setup.md and
docs/raw-dataset.md.

How it works: for every tracked card it calls toco's own search endpoint (the same request the
website makes, verified on the VPS: POST /api/toco-api/search with path /listing-search, 45 results
per request, paged with from/size), keeps listings that are single raw cards and match a tracked card,
reads the card language, and writes raw.json. Language: clearly foreign text (Japanese, English,
Chinese in title or description) is skipped for free; everything else is decided from the listing
photo by a vision model (Claude by default, OpenAI as an option; official SDKs), cached per listing in
a local SQLite file, capped per run. --lang-mode text needs no API key at all: only listings whose text
says Indonesian are counted (marked lang_by "title").

  python3 tools/collect_raw.py --dry-run                  # no API calls, no raw.json
  python3 tools/collect_raw.py --sample-check "Zekrom N 210/193"   # 6 photos: see the language check work
  python3 tools/collect_raw.py --test-image card.jpg      # check the API key with one photo
  python3 tools/collect_raw.py                            # real run
  python3 tools/collect_raw.py --lang-mode text           # no API key: language from the text only
  python3 tools/collect_raw.py --provider openai --list-models   # models your OpenAI key can use
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
import validate_raw  # noqa: E402
import watch_queries  # noqa: E402
from classify import classify_title, guess_name, text_language  # noqa: E402

SITE = os.environ.get("EGS_TOCO_SITE", "https://toco.id")  # only changed in tests
SEARCH_URL = SITE + "/api/toco-api/search"
UA = "Mozilla/5.0 (compatible; egs-price-reports/1.0)"
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-4o-mini"}  # the OpenAI default is unverified: use --list-models
LANG_PROMPT = (
    "This is a photo from a marketplace listing of a Pokemon trading card. "
    "What language is the text printed on the card itself? "
    "Answer with exactly one word: ID (Indonesian), JP (Japanese), EN (English), "
    "OTHER (any other language), or UNCLEAR (cannot tell: blurry, glare, back of the card, "
    "or no card visible).")
LANG_ALIASES = {"ID": "ID", "INDONESIAN": "ID", "INDONESIA": "ID", "JP": "JP", "JAPANESE": "JP",
                "EN": "EN", "ENGLISH": "EN", "OTHER": "OTHER", "UNCLEAR": "UNCLEAR"}
MATCHED = ("nomor", "nama+set")
CATALOG = "katalog"  # not a tracked card: set code and card number are in the title, so the card is known by its number
CATALOG_LISTINGS = 10  # per catalog card in raw.json (tracked cards keep 30)
AGGREGATIONS = {"department_agg": {"terms": {"field": "department"}}, "category_agg": {"terms": {"field": "category"}},
                "type_agg": {"terms": {"field": "type"}}, "price_agg": {"terms": {"field": "price"}},
                "production_year_agg": {"terms": {"field": "production_year"}},
                "store_tier_agg": {"terms": {"field": "store_tier"}},
                "official_seller_agg": {"terms": {"field": "official_seller"}}}


class FetchError(Exception):
    pass


def fetch(url, timeout=30, limit=6_000_000):
    """Plain GET (photos). Returns (bytes, content_type)."""
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.read(limit), r.headers.get("Content-Type", "")
    except Exception as e:  # network, HTTP status, timeout
        raise FetchError(f"{url}: {e}") from e


def make_post(url=SEARCH_URL):
    def post(body):
        req = Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA,
            "Origin": SITE, "Referer": SITE + "/search"})
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read(8_000_000).decode("utf-8", "replace"))
        except Exception as e:
            raise FetchError(f"{url}: {e}") from e
    return post


def search_body(query, offset, size):
    return {"path": "/listing-search", "body": {
        "from": offset, "size": size, "search_operator": "and", "include_quality_score": True,
        "sort_mode": "relevance_only", "query": query,
        "filters": {"web": True, "add_to_cart": True, "special_daytop": False, "safe_search": True},
        "aggregations": AGGREGATIONS}}


def crawl(queries, post_fn, pages, size, delay, save_dir=None, sleep=time.sleep, log=print):
    """Run every query page by page. Returns ({slug: item}, per-query diagnostics)."""
    items, diag = {}, []
    for q in queries:
        seen, fetched, total = set(), 0, None
        for n in range(pages):
            try:
                data = post_fn(search_body(q, n * size, size))
            except FetchError as e:
                log(f"  ! pencarian gagal: {e}")
                break
            fetched += 1
            total = data.get("total", total)
            if save_dir and n == 0:
                os.makedirs(save_dir, exist_ok=True)
                with open(os.path.join(save_dir, re.sub(r"\W+", "_", q)[:50] + ".json"), "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
            new = [r for r in data.get("results") or [] if r.get("slug") and r["slug"] not in seen]
            seen.update(r["slug"] for r in new)
            for r in new:
                items.setdefault(r["slug"], r)
            if not new or (total is not None and (n + 1) * size >= total):
                break
            sleep(delay)
        diag.append({"query": q, "pages": fetched, "listings": len(seen), "total": total})
    return items, diag


def to_listing(item):
    price, special = item.get("price"), item.get("price_special")
    ask = special if special and price and special < price else price
    photos = item.get("photos") or []
    active = not item.get("archived") and not item.get("deleted") and item.get("show") is not False
    return {
        "url": f"{SITE}/listing/{item.get('slug')}",
        "title": (item.get("title") or "").strip(),
        "description": (item.get("description") or "").strip(),
        "ask": ask, "list": price if ask != price else None,
        "city": item.get("city") or "?",
        "stock": item.get("stock") if active else 0,
        "image": item.get("primary_photo") or (photos[0] if photos else None),
    }


SET_SUFFIXES = ("", "indonesia", "RR", "AR", "SR", "SAR", "UR")  # one query per slice of a set; AND search, so each is a different list


def top_sets(listings, limit):
    """Set codes that appear most among Indonesian raw cards in this run's results (at least twice)."""
    counts = Counter()
    for l in listings:
        c = classify_title(l["title"])
        if c["kind"] == "raw" and c["set"] and text_language(l["title"], l["description"]) == "ID":
            counts[c["set"]] += 1
    return [code for code, n in counts.most_common(limit) if n >= 2]


def crawl_sets(codes, items, post_fn, size, delay, max_pages, budget, min_share, sleep=time.sleep, log=print):
    """Page deep through every set code, round-robin across its queries, until the budget is spent.

    A query stops when a page brings nothing new or fewer than `min_share` of its listings name the set,
    which means the engine has run out of cards of that set. New listings are added to `items`.
    """
    state = {}
    for code in codes:
        for suffix in SET_SUFFIXES:
            q = f"{code} {suffix}".strip()
            state[q] = {"code": code, "re": re.compile(rf"\b{re.escape(code)}\b", re.I), "page": 0, "seen": set(),
                        "total": None, "done": False, "relevant": 0}
    requests = 0
    while requests < budget and not all(st["done"] for st in state.values()):
        for q, st in state.items():
            if st["done"] or requests >= budget:
                continue
            try:
                data = post_fn(search_body(q, st["page"] * size, size))
            except FetchError as e:
                log(f"  ! pencarian gagal: {e}")
                st["done"] = True
                continue
            requests += 1
            results = data.get("results") or []
            new = [r for r in results if r.get("slug") and r["slug"] not in st["seen"]]
            st["seen"].update(r["slug"] for r in new)
            for r in new:
                items.setdefault(r["slug"], r)
            share = sum(1 for r in results if st["re"].search(r.get("title") or "")) / len(results) if results else 0
            st["page"] += 1
            st["relevant"] += sum(1 for r in new if st["re"].search(r.get("title") or ""))
            st["total"] = data.get("total", st["total"])
            if (not new or share < min_share or st["page"] >= max_pages
                    or (st["total"] is not None and st["page"] * size >= st["total"])):
                st["done"] = True
            sleep(delay)
    for q, st in state.items():
        log(f"  {q!r}: {st['page']} halaman, {len(st['seen'])} listing ({st['relevant']} menyebut {st['code']})"
            f"{'' if st['done'] else ' [anggaran habis]'}")
    return requests


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


def make_vision_classifier(model, provider="anthropic"):
    """Return classify(bytes, media_type) -> 'ID'|'JP'|'EN'|'OTHER'|'UNCLEAR'|None (None = try again later)."""
    return _openai_classifier(model) if provider == "openai" else _anthropic_classifier(model)


def list_openai_models():
    import openai  # pip install openai
    return sorted(m.id for m in openai.OpenAI().models.list())


def _openai_classifier(model, max_retries=2):
    import openai  # pip install openai
    client = openai.OpenAI(max_retries=max_retries)  # reads OPENAI_API_KEY
    usage = Counter()

    def classify(data, media_type):
        url = f"data:{media_type};base64,{base64.standard_b64encode(data).decode('ascii')}"
        try:
            r = client.chat.completions.create(
                model=model, max_completion_tokens=2000,  # room for reasoning models; short answers cost little
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": LANG_PROMPT},
                    {"type": "image_url", "image_url": {"url": url}}]}])
        except openai.AuthenticationError:
            raise SystemExit("OPENAI_API_KEY salah atau belum diisi.")
        except openai.PermissionDeniedError:
            raise SystemExit("Kunci API tidak punya izin untuk model ini.")
        except openai.NotFoundError:
            raise SystemExit(f"Model tidak ditemukan: {model}. Lihat daftarnya dengan --provider openai --list-models")
        except openai.BadRequestError as e:  # e.g. a model without image input: every call would fail the same way
            raise SystemExit(f"OpenAI menolak permintaan ({e.message}). Coba model lain: --list-models")
        except openai.RateLimitError as e:
            if "insufficient_quota" in str(e):
                raise SystemExit("Saldo API OpenAI habis atau belum diisi (insufficient_quota). Langganan ChatGPT bukan saldo API.")
            print("  ! OpenAI membatasi laju; dicoba lagi di putaran berikutnya")
            return None
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            print(f"  ! panggilan OpenAI gagal ({type(e).__name__}); dicoba lagi di putaran berikutnya")
            return None
        if r.usage:
            usage["input_tokens"] += r.usage.prompt_tokens
            usage["output_tokens"] += r.usage.completion_tokens
        return normalize_lang(r.choices[0].message.content or "")

    classify.usage = usage
    return classify


def _anthropic_classifier(model):
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
    """Language per listing, cached in SQLite so each listing is decided once."""

    def __init__(self, db, classify_fn, image_fn, budget, now, delay=0.5, sleep=time.sleep, log=print, text_only=False):
        self.db, self.classify_fn, self.image_fn, self.text_only = db, classify_fn, image_fn, text_only
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
        if not listing["image"]:
            return None, None
        try:
            self.sleep(self.delay)
            data, _ = self.image_fn(listing["image"])
        except FetchError as e:
            self.log(f"  ! foto tidak bisa diambil: {e}")
            return None, None
        media = sniff_media_type(data)
        return (data, media) if media else (None, None)

    def ensure(self, listing):
        hit = self.cached(listing["url"])
        if hit:
            return hit
        hint = text_language(listing["title"], listing["description"])
        if hint == "foreign":
            self.store(listing["url"], "FOREIGN", "text", None)  # free: no photo needed
            return "FOREIGN"
        if self.text_only:  # no photo check: only an explicit "Indonesia" in the text counts
            if hint == "ID":
                self.store(listing["url"], "ID", "title", listing["image"])
                return "ID"
            return None
        if self.classify_fn is None or self.budget <= 0 or self.failures >= 5:
            return None
        data, media = self.photo(listing)
        if data is None:  # no usable photo: fall back to an explicit "ID" in the title
            if classify_title(listing["title"])["lang"] == "ID":
                self.store(listing["url"], "ID", "title", listing["image"])
                return "ID"
            return None
        self.calls += 1
        self.budget -= 1
        lang = self.classify_fn(data, media)
        if lang is None:
            self.failures += 1
            return None
        self.failures = 0
        self.store(listing["url"], lang, "image", listing["image"])
        return lang


# ---- storage, statistics, output ----------------------------------------------

def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS listing(
            url TEXT PRIMARY KEY, title TEXT, ask_idr INTEGER, list_idr INTEGER, city TEXT, stock INTEGER,
            image_url TEXT, kind TEXT, card_set TEXT, card_number TEXT, conf TEXT, first_seen TEXT, last_seen TEXT, variant TEXT);
        CREATE TABLE IF NOT EXISTS lang(
            url TEXT PRIMARY KEY, lang TEXT, lang_by TEXT, image_url TEXT, checked_at TEXT);
        CREATE TABLE IF NOT EXISTS snapshot(
            day TEXT, card_set TEXT, card_number TEXT, n INTEGER, median INTEGER, min_idr INTEGER, max_idr INTEGER,
            taken_at TEXT, PRIMARY KEY(day, card_set, card_number));
    """)
    if "variant" not in {r[1] for r in db.execute("PRAGMA table_info(listing)")}:  # databases made before foil versions
        db.execute("ALTER TABLE listing ADD COLUMN variant TEXT")
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


WIB = timezone(timedelta(hours=7))
HISTORY_DAYS = 60


def denominator_sets(titles):
    """Which set a card total belongs to (193 -> MA3), learned from Indonesian titles that show both."""
    votes = defaultdict(Counter)
    for title in titles:
        c = classify_title(title)
        if c["kind"] == "raw" and c["number"] and c["set"] and text_language(title) == "ID":
            votes[c["number"].split("/")[1]][c["set"]] += 1
    out = {}
    for total, counter in votes.items():
        (code, n), seen = counter.most_common(1)[0], sum(counter.values())
        if n >= 3 and n / seen >= 0.9:
            out[total] = code
    return out


def catalog_key(title, sets_by_total):
    """(set, number) of an untracked raw card named only by what the title shows, else None."""
    c = classify_title(title)
    if c["kind"] != "raw" or not c["number"]:
        return None
    code = c["set"] or sets_by_total.get(c["number"].split("/")[1])
    return (code, c["number"]) if code else None


def load_official(path):
    """{card_key: official card} from catalog.json (tools/fetch_catalog.py); {} when there is no file."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return build_site.official_index(data["cards"], data.get("sets"))


def load_defaults(path):
    """{rarity: rupiah} base prices for cards without a listing (tools/default_prices.json)."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {r: int(p) for r, p in json.load(fh).get("by_rarity", {}).items() if int(p) > 0}


def reconcile(key, title, official, by_set_num):
    """Put a (set, number) read from a title onto the official card when sellers wrote the wrong total.

    '1/185' for a card that is 001/139 officially: same set, same number, and the official name appears in the
    title. A number that is only a side series ('1/30' Pikachu next to 1/130 Exeggcute) stays as it is.
    """
    code, number = key
    if build_site.card_key(code, number) in official:
        return key
    num = int(number.split("/")[0])
    low = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    hits = []
    for o in by_set_num.get((code.lower(), num), []):
        words = [w for w in re.sub(r"[^a-z0-9 ]", " ", o["name"].lower()).split() if w != "ex"]
        if words and all(w in low.split() for w in words):
            hits.append(o)
    if len(hits) == 1:
        return code, re.sub(r"^0+(?=\d)", "", hits[0]["number"])
    return key


def majority(values):
    """Most common non-empty value; ties go to the shortest, then alphabetical."""
    counts = Counter(v for v in values if v)
    return min(counts, key=lambda v: (-counts[v], len(v), v)) if counts else ""


def build_raw(db, types, now, max_age_hours, record=True, catalog=False, official=None, defaults=None):
    cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
    rows = db.execute(
        "SELECT l.url, l.title, l.ask_idr, l.city, l.stock, l.card_set, l.card_number, l.conf, l.last_seen, g.lang, g.lang_by, l.image_url, l.variant "
        "FROM listing l LEFT JOIN lang g ON g.url = l.url WHERE l.last_seen >= ? AND l.kind = 'raw'", (cutoff,)).fetchall()
    by_type = {build_site.card_key(t["set"], t["number"]): [] for t in types}
    found_catalog = defaultdict(list)
    unmatched = unverified = 0
    for url, title, ask, city, stock, cset, cnum, conf, seen, lang, lang_by, image, variant in rows:
        variant = variant or ""
        if conf not in MATCHED and not (catalog and conf == CATALOG):
            unmatched += 1
            continue
        if stock is not None and stock <= 0:
            continue
        if lang == "ID":
            key = build_site.card_key(cset, cnum, variant if conf == CATALOG else "")
            row = (ask, url, title, city, seen, conf, lang_by, image)
            if key in by_type:  # a catalog hit on a tracked card is that card
                by_type[key].append(row[:5] + ("nomor",) + row[6:] if conf == CATALOG else row)
            else:
                found_catalog[key].append((row, cset, cnum, variant))
        elif lang in (None, "UNCLEAR"):
            unverified += 1
    cards, newest = [], []
    for t in types:
        found = sorted(by_type[build_site.card_key(t["set"], t["number"])])
        if not found:
            continue
        prices = [f[0] for f in found]
        listings = []
        for ask, url, title, city, seen, conf, lang_by, image in found[:30]:
            item = {"url": url, "ask_idr": ask, "city": city, "observed_at": seen, "conf": conf,
                    "lang": "ID", "lang_by": lang_by, "title": title}
            if image:
                item["photo"] = image
            cond = classify_title(title)["condition"]
            if cond:
                item["condition"] = cond
            listings.append(item)
            newest.append(seen)
        stats = {"n": len(prices), "min": prices[0], "p25": quantile(prices, .25),
                 "median": quantile(prices, .5), "p75": quantile(prices, .75), "max": prices[-1]}
        if record:  # one row per card per WIB day; the last run of the day wins
            db.execute(
                "INSERT INTO snapshot(day, card_set, card_number, n, median, min_idr, max_idr, taken_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(day, card_set, card_number) DO UPDATE SET n=excluded.n, median=excluded.median, "
                "min_idr=excluded.min_idr, max_idr=excluded.max_idr, taken_at=excluded.taken_at",
                (now.astimezone(WIB).date().isoformat(), t["set"], t["number"], stats["n"], stats["median"],
                 stats["min"], stats["max"], now.isoformat()))
        history = [{"date_wib": d, "n": n, "median": m, "min": lo} for d, n, m, lo in db.execute(
            "SELECT day, n, median, min_idr FROM snapshot WHERE card_set=? AND card_number=? ORDER BY day DESC LIMIT ?",
            (t["set"], t["number"], HISTORY_DAYS))][::-1]
        cards.append({"name": t["name"], "set_code": t["set"], "card_number": t["number"], "rarity": t["rarity"],
                      "stats": stats, "history": history, "listings": listings})
    cards.sort(key=lambda c: -c["stats"]["n"])
    extra = []
    for key, items in found_catalog.items():
        found = sorted(r for r, _, _, _ in items)
        cset, cnum, variant = items[0][1], items[0][2], items[0][3]
        snap_no = f"{cnum}#{variant}" if variant else cnum  # a foil version has its own history
        prices = [f[0] for f in found]
        stats = {"n": len(prices), "min": prices[0], "p25": quantile(prices, .25),
                 "median": quantile(prices, .5), "p75": quantile(prices, .75), "max": prices[-1]}
        listings = []
        for ask, url, title, city, seen, conf, lang_by, image in found[:CATALOG_LISTINGS]:
            item = {"url": url, "ask_idr": ask, "city": city, "observed_at": seen, "conf": conf,
                    "lang": "ID", "lang_by": lang_by, "title": title}
            if image:
                item["photo"] = image
            listings.append(item)
            newest.append(seen)
        if record:
            db.execute(
                "INSERT INTO snapshot(day, card_set, card_number, n, median, min_idr, max_idr, taken_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(day, card_set, card_number) DO UPDATE SET n=excluded.n, median=excluded.median, "
                "min_idr=excluded.min_idr, max_idr=excluded.max_idr, taken_at=excluded.taken_at",
                (now.astimezone(WIB).date().isoformat(), cset, snap_no, stats["n"], stats["median"],
                 stats["min"], stats["max"], now.isoformat()))
        history = [{"date_wib": d, "n": n, "median": m, "min": lo} for d, n, m, lo in db.execute(
            "SELECT day, n, median, min_idr FROM snapshot WHERE card_set=? AND card_number=? ORDER BY day DESC LIMIT ?",
            (cset, snap_no, HISTORY_DAYS))][::-1]
        titles = [r[2] for r, _, _, _ in items]
        extra.append({"name": majority(guess_name(t) for t in titles), "set_code": cset, "card_number": cnum,
                      "rarity": majority((classify_title(t)["rarity"] or [""])[0] for t in titles),
                      "catalog": True, "stats": stats, "history": history, "listings": listings})
        if variant:
            extra[-1]["variant"] = variant
    official = official or {}
    for c in extra:  # the official name and rarity beat what could be guessed from a seller's title
        o = official.get(build_site.card_key(c["set_code"], c["card_number"], c.get("variant", ""))) \
            or official.get(build_site.card_key(c["set_code"], c["card_number"]))
        if o:
            c["name"], c["rarity"] = o["name"], o.get("rarity") or c["rarity"]
    extra.sort(key=lambda c: (-c["stats"]["n"], c["set_code"], c["card_number"]))
    cards += extra
    # a card nobody lists yet still gets a price when its rarity has a base price (replaced as soon as a listing exists)
    priced = {build_site.card_key(c["set_code"], c["card_number"], c.get("variant", "")) for c in cards}
    base_priced = []
    regular = {key: o for key, o in official.items() if not o["variant"]}  # only the regular card gets a base price, not its holo versions
    main_den = {code: Counter(o["den"] for o in regular.values() if o["set"] == code and o.get("den")).most_common(1)[0][0]
                for code in {o["set"] for o in regular.values() if o.get("den")}}
    for key, o in regular.items():
        # "-" is the price of a card without a rarity mark, only inside the set's own numbering (not its n/30 Pikachu or secret rares)
        no_mark_main = (not o.get("rarity") and o.get("den") == main_den.get(o["set"]) and (o.get("num") or 0) <= o["den"])
        price = (defaults or {}).get(o.get("rarity")) if o.get("rarity") else (defaults or {}).get("-") if no_mark_main else None
        if price and key not in priced:
            base_priced.append({"name": o["name"], "set_code": o["set"].upper(), "card_number": re.sub(r"^0+(?=\d)", "", o["number"]),
                                "rarity": o.get("rarity") or "", "catalog": True, "price_basis": "default",
                                "stats": {"n": 0, "min": price, "p25": price, "median": price, "p75": price, "max": price},
                                "listings": []})
    base_priced.sort(key=lambda c: (c["set_code"], int(c["card_number"].split("/")[1]), int(c["card_number"].split("/")[0])))
    cards += base_priced
    return {"schema_version": 1, "kind": "RAW", "price_type": "OBSERVED_ASKING_ONLY", "language": "ID",
            "source": "toco_public", "generated_at": now.isoformat(),
            "coverage": {"cards": len(cards), "catalog_cards": len(extra) + len(base_priced), "default_priced_cards": len(base_priced),
                         "listings_counted": sum(c["stats"]["n"] for c in cards),
                         "last_observed_at": max(newest) if newest else now.isoformat()},
            "cards": cards, "unmatched": {"count": unmatched, "language_unverified": unverified}}


def run(cfg, post_fn, image_fn, classify_fn, now=None, sleep=time.sleep, log=print):
    now = now or datetime.now(timezone.utc)
    with open(cfg.latest, encoding="utf-8") as fh:
        types = load_types(json.load(fh))
    queries = list(dict.fromkeys(q for t in types for q in watch_queries.queries(t)["raw"]))
    if cfg.queries_limit:
        queries = queries[:cfg.queries_limit]
    log(f"{len(types)} jenis kartu, {len(queries)} kata kunci")
    items, diag = crawl(queries, post_fn, cfg.pages, cfg.size, cfg.delay, cfg.save_json, sleep, log)
    for d in diag:
        log(f"  {d['query']!r}: {d['pages']} halaman, {d['listings']} listing dari total {d['total']}")
    if items and getattr(cfg, "catalog", False) and getattr(cfg, "sets", 0) > 0:
        codes = top_sets([to_listing(i) for i in items.values()], cfg.sets)
        log(f"telusuri set: {', '.join(codes) or '(tidak ada)'} (anggaran {cfg.set_budget} permintaan)")
        before = len(items)
        used = crawl_sets(codes, items, post_fn, cfg.size, cfg.delay, cfg.set_pages, cfg.set_budget, cfg.set_min_share, sleep, log)
        log(f"  {used} permintaan, {len(items) - before} listing baru")
    if not items:
        log("Tidak ada listing yang terbaca. raw.json TIDAK ditulis.")
        return None, {"listing_dibaca": 0}

    db = open_db(cfg.db)
    ctr, to_check, catalog_on = Counter(), [], getattr(cfg, "catalog", False)
    listings = [l for l in map(to_listing, items.values())]
    sets_by_total = denominator_sets(l["title"] for l in listings) if catalog_on else {}
    official = load_official(getattr(cfg, "catalog_json", None)) if catalog_on else {}
    by_set_num = defaultdict(list)
    for o in official.values():
        if o.get("num") is not None and not o["variant"]:
            by_set_num[(o["set"].lower(), o["num"])].append(o)
    for l in listings:
        if not l["ask"] or l["ask"] <= 0:
            ctr["tanpa_harga"] += 1
            continue
        kind = classify_title(l["title"])["kind"]
        ctr["jenis_" + kind] += 1
        conf = cset = cnum = None
        variant = None
        if kind == "raw":
            m = build_site.match_type(l["title"], types)
            if m:
                conf, cset, cnum = m[1], types[m[0]]["set"], types[m[0]]["number"]
            if conf not in MATCHED and catalog_on:
                key = catalog_key(l["title"], sets_by_total)
                if key:
                    conf, (cset, cnum) = CATALOG, reconcile(key, l["title"], official, by_set_num)
                    variant = classify_title(l["title"])["variant"]
                    regular_card = official.get(build_site.card_key(cset, cnum))
                    if regular_card and re.search(r"\bball\b", regular_card["name"], re.I):
                        variant = ""  # the trainer card "Poke Ball" is not a Poke Ball holo version of something
            ctr["cocok_" + (conf or "tidak")] += 1
        db.execute(
            "INSERT INTO listing(url,title,ask_idr,list_idr,city,stock,image_url,kind,card_set,card_number,conf,first_seen,last_seen,variant) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title, "
            "ask_idr=excluded.ask_idr, list_idr=excluded.list_idr, city=excluded.city, stock=excluded.stock, "
            "image_url=COALESCE(excluded.image_url, listing.image_url), kind=excluded.kind, card_set=excluded.card_set, "
            "card_number=excluded.card_number, conf=excluded.conf, last_seen=excluded.last_seen, variant=excluded.variant",
            (l["url"], l["title"], l["ask"], l["list"], l["city"], l["stock"], l["image"], kind, cset, cnum,
             conf, now.isoformat(), now.isoformat(), variant or None))
        if (conf in MATCHED or conf == CATALOG) and l["stock"] != 0:
            to_check.append({**l, "tracked": conf in MATCHED})
    db.commit()

    # dry run: classify_fn is None, so only the free text filter runs and nothing is fetched or paid for
    checker = LangChecker(db, None if cfg.dry_run else classify_fn, image_fn, cfg.max_checks, now, cfg.delay / 2, sleep, log,
                          text_only=getattr(cfg, "lang_mode", "photo") == "text")
    # listings whose text already says "Indonesia" first, so a capped budget is spent on likely hits
    to_check.sort(key=lambda l: (not l["tracked"], text_language(l["title"], l["description"]) != "ID"))
    for l in to_check:
        checker.ensure(l)
    db.commit()
    raw = build_raw(db, types, now, cfg.max_age_hours, record=not cfg.dry_run, catalog=catalog_on,
                    official=load_official(getattr(cfg, "catalog_json", None)),
                    defaults=load_defaults(getattr(cfg, "default_prices", None)))
    db.commit()  # the daily snapshots written by build_raw must survive the run
    db.close()
    ctr.update({"listing_dibaca": len(items), "foto_dicek_sekarang": checker.calls})
    return raw, dict(ctr)


def sample_check(cfg, post_fn, image_fn, classify_fn, log=print):
    """Show the photo language check on a few real listings of one query (nothing is stored)."""
    data = post_fn(search_body(cfg.sample_check, 0, 45))
    shown = 0
    for item in data.get("results") or []:
        if shown >= cfg.sample_size:
            break
        l = to_listing(item)
        if not l["image"]:
            continue
        try:
            photo, _ = image_fn(l["image"])
        except FetchError as e:
            log(f"  ! {e}")
            continue
        media = sniff_media_type(photo)
        if not media:
            continue
        shown += 1
        by_photo = classify_fn(photo, media)
        log(f"foto={by_photo} | teks={text_language(l['title'], l['description'])} | jenis={classify_title(l['title'])['kind']} "
            f"| Rp{l['ask']:,} | {l['title'][:70]}")
    return shown


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up in journalctl while the run is going
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--latest", default="latest.json", help="daftar kartu yang dipantau")
    ap.add_argument("--out", default="raw.json")
    ap.add_argument("--db", default=os.path.expanduser("~/.egs-raw/raw.sqlite"), help="database di VPS, bukan di repo")
    ap.add_argument("--pages", type=int, default=5, help="halaman maksimal per kata kunci")
    ap.add_argument("--size", type=int, default=45, help="listing per permintaan")
    ap.add_argument("--delay", type=float, default=1.5, help="jeda antar permintaan (detik)")
    ap.add_argument("--queries-limit", type=int, help="batasi jumlah kata kunci (untuk uji)")
    ap.add_argument("--max-checks", type=int, default=150, help="batas cek foto per putaran (membatasi biaya)")
    ap.add_argument("--sets", type=int, default=8, help="telusuri paling banyak N set yang muncul (0 = matikan)")
    ap.add_argument("--set-budget", type=int, default=400, help="batas permintaan pencarian untuk penelusuran set")
    ap.add_argument("--set-pages", type=int, default=25, help="halaman terdalam per kata kunci set")
    ap.add_argument("--set-min-share", type=float, default=0.3,
                    help="berhenti kalau kurang dari bagian ini dari satu halaman menyebut kode set")
    ap.add_argument("--catalog-json", default="catalog.json" if os.path.exists("catalog.json") else None,
                    help="katalog resmi (tools/fetch_catalog.py): nama dan rarity resmi, dan kartu tanpa listing")
    ap.add_argument("--default-prices", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_prices.json"),
                    help="harga dasar per rarity untuk kartu tanpa listing (hanya berlaku bersama --catalog-json)")
    ap.add_argument("--no-catalog", dest="catalog", action="store_false",
                    help="hanya kartu yang dipantau (bawaan: kartu lain yang nomornya terbaca di judul ikut didata)")
    ap.add_argument("--max-age-hours", type=float, default=36, help="listing dihitung kalau terlihat dalam rentang ini")
    ap.add_argument("--provider", choices=("anthropic", "openai"), default=os.environ.get("EGS_LANG_PROVIDER", "anthropic"),
                    help="penyedia model foto (kunci: ANTHROPIC_API_KEY atau OPENAI_API_KEY)")
    ap.add_argument("--model", default=os.environ.get("EGS_LANG_MODEL"), help="nama model; bawaan tergantung penyedia")
    ap.add_argument("--lang-mode", choices=("photo", "text"), default=os.environ.get("EGS_LANG_MODE", "photo"),
                    help="photo = bahasa dari foto (perlu kunci); text = hanya dari teks, tanpa kunci")
    ap.add_argument("--list-models", action="store_true", help="tampilkan model yang bisa dipakai kunci OpenAI Anda, lalu selesai")
    ap.add_argument("--dry-run", action="store_true", help="hanya baca dan cocokkan; tanpa panggilan Claude, tanpa menulis raw.json")
    ap.add_argument("--save-json", metavar="DIR", help="simpan jawaban pencarian mentah untuk diagnosis")
    ap.add_argument("--test-image", metavar="FILE_OR_URL", help="uji pembaca bahasa pada satu foto, lalu selesai")
    ap.add_argument("--sample-check", metavar="QUERY", help="cek bahasa dari foto pada beberapa listing nyata, lalu selesai")
    ap.add_argument("--sample-size", type=int, default=6)
    cfg = ap.parse_args(argv)
    cfg.model = cfg.model or DEFAULT_MODELS[cfg.provider]

    if cfg.list_models:
        if cfg.provider != "openai":
            print("--list-models hanya untuk --provider openai")
            return 2
        for name in list_openai_models():
            print(name)
        return 0

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
        classify = make_vision_classifier(cfg.model, cfg.provider)
        print(f"{cfg.provider} {cfg.model}: bahasa kartu = {classify(data, media)}  token={dict(classify.usage)}")
        return 0

    if cfg.sample_check:
        try:
            classify = make_vision_classifier(cfg.model, cfg.provider)
        except ImportError:
            print(f"Paket '{cfg.provider}' belum terpasang. Jalankan: pip install {cfg.provider}")
            return 2
        n = sample_check(cfg, make_post(), fetch, classify)
        print(f"{n} foto dicek, {cfg.provider} {cfg.model}, token={dict(classify.usage)}")
        return 0

    classify = None
    if not cfg.dry_run and cfg.lang_mode == "photo":
        try:
            classify = make_vision_classifier(cfg.model, cfg.provider)
        except ImportError:
            print(f"Paket '{cfg.provider}' belum terpasang. Jalankan: pip install {cfg.provider}")
            return 2
    raw, summary = run(cfg, make_post(), fetch, classify)
    print(json.dumps(summary, ensure_ascii=False))
    if raw is None:
        return 3
    if classify is not None:
        print("pemakaian token:", dict(classify.usage), "model:", f"{cfg.provider} {cfg.model}")
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
