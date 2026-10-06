#!/usr/bin/env python3
"""Read the rarity printed on official card pictures, for the cards the official site lists without a rarity mark.

The official site (asia.pokemon-card.com/id) files some cards under "Tanpa Tanda" although the card itself prints a rarity: the reprints of
the 30th Celebration set (MA6 numbers above the printed total: GX cards with RRR or SR, V, VMAX, VSTAR ...) and the cards of the starter decks.
This tool looks at the official picture of such a card with two vision models and writes what BOTH read into tools/card_marks.json,
which the board uses to show the rarity (and "AA" for an alternate-art illustration, e.g. "SR AA"). A field the models do not agree on is not
written. An entry marked "by": "manual" is yours and is never overwritten; edit the file by hand to correct or add one.

Usage (needs the openai or anthropic package and the key in the environment, see docs/vps-setup.md):
  python3 tools/read_card_marks.py --catalog /opt/egs-raw/catalog.json --sets MA6 --limit 3 --dry-run   # what would be read, nothing is called
  python3 tools/read_card_marks.py --catalog /opt/egs-raw/catalog.json --sets MA6 --limit 3             # a trial: three pictures
  python3 tools/read_card_marks.py --catalog /opt/egs-raw/catalog.json --sets MA6,AC3D                  # every candidate of those sets
Candidates: cards without a rarity whose number is above the printed total (secret and special cards), and every card of a set in which no card
has a rarity (decks). Cards already in the file are skipped unless --again.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
import egs_watch  # noqa: E402
from build_site import MARKS_PATH, load_marks, mark_label as label  # noqa: E402

RARITIES = {"C", "U", "R", "RR", "RRR", "PR", "TR", "SR", "HR", "UR", "AR", "SAR", "S", "SSR", "ACE", "BWR", "MUR", "MA", "FUR", "K", "A"}
MARK_PROMPT = (
    "This is the official picture of one Pokemon trading card. Read only what is printed and visible; use null when you cannot read something and never guess. "
    "Answer with one JSON object and nothing else, with these keys: "
    "rarity (the rarity code printed near the bottom of the card, such as C, U, R, RR, RRR, SR, HR, UR, AR or SAR, or null when the card prints none), "
    "alternate_art (true when the Pokemon is drawn in a full-card scene illustration that runs across the whole card, an alternate art; "
    "false when it is the regular artwork inside the usual frame; null when you cannot tell).")


def key_of(card):
    return f"{card['set']}|{card['number']}"


def candidates(catalog, sets=None):
    """Official cards without a rarity that may print one: above the printed total, or in a set where no card has any rarity."""
    wanted = {x.strip().upper() for x in sets.split(",")} if sets else None
    marked = {c["set"] for c in catalog["cards"] if c.get("rarity")}
    out = []
    for c in catalog["cards"]:
        if c.get("rarity") or not c.get("image") or not c.get("number"):
            continue
        if wanted and c["set"].upper() not in wanted:
            continue
        above = c.get("num") and c.get("den") and c["num"] > c["den"]
        if above or c["set"] not in marked:
            out.append(c)
    return out


def parse_mark(text):
    """{'rarity': 'RRR' | None, 'alt_art': True | False | None} from a model's answer; None when it is not usable."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    rarity = data.get("rarity")
    rarity = str(rarity).strip().upper() if rarity not in (None, "", "null", "None") else None
    if rarity and rarity not in RARITIES:
        rarity = "?"                                    # something that is not a rarity code: it can never agree with a second reader
    alt = data.get("alternate_art")
    return {"rarity": rarity, "alt_art": alt if isinstance(alt, bool) else None}


def agree(answers):
    """Both readers must give the same rarity code; the alternate-art flag is kept only when they agree on it as well. {} when they differ."""
    rarities = {a["rarity"] for a in answers}
    if len(rarities) != 1 or None in rarities or "?" in rarities:
        return {}
    out = {"rarity": answers[0]["rarity"]}
    flags = {a["alt_art"] for a in answers}
    if len(flags) == 1 and None not in flags:
        out["alt_art"] = answers[0]["alt_art"]
    return out


def make_reader(provider, models):
    return egs_watch.make_photo_reader(provider, models, prompt=MARK_PROMPT, parse=parse_mark, combine=agree)


def run(args, reader=None, now=None, log=print):
    """Read the candidate pictures and merge what the readers agree on into the marks file. Returns {'read': n, 'written': n, 'skipped': n}."""
    now = now or datetime.now(timezone.utc)
    with open(args.catalog, encoding="utf-8") as fh:
        catalog = json.load(fh)
    marks = load_marks(args.marks)
    todo = [c for c in candidates(catalog, args.sets) if args.again or key_of(c) not in marks["cards"]]
    todo = [c for c in todo if marks["cards"].get(key_of(c), {}).get("by") != "manual"]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} gambar kartu untuk dibaca" + (" (percobaan kering: tidak ada yang dipanggil)" if args.dry_run else ""))
    count = {"read": 0, "written": 0, "skipped": 0}
    for c in todo:
        if args.dry_run:
            log(f"  {c['set']} {c['number']} {c['name']}")
            continue
        answer = reader(c["image"])
        count["read"] += 1
        if answer is None:
            count["skipped"] += 1
            log(f"  ! {c['set']} {c['number']} {c['name']}: tidak terbaca, dicoba lagi di putaran berikutnya")
            continue
        result = answer
        if result.get("rarity") in (None, "?"):
            count["skipped"] += 1
            log(f"  - {c['set']} {c['number']} {c['name']}: kedua pembaca tidak sepakat atau tidak ada tanda tercetak; tidak dicatat")
            continue
        marks["cards"][key_of(c)] = {**result, "name": c["name"], "by": "foto resmi (dua model sepakat)", "at": now.date().isoformat()}
        count["written"] += 1
        log(f"  + {c['set']} {c['number']} {c['name']}: {label(result)}")
        if not args.dry_run:
            save(marks, args.marks)                      # after every card: a stopped run keeps what it learned
    return count


def save(marks, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(marks, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--marks", default=MARKS_PATH)
    ap.add_argument("--sets", help="kode set dipisah koma (bawaan: semua kandidat)")
    ap.add_argument("--limit", type=int, help="paling banyak sekian gambar putaran ini")
    ap.add_argument("--again", action="store_true", help="baca ulang kartu yang sudah ada di berkas (entri manual tetap dilindungi)")
    ap.add_argument("--dry-run", action="store_true", help="hanya tampilkan kandidatnya")
    ap.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    ap.add_argument("--model", default=os.environ.get("EGS_PHOTO_MODEL"), help="model dipisah koma; semuanya harus sepakat (bawaan openai: gpt-4.1,gpt-4o)")
    args = ap.parse_args(argv)
    models = args.model or egs_watch.DEFAULT_PHOTO_MODELS[args.provider]
    if not args.dry_run and len([m for m in models.split(",") if m.strip()]) < 2:
        print("Pakai dua model atau lebih (dipisah koma, mis. gpt-4.1,gpt-4o): satu bacaan tidak cukup untuk mencatat rarity.")
        return 1
    reader = None if args.dry_run else make_reader(args.provider, models)
    count = run(args, reader)
    print(f"dibaca {count['read']}, dicatat {count['written']}, dilewati {count['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
