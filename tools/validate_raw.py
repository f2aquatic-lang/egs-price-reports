#!/usr/bin/env python3
"""Validate raw.json (RAW dataset, schema_version 1). Read-only; exit 1 on errors.

Usage: python3 tools/validate_raw.py [path/to/raw.json]
Format: see docs/raw-dataset.md.
"""
import json
import sys
from datetime import datetime

TOP = ("schema_version", "kind", "price_type", "language", "source", "generated_at",
       "coverage", "cards", "unmatched")
CARD = ("name", "set_code", "card_number", "rarity", "stats", "listings")
STATS = ("n", "min", "p25", "median", "p75", "max")
LISTING = ("url", "ask_idr", "city", "observed_at", "conf", "lang", "lang_by", "title")


VARIANTS = ("poke_ball", "master_ball", "element")


def key(c):
    return (c["set_code"].lower(), c["card_number"].replace(" ", "").lower(), c.get("variant", ""))


def validate(d):
    errors, warnings = [], []
    err, warn = errors.append, warnings.append
    missing = [k for k in TOP if k not in d]
    if missing:
        return ["missing top-level keys: " + ", ".join(missing)], warnings
    if d["schema_version"] != 1:
        err(f"schema_version is {d['schema_version']!r}, expected 1")
    if d["kind"] != "RAW":
        err(f"kind is {d['kind']!r}, expected RAW")
    if d["price_type"] != "OBSERVED_ASKING_ONLY":
        err(f"price_type is {d['price_type']!r}, expected OBSERVED_ASKING_ONLY")
    if d["language"] != "ID":
        err(f"language is {d['language']!r}, expected ID")
    gen = datetime.fromisoformat(d["generated_at"])

    seen, urls, counted, newest, defaults = set(), set(), 0, None, 0
    for c in d["cards"]:
        gone = [k for k in CARD if k not in c]
        if gone:
            err(f"card {c.get('name', '?')}: missing keys {', '.join(gone)}")
            continue
        label = f"{c['name']} {c['set_code']} {c['card_number']}" + (f" ({c['variant']})" if c.get("variant") else "")
        if c.get("variant") and (c["variant"] not in VARIANTS and not c["variant"].startswith("v") or not c.get("catalog")):
            err(f"{label}: variant {c['variant']!r} must be one of {', '.join(VARIANTS)} (or vN) and only on catalog cards")
        if key(c) in seen:
            err(f"{label}: duplicate card (set_code + card_number + variant)")
        seen.add(key(c))
        s = c["stats"]
        if any(k not in s for k in STATS):
            err(f"{label}: stats needs {', '.join(STATS)}")
            continue
        counted += s["n"]
        basis = c.get("price_basis")
        if basis not in (None, "default"):
            err(f"{label}: price_basis {basis!r} must be absent or 'default'")
        if basis == "default":
            defaults += 1
            if not c.get("catalog") or s["n"] != 0 or c["listings"] or c.get("history"):
                err(f"{label}: a default-priced card must be a catalog card with n=0, no listings and no history")
            if len({s["min"], s["p25"], s["median"], s["p75"], s["max"]}) != 1:
                err(f"{label}: a default-priced card has one price for min, p25, median, p75 and max")
        order = [s["min"], s["p25"], s["median"], s["p75"], s["max"]]
        if order != sorted(order):
            err(f"{label}: stats must satisfy min <= p25 <= median <= p75 <= max")
        if s["n"] < len(c["listings"]):
            err(f"{label}: stats.n={s['n']} is smaller than the {len(c['listings'])} listings given")
        if s["min"] <= 0:
            err(f"{label}: stats.min must be positive")
        for l in c["listings"]:
            gone = [k for k in LISTING if k not in l]
            if gone:
                err(f"{label}: listing missing keys {', '.join(gone)}")
                continue
            if l["url"] in urls:
                err(f"{label}: duplicate listing url {l['url'][-30:]}")
            urls.add(l["url"])
            if not (s["min"] <= l["ask_idr"] <= s["max"]):
                err(f"{label}: listing ask {l['ask_idr']} is outside stats min..max")
            if l["lang"] != "ID":
                err(f"{label}: listing lang is {l['lang']!r}; only Indonesian listings may be published")
            if l["lang_by"] not in ("image", "title"):
                err(f"{label}: listing lang_by {l['lang_by']!r} must be image or title")
            if "photo" in l and not str(l["photo"]).startswith(("http://", "https://")):
                err(f"{label}: photo must be an http(s) address")
            allowed = ("katalog",) if c.get("catalog") else ("nomor", "nama+set")
            if l["conf"] not in allowed:
                err(f"{label}: listing conf {l['conf']!r} must be {' or '.join(allowed)} "
                    f"({'a catalog card' if c.get('catalog') else 'a tracked card'}; only counted listings are published)")
            obs = datetime.fromisoformat(l["observed_at"])
            if obs > gen:
                err(f"{label}: observed_at is after generated_at")
            newest = obs if newest is None or obs > newest else newest
        hist = c.get("history")
        if hist is not None:
            dates = [h.get("date_wib") for h in hist]
            if dates != sorted(set(dates)):
                err(f"{label}: history needs unique dates, oldest first")
            for h in hist:
                if not all(k in h for k in ("date_wib", "n", "median", "min")):
                    err(f"{label}: every history entry needs date_wib, n, median, min")
                    break
                if h["n"] < 1 or not (0 < h["min"] <= h["median"]):
                    err(f"{label}: history {h['date_wib']} must satisfy n >= 1 and 0 < min <= median")
        if s["n"] < 5 and not c.get("catalog") and basis != "default":
            warn(f"{label}: only {s['n']} counted listing(s)")

    cov = d["coverage"]
    if cov.get("cards") != len(d["cards"]):
        err(f"coverage.cards={cov.get('cards')} but {len(d['cards'])} cards")
    catalog_cards = sum(1 for c in d["cards"] if c.get("catalog"))
    if cov.get("catalog_cards", catalog_cards) != catalog_cards:
        err(f"coverage.catalog_cards={cov.get('catalog_cards')} but {catalog_cards} cards have catalog=true")
    if cov.get("default_priced_cards", defaults) != defaults:
        err(f"coverage.default_priced_cards={cov.get('default_priced_cards')} but {defaults} cards have price_basis=default")
    if cov.get("listings_counted") != counted:
        err(f"coverage.listings_counted={cov.get('listings_counted')} but stats.n sums to {counted}")
    if newest and cov.get("last_observed_at") != newest.isoformat():
        warn("coverage.last_observed_at is not the newest listing observed_at (listings are a capped sample)")
    return errors, warnings


def main(argv):
    path = argv[1] if len(argv) > 1 else "raw.json"
    with open(path, encoding="utf-8") as fh:
        errors, warnings = validate(json.load(fh))
    for kind, items in (("ERROR", errors), ("WARN", warnings)):
        for line in items:
            print(f"{kind}: {line}")
    print(f"{path}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
