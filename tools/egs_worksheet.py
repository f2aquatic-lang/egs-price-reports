#!/usr/bin/env python3
"""Worksheet for the owner: card photos with empty columns beside them, filled in by hand and read back by the EGS watch.

Two kinds of rows, one CSV (opens in Google Sheets; the photo column is an =IMAGE() formula):
  antrean  a listing the identity check could not settle: the photo, the title, the asking price and why it is in doubt.
           Fill nomor / rarity / bahasa / grade from the photo (what you can read), or write "tolak" in keputusan to throw it out.
  pasar    a card and grade the watch already prices from toco: the official card picture and the toco price.
           Fill bahasa and the price and link of a Tokopedia and/or a Shopee listing you looked at yourself.

Nothing here opens Shopee or Tokopedia; the numbers are what a person typed. Rows are read back by egs_watch.py --worksheet FILE_OR_URL
(a Google Sheets "publish to the web as CSV" link works, so typing in the sheet is enough). A value that is not understood is
reported and the whole row is skipped, never half applied or guessed. Standard library only.
"""
import csv
import io
import re
from urllib.parse import urlsplit

COLUMNS = ["jenis", "foto", "kartu", "grade", "harga_toco", "nomor", "rarity", "bahasa", "keputusan",
           "harga_tokopedia", "link_tokopedia", "harga_shopee", "link_shopee", "catatan", "alasan_ragu", "link_toco", "kunci"]
GRADES = {"10", "9.5", "9", "8.5", "8", "7.5", "7"}
LANGUAGES = {"id": "ID", "indo": "ID", "indonesia": "ID", "indonesian": "ID", "en": "EN", "inggris": "EN", "english": "EN",
             "jp": "JP", "jepang": "JP", "japanese": "JP"}
REJECT_WORDS = {"tolak", "bukan", "salah", "buang"}
NUMBER = re.compile(r"(\d{1,3})\s*/\s*(\d{2,3})")


def image_formula(url):
    return '=IMAGE("' + url.replace('"', "") + '")' if url else ""


def card_images(catalog):
    """(set, number) -> official picture of the first catalog entry with that number."""
    out = {}
    for c in (catalog or {}).get("cards", []):
        out.setdefault((c["set"], c["number"]), c.get("image"))
    return out


def rows_for(export, catalog):
    """The worksheet rows: first the priced cards (pasar), then the review queue (antrean) with the dearest listing on top."""
    images = card_images(catalog)
    rows = []
    for c in export["cards"]:
        for g in c["grades"]:
            rows.append({"jenis": "pasar", "foto": image_formula(images.get((c["set_code"], c["card_number"]))),
                         "kartu": " ".join(x for x in (c["name"], c["card_number"], c["set_code"], c["rarity"]) if x), "grade": g["grade"],
                         "harga_toco": g["median"], "link_toco": (g.get("urls") or [""])[0],
                         "kunci": "|".join((c["name"], c["set_code"], c["card_number"], c["rarity"] or "", g["grade"]))})
    for q in export["queue"]:
        rows.append({"jenis": "antrean", "foto": image_formula(q.get("photo")), "kartu": q["title"], "harga_toco": q["asking_idr"],
                     "alasan_ragu": "; ".join(r["text"] for r in q["reasons"]), "link_toco": q["url"], "kunci": q["url"]})
    return rows


def make(export, catalog, path):
    """Write the worksheet; returns {"pasar": n, "antrean": n}. UTF-8 with a byte-order mark so that Sheets and Excel read the accents."""
    rows = rows_for(export, catalog)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return {"pasar": sum(1 for r in rows if r["jenis"] == "pasar"), "antrean": sum(1 for r in rows if r["jenis"] == "antrean")}


def _clean(value):
    return (value or "").strip()


def _read_queue_row(row):
    """(read, problem) for an antrean row. read is None when the row has nothing filled in."""
    url = _clean(row.get("kunci"))
    if not url.startswith(("http://", "https://")):
        return None, "baris antrean tanpa kunci alamat (kolom kunci tidak boleh diubah)"
    if _clean(row.get("keputusan")).lower() in REJECT_WORDS:
        return {"manual": True, "reject": True}, None
    number, rarity, language, grade = (_clean(row.get(k)) for k in ("nomor", "rarity", "bahasa", "grade"))
    if not (number or rarity or language or grade):
        return None, None
    read = {"manual": True, "is_slab": None, "company": None, "cert": None, "name": None, "number": None, "rarity": None, "language": None, "grade": None}
    if number:
        m = NUMBER.fullmatch(number)
        if not m:
            return None, f"{url[-45:]}: nomor '{number}' harus lengkap, mis. 234/193"
        read["number"] = f"{int(m.group(1))}/{m.group(2)}"
    if rarity:
        if not re.fullmatch(r"[A-Za-z]{1,4}", rarity):
            return None, f"{url[-45:]}: rarity '{rarity}' tidak dikenal (mis. SAR, SR, AR, RR, MA)"
        read["rarity"] = rarity.upper()
    if language:
        if language.lower() not in LANGUAGES:
            return None, f"{url[-45:]}: bahasa '{language}' tidak dikenal (ID, EN, atau JP)"
        read["language"] = LANGUAGES[language.lower()]
    if grade:
        g = grade.replace(",", ".")
        if g not in GRADES:
            return None, f"{url[-45:]}: grade '{grade}' tidak dikenal (10, 9.5, 9, 8.5, 8, 7.5, 7)"
        read["grade"] = g
    return read, None


def _read_market_row(row):
    """([import rows], [problems]) for a pasar row: one row per site where a price was typed. The title is built from the card of the
    row and the language the owner stated, so that the usual identity check can accept it."""
    parts = _clean(row.get("kunci")).split("|")
    if len(parts) != 5:
        return [], ["baris pasar tanpa kunci yang utuh (kolom kunci tidak boleh diubah)"]
    name, set_code, number, rarity, grade = parts
    what = f"{name} {number} {set_code} {rarity} EGS {grade}".replace("  ", " ")
    sites = [(site, _clean(row.get(f"harga_{site}")), _clean(row.get(f"link_{site}"))) for site in ("tokopedia", "shopee")]
    if not any(price or link for _, price, link in sites):
        return [], []
    language = LANGUAGES.get(_clean(row.get("bahasa")).lower())
    if not language:
        return [], [f"{what}: isi kolom bahasa (ID, EN, atau JP) di baris yang ada harganya"]
    if language != "ID":
        return [], [f"{what}: bahasa {language}, bukan kartu Indonesia, tidak dihitung"]
    out, problems = [], []
    for site, price, link in sites:
        if not (price or link):
            continue
        if not price or not link:
            problems.append(f"{what} ({site}): harga dan link harus diisi dua-duanya")
            continue
        out.append({"url": link, "title": f"{what} ID", "price": price, "seller": None, "photo": None, "stock": "1", "city": None,
                    "description": f"Dicatat manual dari lembar kerja (dilihat langsung di {site}); bahasa kartu dinyatakan Indonesia"})
    return out, problems


def parse(text):
    """(reads, imports, problems) from the worksheet CSV text: reads = {listing url: dict like a photo reading}, imports = rows for egs_watch.normalize_import."""
    reads, imports, problems = {}, [], []
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or "kunci" not in {(k or "").strip().lower() for k in rows[0]}:
        return {}, [], ["lembar kerja tidak dikenali: baris pertama harus memuat nama kolom (jenis, ..., kunci)"]
    for i, raw in enumerate(rows, 2):
        row = {(k or "").strip().lower(): v for k, v in raw.items()}
        kind = _clean(row.get("jenis")).lower()
        if kind == "antrean":
            read, problem = _read_queue_row(row)
            if problem:
                problems.append(f"baris {i}: {problem}")
            elif read:
                reads[_clean(row["kunci"])] = read
        elif kind == "pasar":
            rec, probs = _read_market_row(row)
            imports.extend(rec)
            problems.extend(f"baris {i}: {p}" for p in probs)
        elif kind:
            problems.append(f"baris {i}: jenis '{kind}' tidak dikenal (antrean atau pasar)")
    return reads, imports, problems


def load(source, fetch=None):
    """Worksheet text from a file or an http(s) address (e.g. a Google Sheets 'publish to the web' CSV link). Raises OSError / ValueError."""
    if urlsplit(source).scheme in ("http", "https"):
        if fetch is None:
            raise ValueError("tidak ada fungsi pengambil untuk alamat web")
        data, _ = fetch(source)
        return data.decode("utf-8-sig", "replace")
    with open(source, encoding="utf-8-sig") as fh:
        return fh.read()


def read(source, fetch=None):
    """(reads, imports, problems) from a file or address; a source that cannot be read is a problem, not a crash."""
    try:
        return parse(load(source, fetch))
    except (OSError, ValueError) as e:
        return {}, [], [f"lembar kerja tidak terbaca: {e}"]
    except Exception as e:      # a fetch error of the caller's own kind
        return {}, [], [f"lembar kerja tidak terbaca: {type(e).__name__}: {e}"]
