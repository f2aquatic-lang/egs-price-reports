#!/usr/bin/env python3
"""Sixth probe: the fields of a toco listing and how per-card search queries behave. Plain HTTP only
(no browser, no key, no files written): about 10 POST requests to toco's own search endpoint, 1 second apart.

Run: python3 probe_toco_fields.py
"""
import json
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://toco.id"
API = "/api/toco-api/search"
UA = "Mozilla/5.0 (compatible; egs-price-reports/1.0)"
AGG = {"department_agg": {"terms": {"field": "department"}}, "category_agg": {"terms": {"field": "category"}},
       "type_agg": {"terms": {"field": "type"}}, "price_agg": {"terms": {"field": "price"}},
       "production_year_agg": {"terms": {"field": "production_year"}},
       "store_tier_agg": {"terms": {"field": "store_tier"}}, "official_seller_agg": {"terms": {"field": "official_seller"}}}
QUERIES = ["Mega Venusaur ex 173/126", "Pikachu ex 234/193", "pikachu ex sar ma3", "Mega Charizard X ex 223/193",
           "Zekrom 210/193", "Mega Dragonite ex 232/193"]
SHOW = ("name", "title", "slug", "condition", "stock", "quantity", "price", "description", "city", "created_at")


def search(query, size=45, offset=0, base=BASE):
    body = {"path": "/listing-search", "body": {
        "from": offset, "size": size, "search_operator": "and", "include_quality_score": True,
        "sort_mode": "relevance_only", "query": query,
        "filters": {"web": True, "add_to_cart": True, "special_daytop": False, "safe_search": True},
        "aggregations": AGG}}
    req = Request(base + API, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA,
        "Origin": base, "Referer": f"{base}/search"})
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read(6_000_000).decode("utf-8", "replace"))
    except HTTPError as e:
        return e.code, {"error": e.read(300).decode("utf-8", "replace")}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def preview(v, n=90):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return " ".join(s.split())[:n]


def run(out=print, sleep=time.sleep, base=BASE, queries=QUERIES):
    out("== 1. satu listing: kolom yang terisi")
    status, data = search("egs", size=1, base=base)
    results = data.get("results") if isinstance(data, dict) else None
    if status != 200 or not results:
        out(f"gagal: status={status} {preview(data, 200)}")
        return 1
    item = results[0]
    empty = []
    for key in sorted(item):
        val = item[key]
        if val in (None, "", [], {}, False):
            empty.append(key)
        elif key == "store" and isinstance(val, dict):
            out(f"store = kunci {sorted(val)}")
        else:
            out(f"{key} = {preview(val)}")
    out(f"kolom kosong atau false di listing ini ({len(empty)}): " + ", ".join(empty))

    out("== 2. kata kunci per kartu")
    for q in queries:
        sleep(1)
        status, data = search(q, size=45, base=base)
        res = data.get("results") if isinstance(data, dict) else None
        if status != 200 or res is None:
            out(f"[{q}] gagal: status={status} {preview(data, 120)}")
            continue
        out(f"[{q}] total={data.get('total')} diterima={len(res)}")
        for it in res[:6]:
            show = {k: it[k] for k in SHOW if k in it and it[k] not in (None, "")}
            out("   " + preview(show, 230))
    out("== selesai. Kirim seluruh output ini.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
