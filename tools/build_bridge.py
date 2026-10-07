#!/usr/bin/env python3
"""Pair the cards of the Indonesian official catalog with their English and Japanese editions (bridge.json).

Inputs, both already in the repository: catalog.json (tools/fetch_catalog.py) and tcgdex.json (tools/fetch_tcgdex.py).
Why: an Indonesian card is numbered differently from its Japanese original (Mega Charizard X ex SAR is 121/103 in Indonesia, 110/080 in Japan), so
neither prices nor references can be looked up by number. The chain used here, each step with a visible rule:

  1. Indonesian set -> English set: every English set is tried and the one that pairs the most cards wins, if it pairs at least 40% of the Indonesian
     set's cards and is clearly ahead of the next one. (Trainer cards have Indonesian names and never pair; the Pokemon of a set carry it.)
  2. Indonesian card -> English card, inside that pair of sets: the same name and the same rarity (Common = C, Uncommon = U, Rare = R, Double rare = RR,
     Illustration rare = AR, Ultra Rare = SR, Special illustration rare = SAR, Mega Hyper Rare = MUR). A card with no mark in a High Class set is a
     Common, Uncommon or Rare. When several cards share name and rarity they are paired in order of number only if both sides have the same count.
  3. English card -> Japanese card: the same Pokedex number, illustrator and rarity in the Japanese sets whose cards overlap the English set.

Nothing is guessed: a card that does not pair cleanly stays unpaired and the report counts it. Standard library only.

Usage:  python3 tools/build_bridge.py --catalog catalog.json --tcgdex tcgdex.json --out bridge.json [--report]
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

RARITY = {"common": "C", "uncommon": "U", "rare": "R", "double rare": "RR", "illustration rare": "AR", "ultra rare": "SR", "secret rare": "SR",
          "special illustration rare": "SAR", "mega hyper rare": "MUR", "hyper rare": "HR", "ace spec rare": "ACE", "shiny rare": "S",
          "shiny ultra rare": "SSR", "black white rare": "BWR"}
PLAIN = {"C", "U", "R", None}      # what an Indonesian card without a rarity mark can be on the English side
MIN_YIELD = 0.4                    # share of an Indonesian set's cards that its English set must pair
MARGIN = 0.15                      # and by how much (in share of the set) it must beat the runner-up


def norm_name(text):
    """One spelling for a card name on both sides. The Indonesian site writes "Garchomp <Cynthia> ex" and "Archer Tim Roket" where the English
    edition writes "Cynthia's Garchomp ex" and "Team Rocket's Archer": the owner goes first and the possessive 's is dropped."""
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    m = re.search(r"<([^>]+)>", s)
    if m:
        s = m.group(1) + " " + s[:m.start()] + " " + s[m.end():]
    s = re.sub(r"\b(.+?) tim roket\b", r"team rocket \1", s)
    s = s.replace("'s ", " ").replace("’s ", " ")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def code_of(name):
    """TCGdex rarity name -> the code the Indonesian site uses (None for an unknown or missing rarity)."""
    return RARITY.get((name or "").strip().lower())


def number_of(card):
    m = re.match(r"\d+", str(card.get("local") or card.get("number") or ""))
    return int(m.group(0)) if m else 10 ** 6


def regular_cards(catalog):
    """{set code: [catalog card]} with one entry per printed number (the foil versions of a number share its pairing)."""
    out, seen = defaultdict(list), set()
    for c in catalog["cards"]:
        key = (c["set"], c["number"])
        if key in seen or not c.get("number"):
            continue
        seen.add(key)
        out[c["set"]].append(c)
    return out


def pair_sets(id_cards, en_by_set):
    """(english set id, {catalog number: english card id}, ambiguous count, yield) for the English set that pairs the most cards, else (None, {}, 0, best yield)."""
    tries = sorted(((len(found) / len(id_cards), sid, found, amb) for sid, en_cards in en_by_set.items()
                    for found, amb in [match_id_to_en(id_cards, en_cards)]), key=lambda t: (-t[0], t[1]))
    if not tries:
        return None, {}, 0, 0.0
    best = tries[0]
    if best[0] >= MIN_YIELD and (len(tries) == 1 or best[0] - tries[1][0] >= MARGIN):
        return best[1], best[2], best[3], best[0]
    return None, {}, 0, best[0]


def match_id_to_en(id_cards, en_cards):
    """{catalog number: english card id} for one pair of sets, by name and rarity."""
    by_name = defaultdict(list)
    for c in en_cards:
        by_name[norm_name(c["name"])].append(c)
    id_by_name = defaultdict(list)
    for c in id_cards:
        id_by_name[norm_name(c["name"])].append(c)
    out, ambiguous = {}, 0
    for name, ids in id_by_name.items():
        ens = by_name.get(name)
        if not ens:
            continue
        buckets = defaultdict(lambda: ([], []))
        for c in ids:
            buckets["plain" if c.get("rarity") in PLAIN else c["rarity"]][0].append(c)
        for c in ens:
            buckets[rarity_class(c["rarity"])][1].append(c)
        for key, (a, b) in buckets.items():
            if not a or not b:
                continue
            if len(a) != len(b):
                ambiguous += len(a)
                continue
            for x, y in zip(sorted(a, key=lambda c: c.get("num") or number_of(c)), sorted(b, key=number_of)):
                out[x["number"]] = y["id"]
    return out, ambiguous


def rarity_class(name):
    """C, U, R and a missing mark are one class (they differ between the Japanese and the English print of a card and some Japanese sets mark none)."""
    code = code_of(name)
    return "plain" if code in PLAIN else code


def signature(card):
    dex = tuple(card.get("dex") or ())
    return (dex, (card.get("illustrator") or "").strip().lower(), rarity_class(card["rarity"])) if dex and card.get("illustrator") else None


def match_en_to_ja(en_cards, ja_cards):
    """{english card id: japanese card id} by Pokedex number, illustrator and rarity; only where both sides have the same number of such cards."""
    groups = defaultdict(lambda: ([], []))
    for c in en_cards:
        sig = signature(c)
        if sig:
            groups[sig][0].append(c)
    for c in ja_cards:
        sig = signature(c)
        if sig:
            groups[sig][1].append(c)
    out = {}
    for a, b in groups.values():
        if a and len(a) == len(b):
            for x, y in zip(sorted(a, key=number_of), sorted(b, key=number_of)):
                out[x["id"]] = y["id"]
    return out


def pair_ja_sets(en_cards, ja_by_set, min_share=0.25):
    """The Japanese sets that share Pokemon and illustrators with an English set (the English MEGA Evolution set is two Japanese sets)."""
    sigs = {signature(c) for c in en_cards} - {None}
    picked = []
    for sid, cards in ja_by_set.items():
        have = {signature(c) for c in cards} - {None}
        if have and len(sigs & have) / len(have) >= min_share:
            picked.append((len(sigs & have) / len(have), sid))
    return [sid for _, sid in sorted(picked, reverse=True)]


def yuyutei_set(ja_set_id):
    """Yuyu-tei's set code of a Japanese set id: M2a -> m02a, SV8a -> sv08a, M1L -> m01l (lower case, the number zero padded to two digits)."""
    m = re.fullmatch(r"([A-Za-z]+)(\d+)([A-Za-z]*)", ja_set_id or "")
    return (m.group(1) + m.group(2).zfill(2) + m.group(3)).lower() if m else None


def jp_key(ja_card):
    """(Yuyu-tei set code, card number) of a Japanese card of tcgdex.json, or None when the card has no plain number."""
    m = re.match(r"\d+", str(ja_card.get("local") or ""))
    code = yuyutei_set(ja_card.get("set"))
    return (code, int(m.group(0))) if m and code else None


def squash(text):
    return re.sub(r"\s+", "", text or "")


def check_jp(bridge, tcgdex, jp):
    """{'same': n, 'different': n, 'absent': n}: do the Japanese cards the bridge found have the same name on Yuyu-tei under (set code, number)?
    This checks only the last step (the Yuyu-tei set code and number derived from a Japanese card of TCGdex). It says nothing about whether the
    Indonesian card was paired with the right Japanese card: for that see check_numbers."""
    ja = {c["id"]: c for c in tcgdex["cards"] if c["lang"] == "ja"}
    shop = defaultdict(list)
    for c in jp.get("cards", []):
        m = re.match(r"\d+", c["number"])
        if m:
            shop[(c["set"], int(m.group(0)))].append(squash(c["name"]))
    out = {"same": 0, "different": 0, "absent": 0}
    for entry in bridge["cards"].values():
        card = ja.get(entry.get("ja"))
        key = jp_key(card) if card else None
        if not key:
            continue
        names = shop.get(key)
        out["absent" if not names else "same" if squash(card["name"]) in names else "different"] += 1
    return out


def check_numbers(bridge, tcgdex, mapping):
    """{set: {'same': n, 'other_number': n, 'other_set': n}} for the Indonesian sets that tools/sets.json maps to a Yuyu-tei set because every number is the
    same in both (jp_sets). There the right Japanese card is known without the bridge: the Yuyu-tei set of the mapping and the Indonesian number.
    Comparing it with the Japanese card the bridge reached through the English edition tests the whole chain (name, rarity, Pokedex number, illustrator)."""
    ja = {c["id"]: c for c in tcgdex["cards"] if c["lang"] == "ja"}
    out = {}
    for key, entry in bridge["cards"].items():
        code, number = key.split("|", 1)
        card = ja.get(entry.get("ja"))
        jk = jp_key(card) if card else None
        if code not in mapping or not jk:
            continue
        row = out.setdefault(code, {"same": 0, "other_number": 0, "other_set": 0})
        row["same" if jk == (mapping[code], int(number.split("/")[0])) else "other_set" if jk[0] != mapping[code] else "other_number"] += 1
    return out


def build(catalog, tcgdex, now=None):
    now = now or datetime.now(timezone.utc)
    id_sets = regular_cards(catalog)
    en_by_set, ja_by_set = defaultdict(list), defaultdict(list)
    for c in tcgdex["cards"]:
        (en_by_set if c["lang"] == "en" else ja_by_set)[c["set"]].append(c)
    cards_out, sets_out = {}, []
    for code, cards in sorted(id_sets.items()):
        en_set, to_en, ambiguous, share = pair_sets(cards, en_by_set)
        row = {"id_set": code, "cards": len(cards), "en_set": en_set, "yield": round(share, 2), "ja_sets": [], "en_matched": len(to_en), "ja_matched": 0, "ambiguous": ambiguous}
        if en_set:
            en_cards = en_by_set[en_set]
            row["ja_sets"] = pair_ja_sets(en_cards, ja_by_set)
            to_ja = match_en_to_ja(en_cards, [c for sid in row["ja_sets"] for c in ja_by_set[sid]])
            for number, en_id in to_en.items():
                entry = {"en": en_id}
                if en_id in to_ja:
                    entry["ja"] = to_ja[en_id]
                    row["ja_matched"] += 1
                cards_out[f"{code}|{number}"] = entry
        sets_out.append(row)
    return {"schema_version": 1, "generated_at": now.isoformat(), "sets": sets_out, "cards": cards_out}


def report(data):
    lines = [f"{'set':7} {'kartu':>5} {'set Inggris':12} {'hasil':>5} {'set Jepang':16} {'cocok EN':>8} {'cocok JP':>8} {'ambigu':>6}"]
    for r in data["sets"]:
        lines.append(f"{r['id_set']:7} {r['cards']:5} {(r['en_set'] or '-'):12} {r['yield']:5.0%} {','.join(r['ja_sets']) or '-':16} {r['en_matched']:8} {r['ja_matched']:8} {r['ambiguous']:6}")
    total = sum(r["cards"] for r in data["sets"] if r["en_set"])
    lines.append(f"kartu di set yang berpasangan: {total}; cocok ke Inggris: {sum(r['en_matched'] for r in data['sets'])}; ke Jepang: {sum(r['ja_matched'] for r in data['sets'])}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--tcgdex", default="tcgdex.json")
    ap.add_argument("--out", default="bridge.json")
    ap.add_argument("--report", action="store_true", help="cetak ringkasan per set")
    ap.add_argument("--check-jp", metavar="JP_PRICES_JSON", help="cocokkan nama Jepang kartu hasil jembatan dengan baris Yuyutei (hanya menguji langkah terakhir)")
    ap.add_argument("--check-numbers", action="store_true", help="uji seluruh rantai pada set yang nomornya sama di Jepang (jp_sets di tools/sets.json)")
    a = ap.parse_args(argv)
    with open(a.catalog, encoding="utf-8") as fh:
        catalog = json.load(fh)
    with open(a.tcgdex, encoding="utf-8") as fh:
        tcgdex = json.load(fh)
    data = build(catalog, tcgdex)
    with open(a.out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(a.out + ".tmp", a.out)
    if a.report:
        print(report(data))
    if a.check_numbers:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sets.json"), encoding="utf-8") as fh:
            mapping = {k.upper(): v for k, v in json.load(fh).get("jp_sets", {}).items()}
        result = check_numbers(data, tcgdex, mapping)
        for code, r in sorted(result.items()):
            print(f"  {code:6} kartu Jepang sama dengan nomornya: {r['same']:4}  nomor lain di set yang sama: {r['other_number']:3}  set lain: {r['other_set']:3}")
        total = {k: sum(r[k] for r in result.values()) for k in ("same", "other_number", "other_set")}
        print("uji rantai (set bernomor sama): sama {same}, nomor lain {other_number}, set lain {other_set}".format(**total))
    if a.check_jp:
        with open(a.check_jp, encoding="utf-8") as fh:
            print("langkah terakhir (nama Jepang = baris Yuyutei): sama {same}, beda {different}, tidak ada barisnya {absent}".format(**check_jp(data, tcgdex, json.load(fh))))
    print(f"ditulis: {a.out} ({len(data['cards'])} kartu berpasangan)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
