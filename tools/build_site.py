#!/usr/bin/env python3
"""Build the "Papan Harga Kartu Pokemon" page (RAW and EGS slab asking prices) from latest.json and raw.json. Read-only on the data.

Usage: python3 tools/build_site.py [latest.json] [--raw raw.json] [--out FILE] [--standalone]

Every asking price is treated as the price (price_type OBSERVED_ASKING_ONLY);
nothing waits for a sale. For each tracked card type the page lists every known
listing per grade: the tracked listing plus matching discovery candidates.

Match confidence of a discovery candidate to a tracked card:
  nomor      card number (e.g. 223/193) appears in the title      -> counted
  nama+set   name and set code match, no conflicting rarity       -> counted
  nama       name only, or the rarity/set conflicts               -> shown, not counted
Foreign-language versions are shown but never counted. Non-Pokemon cards are hidden.
With --raw (see docs/raw-dataset.md) each card also shows the RAW (ungraded) asking range next to the
slabs, and RAW-only cards get their own table.
Without --standalone the output is a fragment for the Artifact tool (no <html> tag).
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from triage_candidates import FOREIGN, NON_POKEMON, clean_name, parse  # noqa: E402

WIB = timezone(timedelta(hours=7))
RARITY = re.compile(r"\b(SAR|SR|AR|UR|HR|CSR|SSR|RRR|RR)\b")
FRACTION = re.compile(r"\b(\d{1,3})\s*/\s*(\d{2,3})\b")
HASH_NO = re.compile(r"#\s?(\d{1,3})\b")
URL_TS = re.compile(r"-(\d{13})-[0-9a-f]{4}$")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def wib_label(iso):
    t = datetime.fromisoformat(iso).astimezone(WIB)
    return f"{t.day} {MONTHS[t.month - 1]} {t:%H:%M} WIB"


def listed_at(url):
    m = URL_TS.search(url)
    return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc) if m else None


def age_fields(url, gen):
    t = listed_at(url)
    if not t:
        return {"age_days": None, "listed": None}
    t = t.astimezone(WIB)
    return {"age_days": (gen - t).days, "listed": f"{t.day} {MONTHS[t.month - 1]}"}


def number_conflict(text, number):
    """True when the text shows a card number and it is not the tracked one (003/126 is not 173/126)."""
    want = tuple(int(x) for x in number.replace(" ", "").split("/"))
    fractions = [(int(a), int(b)) for a, b in FRACTION.findall(text)]
    if fractions:
        return want not in fractions
    hashes = [int(h) for h in HASH_NO.findall(text)]
    return bool(hashes) and want[0] not in hashes


def match_type(text, types):
    """Best (index, confidence) of a candidate title against the tracked card types."""
    low = text.lower()
    squashed = low.replace(" ", "")
    tokens = set(RARITY.findall(text.upper()))
    best = None
    for i, t in enumerate(types):
        words = clean_name(t["name"])
        name_ok = all(re.search(rf"\b{re.escape(w)}\b", low) for w in words)
        if t["number"].lower() in squashed:
            cand = (3, i, "nomor")
        elif name_ok:
            set_ok = re.search(rf"\b{re.escape(t['set'].lower())}\b", low)
            conflict = (bool(tokens) and t["rarity"].upper() not in tokens) or number_conflict(text, t["number"])
            cand = (2, i, "nama+set") if set_ok and not conflict else (1, i, "nama")
        else:
            continue
        if best is None or cand[0] > best[0]:
            best = cand
    return (best[1], best[2]) if best else None


def build(d, raw=None):
    gen = datetime.fromisoformat(d["generated_at"])
    types, index = [], {}
    for l in d["listings"]:
        key = (l["name"], l["set_code"], l["card_number"], l["rarity"])
        if key not in index:
            index[key] = len(types)
            types.append({
                "name": re.sub(r"[<>]", "", l["name"]), "set": l["set_code"], "number": l["card_number"],
                "rarity": l["rarity"],
                "search": "https://toco.id/search?q=" + quote_plus(re.sub(r"[<>]", "", l["name"]) + " egs"),
                "rows": [],
            })
        prices = [x["asking_idr"] for x in l["daily_latest"]]
        types[index[key]]["rows"].append({
            "grade": l["grade"], "ask": l["latest"]["asking_idr"], "list": None,
            "tracked": True, "conf": "dipantau", "lang": "ID", "city": "",
            "url": l["url"], "in_stock": l["latest"]["in_stock"],
            "days": len(prices), "flat": len(set(prices)) == 1,
            "seen": wib_label(l["latest"]["observed_at"]),
            "text": f"{l['name']} {l['card_number']} {l['set_code']} {l['rarity']}",
            **age_fields(l["url"], gen),
        })

    tracked_urls = {l["url"] for l in d["listings"]}
    others, hidden = [], 0
    for c in d["discovery"]["candidates"]:
        if c["url"] in tracked_urls:
            continue
        p = parse(c)
        if NON_POKEMON.search(p["text"]):
            hidden += 1
            continue
        row = {
            "grade": p["grade"], "ask": p["ask_idr"], "list": p["list_idr"], "tracked": False,
            "lang": "beda" if FOREIGN.search(p["text"]) else "ID", "city": p["city"],
            "url": p["url"], "in_stock": True, "text": re.sub(r"^Sisa \d+ ", "", p["text"]),
            **age_fields(p["url"], gen),
        }
        m = match_type(p["text"], types)
        if m:
            row["conf"] = m[1]
            types[m[0]]["rows"].append(row)
        else:
            row["conf"] = "lain"
            others.append(row)

    data = {
        "generated": wib_label(d["generated_at"]),
        "last_observed": wib_label(d["coverage"]["last_observed_at"]),
        "discovery": wib_label(d["discovery"]["generated_at"]),
        "types": types, "others": others, "hidden": hidden,
        "raw_meta": None, "raw_only": [], "raw_cards": [], "sets": [], "families": [],
    }
    if raw:
        attach_raw(data, raw, gen)
    return data


def card_key(set_code, number):
    return (set_code.lower(), number.replace(" ", "").lower())


def trend_of(history):
    """Change of the RAW median against the entry about 7 days earlier (or the oldest one we have)."""
    if len(history) < 2:
        return None
    last = history[-1]
    last_day = date.fromisoformat(last["date_wib"])
    older = [h for h in history[:-1] if date.fromisoformat(h["date_wib"]) <= last_day - timedelta(days=7)]
    base = older[-1] if older else history[0]
    days = (last_day - date.fromisoformat(base["date_wib"])).days
    if days <= 0 or not base["median"]:
        return None
    return {"days": days, "pct": round((last["median"] - base["median"]) / base["median"] * 100, 1)}


SETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sets.json")


def load_sets(path=SETS_PATH):
    """Set names from tools/sets.json (hand-kept, see its 'about'); a missing file just means no names."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"families": [], "sets": []}


def set_family(code, known):
    if code in known:
        return known[code]["family"]
    m = re.match(r"[A-Z]+", code)
    return m.group(0) if m and m.group(0) in ("MA", "SV", "AC") else "AC"


def set_sort_key(meta, family_order):
    num = re.search(r"\d+", meta["code"])
    return (family_order.get(meta["family"], 99), -(int(num.group(0)) if num else 0), meta["code"])


def egs_lowest(t):
    """Lowest counted EGS ask per grade for one tracked type."""
    best = {}
    for r in t["rows"]:
        if r["in_stock"] and r["lang"] == "ID" and (r["tracked"] or r["conf"] in ("nomor", "nama+set")):
            n, low = best.get(r["grade"], (0, None))
            best[r["grade"]] = (n + 1, r["ask"] if low is None else min(low, r["ask"]))
    return [{"grade": g, "ask": low, "n": n} for g, (n, low) in sorted(best.items(), key=lambda kv: -float(kv[0]))]


def attach_raw(data, raw, gen):
    """Join raw.json cards to the tracked types on (set_code, card_number)."""
    by = {card_key(c["set_code"], c["card_number"]): c for c in raw["cards"]}
    used = set()
    for t in data["types"]:
        k = card_key(t["set"], t["number"])
        c = by.get(k)
        if not c:
            continue
        used.add(k)
        history = c.get("history") or []
        t["raw"] = {**c["stats"], "history": history, "trend": trend_of(history), "listings": [
            {"ask": l["ask_idr"], "city": l["city"], "url": l["url"], "conf": l["conf"],
             "text": l["title"], **age_fields(l["url"], gen)} for l in c["listings"]]}
    data["raw_only"] = sorted(
        ({"name": c["name"], "set": c["set_code"], "number": c["card_number"],
          "rarity": c["rarity"], **c["stats"], "trend": trend_of(c.get("history") or []),
          "url": c["listings"][0]["url"] if c["listings"] else None,
          "photo": next((l["photo"] for l in c["listings"] if l.get("photo")), None)}
         for k, c in by.items() if k not in used),
        key=lambda c: (-c["n"], c["set"], c["number"]))
    data["raw_meta"] = {"generated": wib_label(raw["generated_at"]), "cards": len(raw["cards"]),
                        "listings": raw["coverage"]["listings_counted"]}
    attach_sets(data, raw)


def attach_sets(data, raw):
    """Every RAW card as a row, grouped by set code, with the set's name and how much of it we have priced."""
    meta = load_sets()
    known = {m["code"].upper(): m for m in meta["sets"]}
    family_order = {f["id"]: i for i, f in enumerate(meta["families"])}
    tracked = {card_key(t["set"], t["number"]): t for t in data["types"]}
    rows, per_set = [], {}
    for c in raw["cards"]:
        code = c["set_code"].upper()
        key = card_key(c["set_code"], c["card_number"])
        num, den = (int(x) for x in c["card_number"].replace(" ", "").split("/"))
        history = c.get("history") or []
        photo = next((l["photo"] for l in c["listings"] if l.get("photo")), None)
        t = tracked.get(key)
        rows.append({"name": c["name"], "set": code, "number": c["card_number"], "num": num, "den": den,
                     "rarity": c["rarity"], **c["stats"], "trend": trend_of(history),
                     "url": c["listings"][0]["url"] if c["listings"] else None, "photo": photo,
                     "tracked": t is not None, "egs": egs_lowest(t) if t else []})
        per_set.setdefault(code, []).append(rows[-1])
    sets = []
    for code, cards in per_set.items():
        m = known.get(code, {"code": code, "family": set_family(code, known), "name_id": None, "name_en": None,
                             "released": None, "base": None, "total": None, "basis": None, "note": ""})
        base = m.get("base") or Counter(c["den"] for c in cards).most_common(1)[0][0]
        for c in cards:  # the set's own numbering first, then side series such as the n/30 Pikachu of MA6
            c["sub"] = 0 if c["den"] == base else 1
        sets.append({**m, "code": code, "base": base, "cards": len(cards), "listings": sum(c["n"] for c in cards),
                     "released_label": (lambda d: f"{d.day} {MONTHS[d.month - 1]} {d.year}")(date.fromisoformat(m["released"]))
                                       if m.get("released") else None})
    sets.sort(key=lambda m: set_sort_key(m, family_order))
    order = {m["code"]: i for i, m in enumerate(sets)}
    rows.sort(key=lambda c: (order[c["set"]], c["sub"], c["den"], c["num"]))
    data["raw_cards"], data["sets"], data["families"] = rows, sets, meta["families"]


def render(data, standalone):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "site_template.html"), encoding="utf-8") as fh:
        page = fh.read()
    # card photos are loaded from the seller's image host: fine in a normal page, blocked in an Artifact
    blob = json.dumps({**{k: v for k, v in data.items() if k != "raw_only"}, "photos": standalone},
                      ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("/*__DATA__*/null", blob)
    if standalone:
        # private page: keep search engines out, and send no Referer to the photo host (the sellers' images)
        page = ("<!doctype html><html lang=\"id\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
                "<meta name=\"robots\" content=\"noindex,nofollow\"><meta name=\"referrer\" content=\"no-referrer\">"
                "<title>Papan Harga Kartu Pok\u00e9mon</title>"
                "</head><body>" + page + "</body></html>")
    return page


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", default="latest.json")
    ap.add_argument("--raw", help="raw.json with the RAW (ungraded) dataset")
    ap.add_argument("--out", default="-", help="output file (default: stdout)")
    ap.add_argument("--standalone", action="store_true", help="wrap in a full HTML document")
    a = ap.parse_args()
    with open(a.path, encoding="utf-8") as fh:
        latest = json.load(fh)
    raw = None
    if a.raw:
        with open(a.raw, encoding="utf-8") as fh:
            raw = json.load(fh)
    html = render(build(latest, raw), a.standalone)
    if a.out == "-":
        sys.stdout.write(html)
    else:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
