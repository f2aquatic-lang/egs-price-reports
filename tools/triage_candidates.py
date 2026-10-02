#!/usr/bin/env python3
"""Triage pending discovery candidates in latest.json. Read-only: prints a report.

Usage: python3 tools/triage_candidates.py [latest.json] [--grades 10,9.5] [--json]

Each pending (untracked) candidate gets one verdict:
  reject      - out of scope (not Pokemon, foreign-language card, or grade not in --grades)
  same-card   - looks like another listing of a card that is already tracked
  new-card    - needs a human decision
The rules are keyword heuristics on search_text; they only order the review queue,
they never change what the bot tracks.
"""
import argparse
import json
import re
import statistics
import sys
from collections import Counter

NON_POKEMON = re.compile(
    r"one ?piece|\bop\d{2}-\d{3}|\bp-\d{3}\b|digimon|\bex\d-\d{3}|yu-?gi-?oh|"
    r"dragon ?ball|\bmtg\b|magic the gathering|union arena", re.I)
FOREIGN = re.compile(
    r"japan\w*|jepang|\bjpn?\b|chinese|china|korea\w*|thai\w*|english", re.I)
PRICE = re.compile(r"Rp\s?([\d.]+)")
CITY = re.compile(r"((?:Kota|Kabupaten) [A-Za-z ]+?)(?:\s*Referensi\w*)?$")


def rp(n):
    return "Rp" + f"{n:,}".replace(",", ".")


def parse(c):
    text = c["search_text"]
    prices = [int(p.replace(".", "")) for p in PRICE.findall(text)]
    city = CITY.search(PRICE.sub("", re.sub(r"-\d+%", "", text)).strip())
    return {
        "url": c["url"], "text": text, "grade": c["grade_hint"],
        "ask_idr": min(prices) if prices else None,
        "list_idr": max(prices) if len(prices) > 1 else None,
        "city": city.group(1) if city else "?",
    }


def clean_name(name):
    return re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()


def match_tracked(text, tracked):
    """Return (listing, how) for the best tracked match, or None."""
    low = text.lower()
    squashed = low.replace(" ", "")
    best = None
    for l in tracked:
        words = clean_name(l["name"])
        name_ok = all(re.search(rf"\b{re.escape(w)}\b", low) for w in words)
        if l["card_number"].lower() in squashed:
            return l, "card number"
        if name_ok:
            how = "name+set" if re.search(rf"\b{re.escape(l['set_code'].lower())}\b", low) else "name only"
            if best is None or how == "name+set":
                best = (l, how)
    return best


def triage(d, grades):
    tracked = d["listings"]
    out = []
    for c in d["discovery"]["candidates"]:
        if c["tracked"]:
            continue
        p = parse(c)
        if NON_POKEMON.search(p["text"]):
            p.update(verdict="reject", reason="not Pokemon")
        elif FOREIGN.search(p["text"]):
            p.update(verdict="reject", reason="foreign-language card")
        elif p["grade"] not in grades:
            p.update(verdict="reject", reason=f"grade {p['grade']} not in scope")
        else:
            m = match_tracked(p["text"], tracked)
            if m:
                p.update(verdict="same-card", reason=m[1], matches=m[0]["url"])
            else:
                p.update(verdict="new-card", reason="")
        out.append(p)
    return out


def report(d, rows):
    tracked = {l["url"]: l for l in d["listings"]}
    by = {k: [r for r in rows if r["verdict"] == k] for k in ("reject", "same-card", "new-card")}
    lines = [f"# Candidate triage ({d['discovery']['generated_at'][:16]} UTC)", "",
             f"{len(rows)} pending -> {len(by['reject'])} reject, "
             f"{len(by['same-card'])} extra listing of a tracked card, "
             f"{len(by['new-card'])} new cards to decide", ""]

    lines += ["## Same card as an already tracked listing", ""]
    groups = {}
    for r in by["same-card"]:
        groups.setdefault(r["matches"], []).append(r)
    for url, rs in groups.items():
        t = tracked[url]
        lines.append(f"**{t['name']} {t['card_number']} {t['set_code']}** "
                     f"(tracked: grade {t['grade']}, {rp(t['latest']['asking_idr'])})")
        for r in sorted(rs, key=lambda r: r["ask_idr"] or 0):
            lines.append(f"- grade {r['grade']} {rp(r['ask_idr'])} [{r['reason']}] {r['city']} :: {r['text'][:70]}")
        same = [r["ask_idr"] for r in rs if r["grade"] == t["grade"]] + [t["latest"]["asking_idr"]]
        if len(same) > 1:
            lines.append(f"  grade {t['grade']} asks across {len(same)} listings: "
                         f"min {rp(min(same))}, median {rp(int(statistics.median(same)))}, max {rp(max(same))}")
        lines.append("")
    if not groups:
        lines += ["(none)", ""]

    lines += ["## New cards needing a decision (highest ask first)", "",
              "| ask | grade | city | listing |", "|---:|:---:|---|---|"]
    for r in sorted(by["new-card"], key=lambda r: -(r["ask_idr"] or 0)):
        lines.append(f"| {rp(r['ask_idr'])} | {r['grade']} | {r['city']} | {r['text'][:80]} |")
    lines += ["", "## Auto-rejected", ""]
    for reason, n in Counter(r["reason"] for r in by["reject"]).most_common():
        lines.append(f"- {reason}: {n}")
        for r in by["reject"]:
            if r["reason"] == reason:
                lines.append(f"    - {r['text'][:80]}")
    cities = Counter(r["city"] for r in rows)
    top, n = cities.most_common(1)[0] if cities else ("-", 0)
    lines += ["", f"Seller-city concentration among pending: {top} = {n}/{len(rows)}"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", default="latest.json")
    ap.add_argument("--grades", default="10,9.5", help="comma-separated in-scope grades")
    ap.add_argument("--json", action="store_true", help="print machine-readable rows")
    a = ap.parse_args()
    with open(a.path, encoding="utf-8") as fh:
        d = json.load(fh)
    rows = triage(d, set(a.grades.split(",")))
    print(json.dumps(rows, indent=2, ensure_ascii=False) if a.json else report(d, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
