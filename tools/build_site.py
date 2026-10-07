#!/usr/bin/env python3
"""Build the "Papan Harga Kartu Pokemon" page (RAW and EGS slab asking prices) from latest.json and raw.json. Read-only on the data.

Usage: python3 tools/build_site.py [latest.json] [--raw raw.json] [--catalog catalog.json] [--jp jp_prices.json] [--rates rates.json] [--egs egs_watch.json] [--marks card_marks.json] [--tcgdex tcgdex.json] [--out FILE] [--standalone]

Every asking price is treated as the price (price_type OBSERVED_ASKING_ONLY);
nothing waits for a sale. For each tracked card type the page lists every known
listing per grade: the tracked listing plus matching discovery candidates.

Match confidence of a discovery candidate to a tracked card:
  nomor      card number (e.g. 223/193) appears in the title      -> counted
  nama+set   name and set code match, no conflicting rarity       -> counted
  nama       name only, or the rarity/set conflicts               -> shown, not counted
Foreign-language versions are shown but never counted. Non-Pokemon cards are hidden.
With --raw (see docs/raw-dataset.md) each card also shows the RAW (ungraded) asking range next to the
slabs, and RAW-only cards get their own table. With --catalog every official card of each set is a row; with --jp the Japanese
shop price (tools/fetch_jp_prices.py) is shown beside the Indonesian prices for the sets that 'jp_sets' in tools/sets.json maps; with --tcgdex
(and --catalog) the cards of the other sets are paired with their Japanese and English editions (tools/build_bridge.py), which adds the Japanese
price of those cards and the English (Cardmarket) reference column.
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
from fetch_jp_prices import split_name  # noqa: E402
import build_bridge  # noqa: E402

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


def build(d, raw=None, catalog=None, jp=None, rates=None, watch=None, marks=None, tcgdex=None):
    d = merge_egs_watch(d, watch)
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
        "raw_meta": None, "raw_only": [], "raw_cards": [], "sets": [], "families": [], "en_meta": None,
        "rates": rates_block(rates), "egs_watch": watch_block(watch),
    }
    if raw:
        attach_raw(data, raw, gen, catalog, jp, marks, tcgdex)
    return data


VARIANTS = ("", "poke_ball", "master_ball")  # default order of a number's entries on the official site: regular, then the foil versions


def card_key(set_code, number, variant=""):
    """Join key of a card: set code, number and foil version, case and zero padding ignored (014/078 is 14/78)."""
    n = number.replace(" ", "").lower()
    m = re.fullmatch(r"(\d+)/(\d+)", n)
    return (set_code.lower(), f"{int(m.group(1))}/{int(m.group(2))}" if m else n, variant or "")


def deck_sets(sets):
    """Codes of official sets that are decks (starter and theme decks): not part of the card-price board."""
    return {x["code"].upper() for x in sets or [] if re.search(r"deck", x.get("product") or "", re.I)}


def variant_orders():
    """{set code: [variant of the 1st entry, of the 2nd, ...]} from the 'variants' lists in tools/sets.json."""
    return {m["code"].upper(): m["variants"] for m in load_sets()["sets"] if m.get("variants")}


BALL_VARIANTS = ("poke_ball", "master_ball", "element")  # the foil versions a Japanese listing can add to an Indonesian card


def merge_egs_watch(d, watch):
    """latest.json plus the listings the EGS watch (tools/egs_watch.py) accepted and counts, as tracked listings. In memory only:
    latest.json is never written. Listings that are already in it, duplicates, outliers and unavailable ones are not added."""
    if not watch:
        return d
    have = {l["url"] for l in d["listings"]}
    extra = []
    for l in watch["listings"]:
        i = l["identity"]
        if l["url"] in have or not l["counted"] or not l["latest"]:
            continue
        extra.append({"name": i["name"], "set_code": i["set_code"], "card_number": i["card_number"], "rarity": i["rarity"] or "", "grade": i["grade"],
                      "url": l["url"], "latest": l["latest"], "previous_day": l["previous_day"],
                      "change_from_previous_day_idr": l["change_from_previous_day_idr"], "daily_latest": l["daily_latest"]})
    return {**d, "listings": d["listings"] + extra}


def watch_block(watch, limit=60):
    """What the page shows of the EGS watch: the counters and the review queue with photo, link and the reasons for the doubt."""
    if not watch:
        return None
    s, r = watch["summary"], watch["run"]
    return {"generated": wib_label(watch["generated_at"]), "source_ok": r.get("source_ok", True), "blocked": r.get("blocked"),
            "requests": r.get("requests", 0), "failed_requests": r.get("failed_requests", 0),
            "summary": {k: s[k] for k in ("candidates_found", "accepted_total", "accepted_existing", "accepted_auto", "accepted_new_this_run", "ambiguous",
                                          "rejected_this_run", "listings_counted", "cards", "sellers_unique", "checked_ok_this_run",
                                          "check_failed_this_run", "price_changes_24h", "status", "freshness")},
            "queue": [{"url": q["url"], "photo": q["photo"], "title": q["title"], "ask": q["asking_idr"], "seller": q["seller"], "city": q["city"],
                       "reasons": [x["text"] for x in q["reasons"]],
                       "suggestions": [f"{x['name']} {x['card_number']} {x['set_code']}" + (f" {x['rarity']}" if x.get("rarity") else "") for x in q["suggestions"]]}
                      for q in watch["queue"][:limit]],
            "queue_total": len(watch["queue"])}


def rates_block(rates):
    """{currency: {idr, source, label}} from rates.json (tools/fetch_rates.py): the exchange rates shown on the page and used for rupiah conversions."""
    if not rates:
        return None
    return {cur: {"idr": r["idr"], "source": r["source"], "fetched_at": r["fetched_at"], "label": wib_label(r["fetched_at"])}
            for cur, r in rates["rates"].items()}


def jp_variant(card):
    """Foil version of a Yuyu-tei card, read from the bracket in its name ('' = the regular card, 'rocket' = Team Rocket mark mirror)."""
    return split_name(f"x({card['tag']})")[2] if card.get("tag") else ""


def jp_sets():
    """{Indonesian set code: Yuyu-tei set code} from 'jp_sets' in tools/sets.json: the sets whose cards have the same numbers in both."""
    return {k.upper(): v for k, v in load_sets().get("jp_sets", {}).items()}


def jp_index(jp):
    """{card_key(yuyu-tei set, number, variant): card} of jp_prices.json; the first entry wins when a version is listed twice."""
    out = {}
    for c in (jp or {}).get("cards", []):
        out.setdefault(card_key(c["set"], c["number"], jp_variant(c)), c)
    return out


def jp_versions(jp, mapping=None):
    """{(Indonesian set code, number key): {versions}}: the foil versions Yuyu-tei lists for a number of a mapped set. Where the
    official Indonesian site lists none of them (SV2A, SV8A's Master Ball) they are the proof such a card exists."""
    back = {v: k for k, v in (jp_sets() if mapping is None else mapping).items()}
    out = {}
    for c in (jp or {}).get("cards", []):
        code, variant = back.get(c["set"]), jp_variant(c)
        if code and variant in BALL_VARIANTS:
            out.setdefault((code, card_key(c["set"], c["number"])[1]), set()).add(variant)
    return out


def en_price(card):
    """{'eur', 'name', 'id'} of an English card of tcgdex.json: the Cardmarket price trend, else the 30-day average, else the lowest ask; None without a price."""
    cm = card.get("cardmarket") or {}
    eur = next((cm[k] for k in ("trend", "avg30", "avg", "low") if cm.get(k)), None)
    if not eur or (cm.get("unit") or "EUR") != "EUR":
        return None
    return {"eur": round(eur, 2), "name": " ".join(x for x in (card["name"], card.get("rarity")) if x), "id": card["id"]}


def bridge_refs(catalog, tcgdex, jp):
    """{(set code lower, number key): {'jp': Yuyu-tei card or None, 'via': its set code, 'en': en_price or None}} for the Indonesian cards that
    tools/build_bridge.py pairs with an English and a Japanese card: the Japanese reference where the Indonesian and the Japanese number differ,
    and the English one (Cardmarket) everywhere. Only the regular version of a card is paired; a foil version is a card of its own."""
    if not (catalog and tcgdex):
        return {}
    bridge = build_bridge.build(catalog, tcgdex)
    by_card = {(c["lang"], c["id"]): c for c in tcgdex["cards"]}
    shop = {}
    for c in (jp or {}).get("cards", []):
        m = re.match(r"\d+", c["number"])
        if m and not jp_variant(c):
            shop.setdefault((c["set"], int(m.group(0))), c)
    out = {}
    for key, entry in bridge["cards"].items():
        code, number = key.split("|", 1)
        ja = by_card.get(("ja", entry.get("ja")))
        jk = build_bridge.jp_key(ja) if ja else None
        yuyu = shop.get(jk) if jk else None
        en = en_price(by_card[("en", entry["en"])])
        if yuyu or en:
            out[card_key(code, number)[:2]] = {"jp": yuyu, "via": jk[0] if yuyu else None, "en": en}
    return out


def official_index(cards, sets=None, orders=None, extras=None, decks=False, marks=None):
    """{card_key: card} of the official catalog, one entry per card AND foil version.

    A number can appear two or three times on the site (regular, Poke Ball holo, Master Ball holo): they are
    different cards with different prices. The entries of a number come in that order, so the first is the
    regular card and the next ones take the variants "poke_ball" and "master_ball" unless tools/sets.json says
    otherwise for that set (MA3: regular, poke_ball, element). They share the rarity of the first entry that has one.
    Versions the site does not list at all (SV2A's Poke Ball and Master Ball commons, SV8A's Master Ball) are added
    from `extras` (jp_versions: what Yuyu-tei lists for that number); they have no official image and stay unpriced until a seller lists them.
    Deck sets are left out unless decks=True (the board lists them as unpriced rows; the collector gives no base price to a deck card)."""
    skip, groups = (set() if decks else deck_sets(sets)), {}
    marks = load_marks()["cards"] if marks is None else marks
    orders = variant_orders() if orders is None else orders
    extras = extras or {}
    for c in cards:
        if not c.get("number") or c["set"].upper() in skip:
            continue
        groups.setdefault(card_key(c["set"], c["number"]), []).append(c)
    out = {}
    for (code, number, _), entries in groups.items():
        mark = marks.get(f"{entries[0]['set']}|{entries[0]['number']}") or {}
        rarity = next((e["rarity"] for e in entries if e.get("rarity")), None) or mark_label(mark)
        reprint = mark.get("reprint") or None
        order = orders.get(code.upper(), VARIANTS)
        for i, e in enumerate(entries):
            variant = order[i] if i < len(order) else f"v{i + 1}"
            out[card_key(e["set"], e["number"], variant)] = {**e, "rarity": rarity, "variant": variant, "reprint": reprint}
        for variant in BALL_VARIANTS:
            key = card_key(entries[0]["set"], entries[0]["number"], variant)
            if variant in extras.get((code.upper(), number), ()) and key not in out:
                out[key] = {**entries[0], "rarity": rarity, "variant": variant, "image": None, "id": None, "extra": True}
    return out


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
MARKS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "card_marks.json")   # the repository root, like catalog.json


def load_marks(path=MARKS_PATH):
    """card_marks.json: the rarity printed on cards that the official site lists without one, and the reprint flag (see tools/read_card_marks.py). The VPS keeps it
    in /opt/egs-raw (outside the checkout, which the publisher resets) and publishes a copy next to catalog.json."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"schema_version": 1, "about": "", "cards": {}}
    data.setdefault("cards", {})
    return data


def mark_label(entry):
    """What the board shows for a mark: the printed rarity, with ' AA' for an alternate-art picture ('SR AA')."""
    if not entry or not entry.get("rarity"):
        return None
    return entry["rarity"] + (" AA" if entry.get("alt_art") else "")


def load_sets(path=SETS_PATH):
    """Set names from tools/sets.json (hand-kept, see its 'about'); a missing file just means no names."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"families": [], "sets": []}


SERIES_FAMILY = {"evolusi mega": "MA", "scarlet & violet": "SV", "pedang & perisai": "SS", "matahari & bulan": "SM"}   # the series names the official site uses


def set_family(code, known, series=None):
    """The family (series) of a set: from tools/sets.json, else from the series name of the official site, else from the code prefix, else 'Lainnya'."""
    if code in known:
        return known[code]["family"]
    if series and series.strip().lower() in SERIES_FAMILY:
        return SERIES_FAMILY[series.strip().lower()]
    for family in ("MA", "SV", "AC"):   # MA6, MAT, MATL ... are all MEGA series; anything else is "Lainnya" until it is named
        if code.startswith(family):
            return family
    return "AC"


def set_sort_key(meta, family_order):
    """Family order, then newest release first (sets without a date after the dated ones, by the number in their code)."""
    num = re.search(r"\d+", meta["code"])
    released = meta.get("released")
    when = -date.fromisoformat(released).toordinal() if released else None
    return (family_order.get(meta["family"], 99), when is None, when or 0, -(int(num.group(0)) if num else 0), meta["code"])


def egs_lowest(t):
    """Lowest counted EGS ask per grade for one tracked type."""
    best = {}
    for r in t["rows"]:
        if r["in_stock"] and r["lang"] == "ID" and (r["tracked"] or r["conf"] in ("nomor", "nama+set")):
            n, low = best.get(r["grade"], (0, None))
            best[r["grade"]] = (n + 1, r["ask"] if low is None else min(low, r["ask"]))
    return [{"grade": g, "ask": low, "n": n} for g, (n, low) in sorted(best.items(), key=lambda kv: -float(kv[0]))]


def attach_raw(data, raw, gen, catalog=None, jp=None, marks=None, tcgdex=None):
    """Join raw.json cards to the tracked types on (set_code, card_number)."""
    by = {card_key(c["set_code"], c["card_number"], c.get("variant", "")): c for c in raw["cards"]}
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
    attach_sets(data, raw, catalog, jp, marks, tcgdex)


def jp_price(row, mapping, jpx, jp, refs=None):
    """The Yuyu-tei card with the same set, number and foil version as a board row; where the set is not mapped (the numbers differ) the card the
    bridge paired with it ('via' says which Yuyu-tei set). None where neither finds the card."""
    jpcode = mapping.get(row["set"])
    c = jpx.get(card_key(jpcode, row["number"], row["variant"])) if jpcode else None
    via = None
    if not c and not jpcode and not row["variant"] and refs:
        ref = refs.get(card_key(row["set"], row["number"])[:2])
        c, via = (ref["jp"], ref["via"]) if ref else (None, None)
    if not c:
        return None
    return {"yen": c["yen"], "in_stock": c.get("in_stock", True), "stock": c.get("stock"), "rarity": c.get("rarity"), "name": c.get("name"),
            "url": jp["card_url"].format(**c) if jp.get("card_url") else None, **({"via": via} if via else {})}


def en_ref(row, refs):
    """The English (Cardmarket) reference of a board row, or None; the regular version of a card only."""
    ref = refs.get(card_key(row["set"], row["number"])[:2]) if refs and not row["variant"] else None
    return ref["en"] if ref else None


def attach_sets(data, raw, catalog=None, jp=None, marks=None, tcgdex=None):
    """Every card as a row, grouped by set code: priced from listings, base-priced, or (with the official catalog) unpriced."""
    meta = load_sets()
    known = {m["code"].upper(): m for m in meta["sets"]}
    family_order = {f["id"]: i for i, f in enumerate(meta["families"])}
    tracked = {card_key(t["set"], t["number"]): t for t in data["types"]}
    mapping, jpx = jp_sets() if jp else {}, jp_index(jp)
    refs = bridge_refs(catalog, tcgdex, jp)
    official = {}
    official_sets = {}
    if catalog:
        official = official_index(catalog["cards"], catalog["sets"], extras=jp_versions(jp, mapping), decks=True, marks=marks)
        official_sets = {x["code"].upper(): x for x in catalog["sets"] if x.get("fetched")}
    rows, per_set, seen = [], {}, set()

    def add(row):
        row["jp"] = jp_price(row, mapping, jpx, jp, refs)
        row["en"] = en_ref(row, refs)
        rows.append(row)
        per_set.setdefault(row["set"], []).append(row)

    for c in raw["cards"]:
        code = c["set_code"].upper()
        variant = c.get("variant", "")
        key = card_key(c["set_code"], c["card_number"], variant)
        seen.add(key)
        num, den = (int(x) for x in c["card_number"].replace(" ", "").split("/"))
        history = c.get("history") or []
        photo = next((l["photo"] for l in c["listings"] if l.get("photo")), None)
        t, o = (tracked.get(key) if not variant else None), official.get(key) or official.get(card_key(c["set_code"], c["card_number"]))
        base = c.get("price_basis") == "default"
        add({"name": (o or c)["name"], "set": code, "number": (o or c)["number"] if o else c["card_number"], "num": num, "den": den, "variant": variant,
             "rarity": (o or {}).get("rarity") or c["rarity"], **c["stats"], "trend": trend_of(history),
             "url": c["listings"][0]["url"] if c["listings"] else None, "photo": photo,
             "image": official[key].get("image") if key in official else None,  # an unofficial version shows the seller's photo
             "basis": "default" if base else "listing", "tracked": t is not None, "egs": egs_lowest(t) if t else [],
             "outliers": len(c.get("outliers") or []), "reprint": (o or {}).get("reprint")})
    for key, o in official.items():  # official cards nobody lists and that have no base price
        if key not in seen and o["set"].upper() in official_sets and o.get("num") is not None:
            add({"name": o["name"], "set": o["set"].upper(), "number": o["number"], "num": o["num"], "den": o["den"], "variant": o["variant"],
                 "rarity": o.get("rarity"), "n": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None,
                 "trend": None, "url": None, "photo": None, "image": o.get("image"), "basis": None, "tracked": False, "egs": [], "outliers": 0, "reprint": o.get("reprint")})
    sets = []
    for code, cards in per_set.items():
        o = official_sets.get(code, {})
        m = known.get(code, {"code": code, "family": set_family(code, known, o.get("series")), "name_id": None, "name_en": None,
                             "released": None, "base": None, "total": None, "basis": None, "note": ""})
        base = m.get("base") or Counter(c["den"] for c in cards).most_common(1)[0][0]
        for c in cards:  # the set's own numbering first, then side series such as the n/30 Pikachu of MA6
            c["sub"] = 0 if c["den"] == base else 1
        released = o.get("released") or m.get("released")
        worth = [c for c in cards if c["basis"] and c.get("median")]     # the card price for ordering: the median of its listings, or its base price
        solid = [c for c in worth if c["basis"] == "listing" and c["n"] >= 2]  # one seller's ask is not yet a price: it orders the set only when no card has two or more
        top = max(solid or worth, key=lambda c: (c["median"], -c["num"]), default=None)
        sets.append({**m, "code": code, "base": base, "cards": len(cards),
                     "top_price": top["median"] if top else None, "top_n": top["n"] if top and top["basis"] == "listing" else None,
                     "top_card": " ".join(x for x in (top["name"], top["number"], top["rarity"]) if x) if top else None,
                     "name_id": o.get("name") or m.get("name_id"),
                     "total": (o.get("total") or m.get("total")) if o else m.get("total"),
                     "basis": "resmi" if o.get("name") else m.get("basis"), "official": bool(o),
                     "priced": sum(1 for c in cards if c["basis"] == "listing"),
                     "base_priced": sum(1 for c in cards if c["basis"] == "default"),
                     "unpriced": sum(1 for c in cards if c["basis"] is None),
                     "listings": sum(c["n"] for c in cards),
                     "jp_set": mapping.get(code) or ", ".join(sorted({c["jp"]["via"] for c in cards if c["jp"] and c["jp"].get("via")})) or None,
                     "jp_via_bridge": not mapping.get(code), "jp_cards": sum(1 for c in cards if c["jp"]), "en_cards": sum(1 for c in cards if c["en"]),
                     "released": released,
                     "released_label": (lambda d: f"{d.day} {MONTHS[d.month - 1]} {d.year}")(date.fromisoformat(released))
                                       if released else None})
    sets.sort(key=lambda m: set_sort_key(m, family_order))
    order = {m["code"]: i for i, m in enumerate(sets)}
    vorder = {v: i for i, v in enumerate(("", "poke_ball", "master_ball", "element", "holo"))}
    rows.sort(key=lambda c: (order[c["set"]], c["sub"], c["den"], c["num"], vorder.get(c["variant"], 9)))
    data["raw_cards"], data["sets"], data["families"] = rows, sets, meta["families"]
    data["jp_meta"] = {"generated": wib_label(jp["generated_at"]), "cards": sum(1 for c in rows if c["jp"])} if jp else None
    data["en_meta"] = {"generated": wib_label(tcgdex["generated_at"]), "cards": sum(1 for c in rows if c["en"])} if tcgdex and refs else None


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
    ap.add_argument("--catalog", help="catalog.json from tools/fetch_catalog.py: every official card of each set, with its image")
    ap.add_argument("--jp", help="jp_prices.json from tools/fetch_jp_prices.py: Japanese shop prices beside the Indonesian ones")
    ap.add_argument("--egs", help="egs_watch.json from tools/egs_watch.py: more slab listings, with the review queue of the doubtful ones")
    ap.add_argument("--rates", help="rates.json from tools/fetch_rates.py: today's yen and dollar rates in rupiah")
    ap.add_argument("--marks", help="card_marks.json from tools/read_card_marks.py: the rarity printed on cards the official site lists without one, and the reprint flag")
    ap.add_argument("--tcgdex", help="tcgdex.json from tools/fetch_tcgdex.py: with --catalog it gives the English (Cardmarket) reference and the Japanese reference of sets whose numbers differ")
    ap.add_argument("--out", default="-", help="output file (default: stdout)")
    ap.add_argument("--standalone", action="store_true", help="wrap in a full HTML document")
    a = ap.parse_args()
    with open(a.path, encoding="utf-8") as fh:
        latest = json.load(fh)
    raw = None
    if a.raw:
        with open(a.raw, encoding="utf-8") as fh:
            raw = json.load(fh)
    catalog = None
    if a.catalog:
        with open(a.catalog, encoding="utf-8") as fh:
            catalog = json.load(fh)
    jp = None
    if a.jp:
        with open(a.jp, encoding="utf-8") as fh:
            jp = json.load(fh)
    rates = None
    if a.rates:
        with open(a.rates, encoding="utf-8") as fh:
            rates = json.load(fh)
    watch = None
    if a.egs:
        with open(a.egs, encoding="utf-8") as fh:
            watch = json.load(fh)
    marks = load_marks(a.marks)["cards"] if a.marks else None
    tcgdex = None
    if a.tcgdex:
        with open(a.tcgdex, encoding="utf-8") as fh:
            tcgdex = json.load(fh)
    html = render(build(latest, raw, catalog, jp, rates, watch, marks, tcgdex), a.standalone)
    if a.out == "-":
        sys.stdout.write(html)
    else:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
