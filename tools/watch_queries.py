#!/usr/bin/env python3
"""Print search queries for every tracked card type, for the EGS and the RAW dataset.

Usage: python3 tools/watch_queries.py [latest.json]

Output is JSON: one entry per card type with `egs` and `raw` query lists in priority
order. The collector runs them against toco search, merges the results by listing URL
and classifies each title with tools/classify.py. Read-only.
"""
import json
import re
import sys


def queries(t):
    name = re.sub(r"[<>]", "", t["name"]).strip()
    base = [f"{name} {t['number']}", f"{name} {t['set']}", f"{t['number']} {t['set']}"]
    return {
        "egs": [f"{q} egs" for q in base],
        "raw": base + [f"{name} {t['number']} indo"],
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "latest.json"
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    seen, out = set(), []
    for l in d["listings"]:
        key = (l["name"], l["set_code"], l["card_number"], l["rarity"])
        if key in seen:
            continue
        seen.add(key)
        t = {"name": l["name"], "set": l["set_code"], "number": l["card_number"], "rarity": l["rarity"]}
        out.append({**t, **queries(t)})
    print(json.dumps({"types": out}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
