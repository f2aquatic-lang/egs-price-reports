#!/usr/bin/env python3
"""Read-only summary of the collector's SQLite file: what the RAW search actually returns.

Usage: python3 tools/db_report.py [/opt/egs-raw/raw.sqlite] [--top 25] [--hours 48]

Shows (1) how the listings seen recently split by kind and language, (2) the listings that were counted,
and (3) the card numbers that appear most among listings that match no tracked card: the raw material for a
catalog of every card, with their asking prices. Nothing is written.
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import classify_title, text_language  # noqa: E402
from collect_raw import MATCHED, quantile  # noqa: E402


def rupiah(n):
    return f"Rp{n:,}".replace(",", ".")


def report(db, hours, top, now=None, out=print):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()
    rows = db.execute(
        "SELECT l.url, l.title, l.ask_idr, l.city, l.stock, l.kind, l.card_set, l.card_number, l.conf, g.lang, g.lang_by "
        "FROM listing l LEFT JOIN lang g ON g.url = l.url WHERE l.last_seen >= ?", (cutoff,)).fetchall()
    out(f"{len(rows)} listing dilihat dalam {hours} jam terakhir")
    kinds = Counter(r[5] for r in rows)
    out("jenis: " + ", ".join(f"{k}={n}" for k, n in kinds.most_common()))

    raws = [r for r in rows if r[5] == "raw" and (r[4] is None or r[4] > 0)]
    langs = Counter()
    for url, title, ask, city, stock, kind, cset, cnum, conf, lang, lang_by in raws:
        langs[("sudah dicek: " + lang) if lang else "teks: " + text_language(title, "")] += 1
    out(f"RAW yang tersedia: {len(raws)} | " + ", ".join(f"{k}={n}" for k, n in langs.most_common()))

    counted = [r for r in raws if r[8] in MATCHED and r[9] == "ID"]
    out(f"\n== Dihitung ({len(counted)}) ==")
    for url, title, ask, city, stock, kind, cset, cnum, conf, lang, lang_by in sorted(counted, key=lambda r: (r[6] or "", r[7] or "", r[2])):
        out(f"{cset} {cnum} | {rupiah(ask)} | {conf} | {lang_by} | {city} | {title[:70]}")

    named = [r for r in raws if r[8] == "nama"]
    out(f"\n== Cocok nama saja, tidak dihitung ({len(named)}); 10 contoh ==")
    for r in named[:10]:
        out(f"{rupiah(r[2])} | {r[1][:90]}")

    groups = defaultdict(list)
    nonum = 0
    for r in raws:
        if r[8] in MATCHED or text_language(r[1], "") == "foreign":
            continue
        c = classify_title(r[1])
        if not c["number"]:
            nonum += 1
            continue
        groups[(c["set"] or "?", c["number"])].append((r[2], r[1], text_language(r[1], "")))
    total = sum(len(v) for v in groups.values())
    out(f"\n== Kartu lain yang muncul (bukan kartu yang dipantau): {len(groups)} nomor kartu dari {total} listing; "
        f"{nonum} listing tanpa nomor kartu ==")
    out("set nomor | listing (teks bilang ID) | termurah-median-termahal | contoh judul")
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:top]
    for (cset, number), items in ranked:
        prices = sorted(p for p, _, _ in items)
        ids = sum(1 for *_, lg in items if lg == "ID")
        out(f"{cset} {number} | {len(items)} ({ids}) | {rupiah(prices[0])} - {rupiah(quantile(prices, .5))} - {rupiah(prices[-1])} | {items[0][1][:70]}")
    return {"listings": len(rows), "counted": len(counted), "other_numbers": len(groups)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default="/opt/egs-raw/raw.sqlite")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--hours", type=int, default=48)
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"Berkas {a.db} tidak ada. Jalankan pengumpul dulu.")
        return 2
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        report(db, a.hours, a.top)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
