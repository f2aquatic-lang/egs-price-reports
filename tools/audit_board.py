#!/usr/bin/env python3
"""Audit of the RadarKartu data files and the board built from them. Read-only: nothing is fetched, nothing is written.

Usage:  python3 tools/audit_board.py [--dir .] [--json]
It reads latest.json, raw.json, egs_watch.json, catalog.json, jp_prices.json, rates.json, tcgdex.json and card_marks.json from --dir (the
repository checkout, where the publisher copies them), builds the board in memory exactly as the site is built, and reports:

  1. umur data      how old each file is against what its refresh schedule promises
  2. validasi       raw.json and egs_watch.json against their own validators
  3. papan          rows, sets, duplicates, impossible prices, the base-price rule (a Common with no listing is Rp500, never 0)
  4. cakupan        how many cards have a price from listings, a base price, or none, and why
  5. kewajaran      asking prices against the Japanese (Yuyutei) price: the share that is more than 3 times it
  6. slab EGS       how thin the slab prices are (listings per card and grade), the review queue, the sources
  7. acuan          how much of the board has a Japanese and an English reference, and their age
  8. folder masuk   the inbox of docs/inbox.md: files, age of the newest, the last report of each collector

Exit code 0 when nothing is wrong, 1 when a check FAILED (validation error, duplicate rows, impossible price, a file missing or unreadable).
Warnings (PERHATIAN) do not change the exit code. Standard library only.
"""
import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
import egs_watch  # noqa: E402
import validate_raw  # noqa: E402

# (file, hours after which the file is older than its schedule promises, the schedule)
FILES = (("latest.json", 14, "bot lama, tiap jam"), ("raw.json", 14, "pengumpul RAW, 4 kali sehari"), ("egs_watch.json", 14, "pemantau slab, tiap beberapa jam"),
         ("jp_prices.json", 30, "Yuyutei, tiap hari"), ("rates.json", 30, "kurs, tiap hari"), ("tcgdex.json", 30, "TCGdex (harga EN), tiap hari"),
         ("catalog.json", 24 * 9, "katalog resmi, tiap minggu"))
DECKS = ("S10D", "S10P", "SC1D", "SCD", "AS1D", "AS2D", "AS3D", "AS4D", "AC3D", "SVDS")


class Report:
    def __init__(self):
        self.lines, self.failed, self.warned = [], 0, 0

    def head(self, text):
        self.lines.append(f"\n== {text}")

    def ok(self, text):
        self.lines.append(f"  OK        {text}")

    def warn(self, text):
        self.warned += 1
        self.lines.append(f"  PERHATIAN {text}")

    def fail(self, text):
        self.failed += 1
        self.lines.append(f"  GAGAL     {text}")

    def info(self, text):
        self.lines.append(f"            {text}")


def load(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def age_hours(iso, now):
    t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return (now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))).total_seconds() / 3600


def audit(folder, now=None, inbox=None):
    now = now or datetime.now(timezone.utc)
    rep, data = Report(), {name: load(folder, name) for name, _, _ in FILES}
    marks = load(folder, "card_marks.json")

    rep.head("1. umur data")
    for name, limit, schedule in FILES:
        d = data[name]
        if d is None:
            (rep.fail if name in ("latest.json", "raw.json") else rep.warn)(f"{name}: tidak ada atau tidak terbaca")
            continue
        h = age_hours(d.get("generated_at") or d.get("fetched_at"), now) if (d.get("generated_at") or d.get("fetched_at")) else None
        if h is None:
            rep.warn(f"{name}: tanpa waktu pembuatan")
        elif h > limit:
            rep.warn(f"{name}: berumur {h:.0f} jam, jadwalnya ({schedule}) menjanjikan paling lama sekitar {limit} jam")
        else:
            rep.ok(f"{name}: {h:.1f} jam ({schedule})")

    raw, latest, egs = data["raw.json"], data["latest.json"], data["egs_watch.json"]
    rep.head("2. validasi")
    if raw:
        errors, warnings = validate_raw.validate(raw)
        (rep.fail if errors else rep.ok)(f"raw.json: {len(errors)} galat, {len(warnings)} peringatan" + (f" ({errors[0]})" if errors else ""))
    if egs:
        errors = egs_watch.validate(egs)
        (rep.fail if errors else rep.ok)(f"egs_watch.json: {len(errors)} galat" + (f" ({errors[0]})" if errors else ""))
    if not (raw and latest):
        rep.fail("papan tidak bisa dibangun tanpa latest.json dan raw.json")
        return rep, None

    board = build_site.build(latest, raw, data["catalog.json"], data["jp_prices.json"], data["rates.json"], egs, (marks or {}).get("cards"), data["tcgdex.json"])
    rows, sets = board["raw_cards"], board["sets"]
    rep.head("3. papan")
    rep.info(f"{len(rows)} baris di {len(sets)} set ({sum(1 for s in sets if s['official'])} dengan katalog resmi)")
    dup = [k for k, n in Counter((r["set"], r["number"], r["variant"]) for r in rows).items() if n > 1]
    (rep.fail if dup else rep.ok)(f"baris ganda (set, nomor, versi): {len(dup)}")
    bad = [r for r in rows if r["basis"] and not (r["median"] and r["median"] > 0)]
    (rep.fail if bad else rep.ok)(f"baris berharga nol atau kosong padahal bertanda berharga: {len(bad)}")
    base = [r for r in rows if r["basis"] == "default"]
    rep.ok(f"harga dasar: {len(base)} kartu, nilainya {sorted({r['median'] for r in base})} (kartu biasa tanpa listing = Rp500, tidak pernah 0)") if base else rep.info("tanpa harga dasar")
    wrong = [r for r in base if r["rarity"] not in ("C", None, "")]      # "" = no printed mark (High Class plain cards), priced like a Common
    (rep.fail if wrong else rep.ok)(f"harga dasar pada rarity selain C atau tanpa tanda: {len(wrong)}")

    rep.head("4. cakupan harga")
    by = Counter(r["basis"] for r in rows)
    listed, unpriced = by["listing"], by[None]
    rep.info(f"dari listing: {listed} ({listed / len(rows):.0%}) | harga dasar: {by['default']} | belum berharga: {unpriced} ({unpriced / len(rows):.0%})")
    decks = [r for r in rows if r["basis"] is None and r["set"] in DECKS]
    foil = [r for r in rows if r["basis"] is None and r["variant"] and r["set"] not in DECKS]
    rest = [r for r in rows if r["basis"] is None and not r["variant"] and r["set"] not in DECKS]
    rep.info(f"belum berharga: {len(rest)} kartu biasa, {len(foil)} baris versi foil, {len(decks)} kartu deck")
    rarity = Counter(r["rarity"] or "-" for r in rest)
    rep.info("kartu biasa belum berharga per rarity: " + ", ".join(f"{k} {v}" for k, v in rarity.most_common(8)))
    top = Counter()
    have = Counter()
    for r in rows:
        if r["rarity"] in ("RR", "AR", "SR", "SAR", "UR", "MUR") and not r["variant"] and r["set"] not in DECKS:
            top[r["rarity"]] += 1
            have[r["rarity"]] += 1 if r["basis"] == "listing" else 0
    for k in ("RR", "AR", "SR", "SAR"):
        if top[k]:
            rep.info(f"{k}: {have[k]} dari {top[k]} kartu punya harga dari listing ({have[k] / top[k]:.0%})")
    thin = Counter(min(r["n"], 3) for r in rows if r["basis"] == "listing")
    rep.info(f"kartu berharga listing menurut jumlah listing: 1 listing {thin[1]}, 2 listing {thin[2]}, 3+ {thin[3]}")

    rep.head("5. kewajaran terhadap harga Yuyutei")
    jpy = (board.get("rates") or {}).get("JPY", {}).get("idr")
    ratios = [(r["median"] / (r["jp"]["yen"] * jpy), r) for r in rows
              if jpy and r["basis"] == "listing" and r["jp"] and r["jp"]["yen"] >= 100 and r["median"]]
    if ratios:
        for label, pick in (("1 listing", lambda r: r["n"] == 1), ("2 listing atau lebih", lambda r: r["n"] >= 2)):
            v = sorted(x for x, r in ratios if pick(r))
            if v:
                rep.info(f"{label}: {len(v)} kartu, rasio median {statistics.median(v):.2f}, lebih dari 3 kali harga Yuyutei: {sum(1 for x in v if x > 3)} ({sum(1 for x in v if x > 3) / len(v):.0%})")
        worst = sorted(ratios, key=lambda t: -t[0])[:3]
        for x, r in worst:
            rep.info(f"  tertinggi: {r['set']} {r['number']} {r['name']}: Rp{r['median']:,} ({x:.1f}x harga Yuyutei, {r['n']} listing)".replace(",", "."))
        wide = sum(1 for x, r in ratios if r["n"] >= 2 and x > 3) / max(1, sum(1 for x, r in ratios if r["n"] >= 2))
        (rep.warn if wide > 0.1 else rep.ok)(f"kartu dengan 2 listing atau lebih dan harga lebih dari 3 kali Yuyutei: {wide:.0%}")
    else:
        rep.info("tanpa harga Yuyutei atau kurs yen: tidak ada pembanding")

    rep.head("6. slab EGS")
    if egs:
        s = egs["summary"]
        grades = [g for c in egs["cards"] for g in c["grades"]]
        one = sum(1 for g in grades if g.get("n", 0) <= 1)
        rep.info(f"{s['accepted_total']} listing diterima di {s['cards']} kartu dari {s['sellers_unique']} penjual; sumber: " + ", ".join(f"{k} {v['counted']}" for k, v in s.get("by_source", {}).items()))
        (rep.warn if grades and one / len(grades) > 0.5 else rep.ok)(f"{one} dari {len(grades)} grade hanya punya satu listing (harga tipis)")
        (rep.warn if s["ambiguous"] > s["accepted_total"] else rep.info)(f"antrean ambigu: {s['ambiguous']} listing menunggu keputusan (diterima: {s['accepted_total']})")
        run = egs.get("run") or {}
        rep.info(f"putaran terakhir: {run.get('results_seen')} hasil dilihat, {run.get('photo_reads', 0)} foto dibaca, impor: {run.get('imported') or 'tidak ada'}")
        if not run.get("photo_reads") and s["ambiguous"]:
            rep.warn("pembacaan foto tidak aktif pada putaran terakhir sementara antrean ambigu berisi; aktifkan EGS_PHOTO_MODE untuk pemantau")

    rep.head("7. acuan Jepang dan Inggris")
    jp_rows, en_rows = sum(1 for r in rows if r["jp"]), sum(1 for r in rows if r["en"])
    rep.info(f"acuan JP: {jp_rows} baris ({sum(1 for r in rows if r['jp'] and r['jp'].get('via'))} lewat jembatan kartu) | acuan EN: {en_rows} baris")
    if board.get("en_meta"):
        h = age_hours(board["en_meta"]["iso"], now)
        (rep.warn if h > 72 else rep.ok)(f"data EN (TCGdex) berumur {h:.0f} jam" + ("; papan sudah menulis 'data lama'" if h > 72 else ""))
    else:
        rep.warn("tanpa data TCGdex: kolom Acuan EN tidak tampil")
    rep.head("8. folder masuk")
    if not inbox or not os.path.isdir(inbox):
        rep.info(f"{inbox or '(tidak diatur)'}: belum ada (docs/inbox.md)")
    else:
        files = [os.path.join(inbox, n) for n in sorted(os.listdir(inbox)) if n.lower().endswith((".json", ".csv", ".tsv")) and not n.startswith(".")]
        if not files:
            rep.warn(f"{inbox}: ada, tetapi tidak berisi berkas")
        else:
            newest = max(os.path.getmtime(f) for f in files)
            h = (now.timestamp() - newest) / 3600
            (rep.warn if h > 36 else rep.ok)(f"{len(files)} berkas, yang terbaru {h:.1f} jam" + ("; tidak ada kiriman baru dalam 36 jam: listing masuk sudah kedaluwarsa" if h > 36 else ""))
        for consumer in ("raw", "slab"):
            path = os.path.join(inbox, "status", f"laporan-{consumer}.txt")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().splitlines()
                rep.info(f"laporan {consumer}: " + " | ".join(l for l in text[1:3]))
    return rep, board


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=".", help="folder berisi berkas data (bawaan: folder ini)")
    ap.add_argument("--inbox", default=os.environ.get("EGS_INBOX", "/opt/egs-raw/inbox"), help="folder masuk (docs/inbox.md)")
    a = ap.parse_args(argv)
    rep, _ = audit(a.dir, inbox=a.inbox)
    print("AUDIT RADARKARTU" + "\n".join(rep.lines))
    print(f"\nhasil: {rep.failed} GAGAL, {rep.warned} PERHATIAN")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
