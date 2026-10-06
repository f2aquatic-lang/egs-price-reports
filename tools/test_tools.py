#!/usr/bin/env python3
"""Unit tests for the read-only tools. Run: python3 tools/test_tools.py

Real titles below were copied from published latest.json candidates; the grade the
bot recorded (grade_hint) is the expected answer. RAW titles are made up.
"""
import argparse
import csv
import collections
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
import collect_raw  # noqa: E402
import db_report  # noqa: E402
import egs_identity  # noqa: E402
import egs_watch  # noqa: E402
import egs_worksheet  # noqa: E402
import fetch_catalog  # noqa: E402
import fetch_jp_prices  # noqa: E402
import fetch_rates  # noqa: E402
import probe_marketplaces  # noqa: E402
import probe_pokemon_card  # noqa: E402
import probe_toco  # noqa: E402
import probe_toco_fields  # noqa: E402
import probe_toco_next  # noqa: E402
import read_card_marks  # noqa: E402
import validate_raw  # noqa: E402
import watch_queries  # noqa: E402
from classify import classify_title  # noqa: E402

REAL_SLABS = [  # (title, bot grade_hint)
    ("Sisa 1 Aerodactyl V SR AA S11 ID EGS 9.5 Rp1.150.000 Kota Jambi", "9.5"),
    ("Sisa 1 EGS GemMint 10 Mega Charizard X EX MA Inferno X #223Japanese Grade: EGS 10 GEM MINT Rp3.400.000 Kota Ja", "10"),
    ("Sisa 1 Kartu EGS Slab 9 Mint Pokemon GO ID Charizard Holo Rp550.000 Kota Bandar Lampung", "9"),
    ("Sisa 1 MA CHARIZARD EGS 9,5 INDO Rp570.000 Kota Jakarta Timur", "9.5"),
    ("Sisa 1 Pika egs 10 Rp600.000 Kabupaten Pemalang", "10"),
    ("Sisa 1 TCG POKEMON INDONESIA AR 210/193 ZEKROM N + SLAB EGS 9.5 Rp550.000 Rp750.000 -27% Kota Tangerang Selata", "9.5"),
]
RAW_CASES = {
    "Pikachu ex SAR 234/193 MA3 ID Mega Impian NM": ("raw", "ID"),
    "Mega Venusaur ex SAR 173/126 MA1 raw ungraded": ("raw", "unknown"),
    "Zekrom N AR 210/193 MA3 Japanese": ("raw", "foreign"),
    "Dragonite ex 232/193 MA3 non slab bukan psa": ("raw", "unknown"),
    "Pikachu ex SR 211/182 SV8s PSA 10": ("slab", "unknown"),
    "Booster Box Pokemon Mega Impian MA3 Indonesia Sealed": ("product", "ID"),
    "Sleeves Pokemon Pikachu 65pcs": ("product", "unknown"),
    "One Piece Luffy OP05-119 SEC": ("other", "unknown"),
}


class Classify(unittest.TestCase):
    def test_real_slab_grades_match_bot(self):
        for title, grade in REAL_SLABS:
            r = classify_title(title)
            self.assertEqual((r["kind"], r["grade"]), ("slab", grade), title)

    def test_kinds_and_language(self):
        for title, (kind, lang) in RAW_CASES.items():
            r = classify_title(title)
            self.assertEqual((r["kind"], r["lang"]), (kind, lang), title)

    def test_ball_pattern_versions_from_real_titles(self):
        from classify import variant_of
        cases = [("Budew Pokeball - 001/187 - Festival Terastal EX (SV8a)", "poke_ball"), ("pokemon (ID) budew - SV8a 001/187 - Master Ball", "master_ball"),
                 ("PAWNIARD HOLO MASTERBALL ma3 116/193 POKEMON CARD TCG INDONESIA", "master_ball"),
                 ("Kartu Pokemon TCG Magearna ma3 I 120/193 Holo Pokeball Indonesia", "poke_ball"),
                 ("Pokemon TCG Indonesia - Magearna Holo (MA3 120/193) ~ Evolusi Mega Impian", ""), ("Pikachu ex SAR 234/193 MA3 Indo", "")]
        for title, expected in cases:
            self.assertEqual(variant_of(title), expected, title)
            self.assertEqual(classify_title(title)["variant"], expected)

    def test_zero_padded_set_codes_are_the_same_set(self):
        self.assertEqual(classify_title("Iron Valiant ex 213/187 SV08A Indonesia")["set"], "SV8A")
        self.assertEqual(classify_title("Pikachu ex MA03 234/193")["set"], "MA3")
        self.assertEqual(classify_title("Zekrom N MA3 210/193")["set"], "MA3")
        self.assertEqual(classify_title("Charizard ex SV10 sv08a")["set"], "SV10")            # a real 10 keeps its zero

    def test_secret_rarities_are_read(self):
        self.assertEqual(classify_title("Mega Dragonite ex MUR 250/193 MA3")["rarity"], ["MUR"])
        self.assertEqual(classify_title("Pikachu CHR 206/205 AC3A")["rarity"], ["CHR"])

    def test_ids_extracted(self):
        r = classify_title("Pikachu ex SAR 234/193 MA3 ID NM")
        self.assertEqual((r["number"], r["set"], r["condition"]), ("234/193", "MA3", "nm"))


GUESS_NAMES = [  # real toco titles (VPS run, 2026-10-04) -> the card name a human reads
    ("Kartu Pokemon Fezandipiti ex 114/193 RR Evolusi Mega Impian ex MA3 Indonesia", "Fezandipiti ex"),
    ("Mega Gardevoir ex (RR) - 060/126 - Mega Evolusi (MA1) - Pokemon TCG Indonesia", "Mega Gardevoir ex"),
    ("MEGA EMBOAR EX RR MA4 I 028/123 LEDAKAN PENIADA", "Mega Emboar ex"),
    ("Archaludon ex MA3 | 122/193 RR - Pokemon TCG Indonesia", "Archaludon ex"),
    ("Kartu Pokemon Mega Mawile ex 094/126 RR Evolusi Mega MA1 Indonesia", "Mega Mawile ex"),
    ("Salazzle 128/123 AR | MA4 Ledakan Peniada | Pokemon TCG Indonesia", "Salazzle"),
    ("Ampharos MA5 | 173/164 AR - Pokemon TCG Indonesia", "Ampharos"),
    ("Pokemon TCG Indonesian Ho-Oh Ethan ex MA3 021/193 RR Evolusi Mega Impian ex", "Ho-Oh Ethan ex"),
    ("Drakloak AR MA3 212/193 POKEMON TCG INDONESA", "Drakloak"),
    ("pokemon (ID) mega abomasnow ex - MA1 036/126 - RR", "Mega Abomasnow ex"),
    ("Zoroark ex (RR) - 112/193 - Impian Mega ex (MA3) - Pokemon TCG Indonesia", "Zoroark ex"),
    ("SCORBUNNY AR MA4 I 129/123 LEDAKAN PENIADA", "Scorbunny"),
    ("TANGELA ERIKA AR MA4 I 124 / 123 INDONESIA", "Tangela Erika"),
    ("kartu pokemon bahasa indonesia mega impian ma3 zekrom N", "Zekrom N"),
    ("Mega Charizard X Ex MA ma3 kartu pokemon tcg indo", "Mega Charizard X ex"),
    ("Energi Tim Roket C MA3 193/193 Pokemon TCG Indonesia", "Energi Tim Roket"),
    ("Sinyal Mega U 158/193 MA3 Indo", "Sinyal Mega"),
    ("Reshiram N AR MA3 128/193 Indonesia", "Reshiram N"),
    ("Pikachu ex SAR 155/130 MA6 30th Anniversary Indonesia", "Pikachu ex"),
    ("Mega Dragonite ex MUR 250/193 MA3 Special Indonesia", "Mega Dragonite ex"),
]


class GuessName(unittest.TestCase):
    def test_real_titles(self):
        from classify import guess_name
        for title, name in GUESS_NAMES:
            self.assertEqual(guess_name(title), name, title)


class Match(unittest.TestCase):
    TYPES = [
        {"name": "Pikachu ex", "set": "SV8s", "number": "211/182", "rarity": "SR"},
        {"name": "Pikachu ex", "set": "MA3", "number": "234/193", "rarity": "SAR"},
        {"name": "Mega Charizard X ex", "set": "MA3", "number": "223/193", "rarity": "MA"},
    ]

    def idx(self, title):
        m = build_site.match_type(title, self.TYPES)
        return m and (m[0], m[1])

    def test_number_wins(self):
        self.assertEqual(self.idx("Kartu Pikachu EX SR 211/182 EGS"), (0, "nomor"))

    def test_name_and_set_without_rarity_conflict(self):
        self.assertEqual(self.idx("Pikachu ex SAR MA3 ID NM"), (1, "nama+set"))

    def test_rarity_conflict_is_uncertain(self):
        self.assertEqual(self.idx("MEGA CHARIZARD X EX SR graded"), (2, "nama"))

    def test_unrelated(self):
        self.assertIsNone(self.idx("Gengar VMAX CSR"))


class RealTitles(unittest.TestCase):
    """Titles copied from toco search results on the VPS (2026-10-03), checked against the 6 tracked cards."""
    TYPES = [
        {"name": "Mega Venusaur ex", "set": "MA1", "number": "173/126", "rarity": "SAR"},
        {"name": "Pikachu ex", "set": "SV8s", "number": "211/182", "rarity": "SR"},
        {"name": "Pikachu ex", "set": "MA3", "number": "234/193", "rarity": "SAR"},
        {"name": "Mega Charizard X ex", "set": "MA3", "number": "223/193", "rarity": "MA"},
        {"name": "Zekrom N", "set": "MA3", "number": "210/193", "rarity": "AR"},
        {"name": "Mega Dragonite ex", "set": "MA3", "number": "232/193", "rarity": "MA"},
    ]
    CASES = [  # title, kind, text language, (tracked card number 1-6, match confidence)
        ("EGS 10 Mega Venusaur ex SAR #173 ma1 Pokemon Evolusi Mega [ID]", "slab", "ID", (1, "nama+set")),
        ("mega venusaur ex 003/132 MEG en pokemon tcg english mega evolution", "raw", "foreign", (1, "nama")),
        ("Mega Venusaur ex M1L | 003/063 RR - Pokemon TCG Japanese", "raw", "foreign", (1, "nama")),
        ("pokemon (ID) mega venusaur ex - MA1 003/126 - RR", "raw", "ID", (1, "nama")),
        ("Mega Venusaur Ex RR Jepang", "raw", "foreign", (1, "nama")),
        ("Pikachu ex RR 044/193 MEGA Dream ex M2a Pokemon Card Japanese NM", "raw", "foreign", (2, "nama")),
        ("Pikachu EX SAR MA3 kartu pokemon tcg indo", "raw", "ID", (3, "nama+set")),
        ("Kartu Pokemon Zekrom N 210/193 AR Evolusi Mega Impian ex MA3 Indonesia", "raw", "ID", (5, "nomor")),
        ("N's Zekrom AR 210/193 MEGA Dream ex M2a Pokemon Card Japanese Near Mint", "raw", "foreign", (5, "nomor")),
        ("Pokemon TCG Indonesian Mega Dragonite ex MA3 232/193 MA Evolusi Mega Impian ex", "raw", "ID", (6, "nomor")),
        ("Mega Dragonite Ex MA3 232/193 egs 10 celar slab not tag Grading Slab Original", "slab", "unknown", (6, "nomor")),
        ("Pikachu ex SAR INDO", "raw", "ID", (2, "nama")),
        ("Pokemon Mega Charizard X Ex 223/193 - MA Mega Attack Rare - JP Japan Jepang - m2a Mega Dream ex",
         "raw", "foreign", (4, "nomor")),
    ]

    def test_kind_language_and_match(self):
        from classify import text_language
        for title, kind, lang, match in self.CASES:
            m = build_site.match_type(title, self.TYPES)
            got = (classify_title(title)["kind"], text_language(title), (m[0] + 1, m[1]) if m else None)
            self.assertEqual(got, (kind, lang, match), title)

    def test_a_different_card_number_is_a_different_card(self):
        self.assertTrue(build_site.number_conflict("Mega Venusaur ex MA1 003/126", "173/126"))
        self.assertTrue(build_site.number_conflict("Mega Venusaur ex #003 ma1", "173/126"))
        self.assertFalse(build_site.number_conflict("Mega Venusaur ex #173 ma1", "173/126"))
        self.assertFalse(build_site.number_conflict("Mega Venusaur ex SAR ma1", "173/126"))


def good_raw():
    s = {"n": 3, "min": 80000, "p25": 90000, "median": 100000, "p75": 120000, "max": 150000}
    ls = [{"url": f"https://toco.id/id/listing/a-{i}-1790000000000-ab12", "ask_idr": p, "city": "Kota Jambi",
           "observed_at": "2026-10-02T22:30:00+00:00", "conf": "nomor",
           "lang": "ID", "lang_by": "image", "title": "t"}
          for i, p in enumerate((80000, 100000, 150000))]
    return {"schema_version": 1, "kind": "RAW", "price_type": "OBSERVED_ASKING_ONLY", "language": "ID",
            "source": "toco_public", "generated_at": "2026-10-03T00:10:00+00:00",
            "coverage": {"cards": 1, "listings_counted": 3, "last_observed_at": "2026-10-02T22:30:00+00:00"},
            "cards": [{"name": "Mega Venusaur ex", "set_code": "MA1", "card_number": "173/126",
                       "rarity": "SAR", "stats": s, "listings": ls}],
            "unmatched": {"count": 0}}


class RawFormat(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_raw.validate(good_raw())[0], [])

    def test_catches_problems(self):
        d = good_raw()
        d["cards"][0]["stats"]["median"] = 1
        d["cards"][0]["listings"][0]["conf"] = "nama"
        d["coverage"]["cards"] = 5
        self.assertEqual(len(validate_raw.validate(d)[0]), 3)

    def test_history_and_photo_rules(self):
        d = good_raw()
        d["cards"][0]["history"] = [{"date_wib": "2026-10-03", "n": 3, "median": 100000, "min": 80000},
                                    {"date_wib": "2026-10-02", "n": 3, "median": 100000, "min": 80000}]
        d["cards"][0]["listings"][0]["photo"] = "ftp://x/y.jpg"
        errors = validate_raw.validate(d)[0]
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("history" in e for e in errors) and any("photo" in e for e in errors))
        d["cards"][0]["history"] = [{"date_wib": "2026-10-02", "n": 3, "median": 100000, "min": 80000},
                                    {"date_wib": "2026-10-03", "n": 3, "median": 101000, "min": 80000}]
        d["cards"][0]["listings"][0]["photo"] = "https://img.example/a.webp"
        self.assertEqual(validate_raw.validate(d)[0], [])

    def test_catalog_cards_use_their_own_conf_and_need_no_five_listings(self):
        d = good_raw()
        card = d["cards"][0]
        card["catalog"] = True
        self.assertEqual(len(validate_raw.validate(d)[0]), 3)  # tracked-style conf on a catalog card
        for l in card["listings"]:
            l["conf"] = "katalog"
        d["coverage"]["catalog_cards"] = 1
        errors, warnings = validate_raw.validate(d)
        self.assertEqual((errors, warnings), ([], []))
        d["coverage"]["catalog_cards"] = 0
        self.assertTrue(any("catalog_cards" in e for e in validate_raw.validate(d)[0]))
        d["cards"][0]["catalog"] = False
        d["cards"][0]["listings"][0]["conf"] = "katalog"
        self.assertTrue(any("tracked card" in e for e in validate_raw.validate(d)[0]))

    def test_variant_rules(self):
        d = good_raw()
        import copy
        v = copy.deepcopy(d["cards"][0])
        v.update(catalog=True, variant="master_ball")
        for l in v["listings"]:
            l["conf"] = "katalog"
            l["url"] += "-m"
        d["cards"].append(v)
        d["coverage"].update(cards=2, listings_counted=6, catalog_cards=1)
        self.assertEqual(validate_raw.validate(d)[0], [])                       # same number and set as the tracked card, another version
        v["variant"] = "gold_ball"
        self.assertTrue(any("variant" in e for e in validate_raw.validate(d)[0]))
        v["variant"] = "master_ball"
        d["cards"].append(copy.deepcopy(v))
        d["coverage"].update(cards=3)
        self.assertTrue(any("duplicate card" in e for e in validate_raw.validate(d)[0]))

    def test_default_priced_cards(self):
        d = good_raw()
        base = {"name": "Foo", "set_code": "MA3", "card_number": "115/193", "rarity": "C", "catalog": True, "price_basis": "default",
                "stats": {"n": 0, "min": 500, "p25": 500, "median": 500, "p75": 500, "max": 500}, "listings": []}
        d["cards"].append(base)
        d["coverage"].update(cards=2, default_priced_cards=1, catalog_cards=1)
        errors, warnings = validate_raw.validate(d)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)                                      # only the tracked card's; none for the base price
        self.assertIn("Mega Venusaur", warnings[0])
        base["stats"]["n"] = 2
        base["stats"]["max"] = 600
        errors = validate_raw.validate(d)[0]
        self.assertTrue(any("default-priced card must be" in e for e in errors) and any("one price" in e for e in errors))
        base["stats"].update(n=0, max=500)
        d["coverage"]["default_priced_cards"] = 0
        self.assertTrue(any("default_priced_cards" in e for e in validate_raw.validate(d)[0]))
        d["coverage"]["default_priced_cards"] = 1
        base["price_basis"] = "guess"
        self.assertTrue(any("price_basis" in e for e in validate_raw.validate(d)[0]))

    def test_non_indonesian_listing_is_rejected(self):
        d = good_raw()
        d["cards"][0]["listings"][0]["lang"] = "JP"
        d["cards"][0]["listings"][1]["lang_by"] = "guess"
        errors = validate_raw.validate(d)[0]
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("Indonesian" in e for e in errors))

    def test_listing_outside_stats(self):
        d = good_raw()
        d["cards"][0]["listings"][2]["ask_idr"] = 999999
        self.assertTrue(any("outside" in e for e in validate_raw.validate(d)[0]))


def rates_file(jpy, usd, source="google", fetched="2026-10-06T07:00:00+00:00"):
    """rates.json as tools/fetch_rates.py writes it."""
    return {"schema_version": 1, "generated_at": fetched, "rates": {
        "JPY": {"idr": jpy, "source": source, "fetched_at": fetched, "google": jpy, "ecb": jpy},
        "USD": {"idr": usd, "source": source, "fetched_at": fetched, "google": usd, "ecb": usd}}}


def jp_card(code, number, name, yen, id=1, in_stock=True):
    """A Yuyu-tei card as tools/fetch_jp_prices.py writes it (name with the bracket as on the site)."""
    base, tag, variant = fetch_jp_prices.split_name(name)
    return {"set": code, "number": number, "rarity": "-", "name": base, "tag": tag, "variant": variant, "yen": yen, "in_stock": in_stock,
            "stock": None if in_stock else 0, "id": id}


def tiny_latest():
    row = {"observed_at": "2026-10-02T22:30:00+00:00", "asking_idr": 650000, "in_stock": True}
    return {
        "generated_at": "2026-10-03T02:10:00+00:00",
        "coverage": {"last_observed_at": "2026-10-02T22:30:00+00:00"},
        "discovery": {"generated_at": "2026-10-03T01:40:00+00:00", "candidates": []},
        "listings": [{"name": "Mega Venusaur ex", "set_code": "MA1", "card_number": "173/126", "rarity": "SAR",
                      "grade": "10", "url": "https://toco.id/id/listing/x-1790389583180-2c05",
                      "latest": row, "previous_day": row, "change_from_previous_day_idr": 0,
                      "daily_latest": [{"date_wib": "2026-10-03", **row}]}],
    }


def board_with_official_catalog():
    """raw.json (the tracked MA1 card, one listed MA3 card, one base-priced MA3 common) + the official catalog of MA3."""
    import copy
    raw = good_raw()
    listed = copy.deepcopy(raw["cards"][0])
    listed.update(name="Fezandipiti ex", set_code="MA3", card_number="114/193", rarity="RR", catalog=True)
    for l in listed["listings"]:
        l["conf"] = "katalog"
        l["url"] += "-114"
        l["photo"] = "https://seller.example/114.webp"
    base = {"name": "Foo", "set_code": "MA3", "card_number": "115/193", "rarity": "C", "catalog": True, "price_basis": "default",
            "stats": {"n": 0, "min": 500, "p25": 500, "median": 500, "p75": 500, "max": 500}, "listings": []}
    raw["cards"] += [listed, base]
    raw["coverage"].update(cards=3, listings_counted=6, catalog_cards=2, default_priced_cards=1)
    cards = [{"set": "MA3", "number": f"{n:03d}/193", "num": n, "den": 193, "name": name, "rarity": rar, "image": f"https://official.example/{n}.png", "id": n}
             for n, name, rar in ((114, "Fezandipiti ex", "RR"), (115, "Foo", "C"), (117, "Rare Thing", "SAR"), (118, "Other Rare", "SR"))]
    catalog = {"schema_version": 1, "generated_at": "2026-10-04T12:00:00+00:00", "cards": cards,
               "sets": [{"code": "MA3", "series": "Evolusi Mega", "name": "Evolusi Mega Impian ex", "product": "High Class Booster Pack", "released": "2026-01-30",
                         "total": 4, "fetched": 4, "complete": True}]}
    return raw, catalog


class Board(unittest.TestCase):
    def test_raw_joins_on_set_and_number(self):
        raw = good_raw()
        raw["cards"].append({**raw["cards"][0], "name": "Gengar ex", "set_code": "MA1", "card_number": "150/126"})
        data = build_site.build(tiny_latest(), raw)
        self.assertEqual(data["types"][0]["raw"]["median"], 100000)
        self.assertEqual([c["name"] for c in data["raw_only"]], ["Gengar ex"])

    def test_trend_compares_with_about_a_week_earlier(self):
        h = lambda d, m: {"date_wib": d, "n": 5, "median": m, "min": m}
        self.assertIsNone(build_site.trend_of([h("2026-10-10", 100)]))
        self.assertEqual(build_site.trend_of([h("2026-10-03", 100), h("2026-10-06", 110), h("2026-10-10", 120)]),
                         {"days": 7, "pct": 20.0})   # baseline is the 3 Oct entry, not the 6 Oct one
        self.assertEqual(build_site.trend_of([h("2026-10-08", 200), h("2026-10-10", 150)]), {"days": 2, "pct": -25.0})

    def test_official_catalog_adds_every_card_of_a_set_priced_base_priced_or_not(self):
        raw, catalog = board_with_official_catalog()
        data = build_site.build(tiny_latest(), raw, catalog)
        rows = {(c["set"], c["number"]): c for c in data["raw_cards"]}
        self.assertEqual(sorted(k for k in rows if k[0] == "MA3"), [("MA3", "114/193"), ("MA3", "115/193"), ("MA3", "117/193"), ("MA3", "118/193")])
        self.assertEqual([rows[("MA3", n)]["basis"] for n in ("114/193", "115/193", "117/193", "118/193")], ["listing", "default", None, None])
        self.assertEqual((rows[("MA3", "115/193")]["median"], rows[("MA3", "117/193")]["median"]), (500, None))
        self.assertEqual(rows[("MA3", "114/193")]["image"], "https://official.example/114.png")
        self.assertEqual(rows[("MA3", "114/193")]["photo"], "https://seller.example/114.webp")
        ma3 = next(m for m in data["sets"] if m["code"] == "MA3")
        self.assertEqual((ma3["name_id"], ma3["released"], ma3["total"], ma3["cards"], ma3["priced"], ma3["base_priced"], ma3["unpriced"], ma3["basis"], ma3["official"]),
                         ("Evolusi Mega Impian ex", "2026-01-30", 4, 4, 1, 1, 2, "resmi", True))
        self.assertEqual([c["number"] for c in data["raw_cards"] if c["set"] == "MA3"], ["114/193", "115/193", "117/193", "118/193"])

    def test_foil_versions_are_cards_of_their_own_and_deck_sets_are_listed_unpriced(self):
        raw, catalog = board_with_official_catalog()
        dup = dict(catalog["cards"][0], id=999, image="https://official.example/999.png")      # the Poke Ball holo version of 114/193
        deck = {"set": "AC3D", "number": "001/180", "num": 1, "den": 180, "name": "Deck card", "rarity": None, "image": "x", "id": 1000}
        catalog["cards"] += [dup, deck]
        catalog["sets"][0]["total"] = 5
        catalog["sets"].append({"code": "AC3D", "product": "Starter Deck GX", "released": "2020-07-10", "total": 1, "fetched": 1, "name": "Deck"})
        data = build_site.build(tiny_latest(), raw, catalog)
        ma3 = next(m for m in data["sets"] if m["code"] == "MA3")
        self.assertEqual((ma3["total"], ma3["cards"]), (5, 5))                                   # 4 numbers, one of them in two versions
        rows = [(c["number"], c["variant"], c["image"]) for c in data["raw_cards"] if c["set"] == "MA3"]
        self.assertEqual(rows[:2], [("114/193", "", "https://official.example/114.png"), ("114/193", "poke_ball", "https://official.example/999.png")])
        deck = next(m for m in data["sets"] if m["code"] == "AC3D")                              # decks are on the board with their cards and pictures ...
        self.assertEqual((deck["cards"], deck["priced"], deck["base_priced"], deck["unpriced"], deck["family"]), (1, 0, 0, 1, "AC"))
        deck_row = next(c for c in data["raw_cards"] if c["set"] == "AC3D")
        self.assertEqual((deck_row["basis"], deck_row["image"]), (None, "x"))                    # ... but with no price of their own until a listing exists

    def test_a_set_finds_its_family_by_the_series_the_official_site_names_and_sets_are_ordered_by_release(self):
        for code, series, family in (("S12A", "Pedang & Perisai", "SS"), ("SC1A", "pedang & perisai", "SS"), ("AS1A", "Matahari & Bulan", "SM"), ("AC3D", "Matahari & Bulan", "SM"),
                                     ("MATL", "Evolusi Mega", "MA"), ("MATL", None, "MA"), ("ZZ9", "Seri Baru", "AC"), ("SV9S", None, "SV")):
            self.assertEqual(build_site.set_family(code, {}, series), family, code)
        self.assertEqual(build_site.set_family("AC3A", {"AC3A": {"family": "SM"}}, "Lainnya"), "SM")                      # tools/sets.json wins
        order = {"SS": 0}
        keys = {c: build_site.set_sort_key({"code": c, "family": "SS", "released": r}, order)
                for c, r in (("S12", "2022-11-25"), ("S12A", "2022-12-23"), ("S11", "2022-09-23"), ("S", None))}
        self.assertEqual(sorted(keys, key=keys.get), ["S12A", "S12", "S11", "S"])                                          # newest first although S12 and S12A share a number; undated last
        families = [f["id"] for f in build_site.load_sets()["families"]]
        self.assertEqual(families, ["MA", "SV", "SS", "SM", "AC"])

    def test_each_set_knows_its_dearest_card_and_unknown_codes_find_their_family_by_prefix(self):
        import copy
        raw, catalog = board_with_official_catalog()
        listed = next(c for c in raw["cards"] if c.get("card_number") == "114/193")
        dear = copy.deepcopy(listed)
        dear.update(name="Other Rare", card_number="118/193", rarity="SR", stats={"n": 3, "min": 1800000, "p25": 1900000, "median": 2000000, "p75": 2100000, "max": 2200000})
        for l in dear["listings"]:
            l["url"] += "-118"
        raw["cards"].append(dear)
        catalog["sets"].append({"code": "MATL", "product": "Promo", "released": "2026-02-01", "total": 1, "fetched": 1, "name": "Promo MEGA"})
        catalog["cards"].append({"set": "MATL", "number": "001/001", "num": 1, "den": 1, "name": "Promo Card", "rarity": None, "image": "https://official.example/p.png", "id": 900})
        data = build_site.build(tiny_latest(), raw, catalog)
        ma3 = next(m for m in data["sets"] if m["code"] == "MA3")
        self.assertEqual((ma3["top_price"], ma3["top_card"]), (2000000, "Other Rare 118/193 SR"))              # the median of the dearest card, with its name
        promo = next(m for m in data["sets"] if m["code"] == "MATL")
        self.assertEqual((promo["top_price"], promo["top_card"], promo["family"], promo["unpriced"]), (None, None, "MA", 1))   # no price at all: no top price, never 0
        for code, family in (("MATL", "MA"), ("MAT", "MA"), ("SVDS", "SV"), ("SV11S", "SV"), ("AC3D", "AC"), ("S12A", "AC"), ("AS1A", "AC")):
            self.assertEqual(build_site.set_family(code, {}), family, code)

    def test_the_board_counts_outliers_and_puts_the_holo_version_after_the_named_patterns(self):
        import copy
        raw, catalog = board_with_official_catalog()
        listed = next(c for c in raw["cards"] if c.get("card_number") == "114/193")
        listed["outliers"] = [dict(copy.deepcopy(listed["listings"][0]), ask_idr=999999, url="https://toco.id/listing/out-1")]
        holo = copy.deepcopy(listed)
        holo.update(variant="holo", outliers=[])
        for l in holo["listings"]:
            l["url"] += "-holo"
        raw["cards"].append(holo)
        raw["coverage"].update(cards=len(raw["cards"]), listings_counted=sum(c["stats"]["n"] for c in raw["cards"]))
        data = build_site.build(tiny_latest(), raw, catalog)
        rows = [(c["number"], c["variant"], c["outliers"]) for c in data["raw_cards"] if c["set"] == "MA3" and c["number"] == "114/193"]
        self.assertEqual(rows, [("114/193", "", 1), ("114/193", "holo", 0)])

    def test_card_keys_ignore_zero_padding(self):
        self.assertEqual(build_site.card_key("SV1S", "014/078"), build_site.card_key("sv1s", "14/78"))
        self.assertEqual(build_site.card_key("MA6", "5 / 30"), ("ma6", "5/30", ""))
        self.assertNotEqual(build_site.card_key("MA3", "1/193"), build_site.card_key("MA3", "1/193", "master_ball"))

    def test_without_a_catalog_every_row_is_priced_from_listings(self):
        data = build_site.build(tiny_latest(), good_raw())
        self.assertEqual({c["basis"] for c in data["raw_cards"]}, {"listing"})
        self.assertTrue(all(m["unpriced"] == 0 and m["base_priced"] == 0 for m in data["sets"]))

    def test_photos_only_in_a_standalone_page(self):
        data = build_site.build(tiny_latest(), good_raw())
        self.assertIn('"photos": true', build_site.render(data, True))
        self.assertIn('"photos": false', build_site.render(data, False))

    def test_raw_only_rows_carry_the_cheapest_listing_link_and_photo(self):
        raw = good_raw()
        raw["cards"][0].update(set_code="MA3", card_number="1/193", catalog=True)
        for l in raw["cards"][0]["listings"]:
            l["conf"] = "katalog"
        raw["cards"][0]["listings"][0]["photo"] = "https://img.example/cheap.webp"
        row = build_site.build(tiny_latest(), raw)["raw_only"][0]
        self.assertEqual((row["url"], row["photo"]), (raw["cards"][0]["listings"][0]["url"], "https://img.example/cheap.webp"))

    def test_without_raw(self):
        data = build_site.build(tiny_latest())
        self.assertNotIn("raw", data["types"][0])
        self.assertIsNone(data["raw_meta"])

    def test_queries_per_type(self):
        q = watch_queries.queries({"name": "Zekrom <N>", "set": "MA3", "number": "210/193"})
        self.assertEqual(q["egs"][0], "Zekrom N 210/193 egs")
        self.assertIn("210/193 MA3", q["raw"])


# The toco page layout is not known; this synthetic page only has the shape the parser relies on:
# links to /listing/ whose text is "Sisa N <title> Rp<price> ... <city>", with an <img> inside.
def fake_page():
    cards = [
        ("pikachu-id-1", "id-1", "Sisa 1 Pikachu ex SAR 234/193 MA3 ID NM Rp420.000 Kota Jambi"),
        ("pikachu-id-2", "id-2", "Sisa 2 Pikachu ex SAR 234/193 MA3 Indo Rp380.000 Kota Surabaya"),
        ("pikachu-jp-3", "jp-3", "Sisa 1 Pikachu ex SAR 234/193 MA3 Rp300.000 Kota Bandung"),
        ("box-4", "id-4", "Sisa 5 Booster Box Pokemon Mega Impian MA3 Indonesia Rp900.000 Kota Jambi"),
        ("slab-5", "id-5", "Sisa 1 Pikachu ex SAR 234/193 MA3 EGS 10 Rp2.760.000 Kota Jakarta"),
        ("gengar-6", "id-6", "Sisa 1 Gengar VMAX CSR Rp150.000 Kota Jambi"),
        ("charizard-7", "id-7", "Sisa 1 MEGA CHARIZARD X EX SR Rp600.000 Kota Depok"),
    ]
    return "<html><body>" + "".join(
        f'<a href="/id/listing/{slug}-1790000000{i}00-ab1{i}"><img src="/img/{img}.jpg">'
        f"<span>{text}</span></a>" for i, (slug, img, text) in enumerate(cards)) + "</body></html>"


def api_item(slug, title, price, stock=1, special=None, description="", photo=None, **extra):
    return {"slug": slug, "title": title, "price": price, "price_special": special, "stock": stock,
            "description": description, "city": "Kota Jambi", "primary_photo": photo or f"https://img.example/{slug}.webp",
            "photos": [], "show": True, "archived": False, "deleted": False, **extra}


API_CATALOG = [
    api_item("a-1", "Pikachu EX SAR MA3 kartu pokemon tcg indo", 420000, photo="https://img.example/id-1.webp"),
    api_item("a-2", "Pikachu ex SAR 234/193 MA3 Indo", 400000, special=380000, photo="https://img.example/id-2.webp"),
    api_item("a-3", "Pikachu ex SAR 234/193 MA3 NM", 300000, photo="https://img.example/jp-3.webp"),  # Japanese only in the photo
    api_item("a-4", "Booster Box Pokemon Mega Impian MA3 Indonesia", 900000, stock=5),
    api_item("a-5", "Pikachu ex SAR 234/193 MA3 EGS 10", 2760000),
    api_item("a-6", "Gengar VMAX CSR", 150000),
    api_item("a-7", "MEGA CHARIZARD X EX SR", 600000),
    api_item("a-8", "Pikachu ex SAR 234/193 MA3 Japanese", 250000),  # foreign by text: no photo needed
    api_item("a-9", "Pikachu ex 234/193 MA3 ID", 350000, stock=0),  # sold out
]


CATALOG_EXTRA = [  # cards we do not track; the card is known by the set and number in the title
    api_item("k-1", "Kartu Pokemon Fezandipiti ex 114/193 RR Evolusi Mega Impian ex MA3 Indonesia", 15000),
    api_item("k-2", "Fezandipiti ex MA3 | 114/193 RR - Pokemon TCG Indonesia", 18000),
    api_item("k-3", "Fezandipiti ex 114/193 RR Pokemon TCG Indo", 12000),                 # no set code: 193 means MA3
    api_item("k-4", "Salazzle 128/123 AR | MA4 Ledakan Peniada | Pokemon TCG Indonesia", 50000),
    api_item("k-5", "Gardevoir ex MA3 | 71/193 RR Indonesia", 9000),
    api_item("k-6", "Mega Venusaur ex 003/132 MEG en pokemon tcg english mega evolution", 30000),  # foreign by text
    api_item("k-7", "Mega Absol ex MA1 086/126 RR", 8000),                                  # language only in the photo
    api_item("k-8", "Mega Absol ex MA1 086/126 RR NM", 9500, photo="https://img.example/jp-8.webp"),  # Japanese photo
    api_item("k-9", "Pokemon Pikachu ex SAR 234/193 MA3 Indo", 1100000, stock=0),            # sold out
]


class Collector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        latest = tiny_latest()
        latest["listings"] += [
            {**latest["listings"][0], "name": "Pikachu ex", "set_code": "MA3", "card_number": "234/193",
             "rarity": "SAR", "url": "https://toco.id/id/listing/s-1790389583181-2c06"},
            {**latest["listings"][0], "name": "Mega Charizard X ex", "set_code": "MA3", "card_number": "223/193",
             "rarity": "MA", "url": "https://toco.id/id/listing/c-1790389583182-2c07"}]
        self.latest = os.path.join(self.tmp.name, "latest.json")
        with open(self.latest, "w") as fh:
            json.dump(latest, fh)
        self.cfg = argparse.Namespace(
            latest=self.latest, out=os.path.join(self.tmp.name, "raw.json"), db=os.path.join(self.tmp.name, "x.sqlite"),
            pages=5, size=45, delay=0, queries_limit=2, max_checks=50, max_age_hours=36, dry_run=False,
            save_json=None, sample_check="Pikachu ex 234/193", sample_size=3)
        self.catalog = API_CATALOG
        self.posts, self.image_calls, self.api_calls = [], 0, 0
        self.photo_error = False

    def tearDown(self):
        self.tmp.cleanup()

    def post_fn(self, body):
        self.posts.append(body)
        inner = body["body"]
        off, size = inner["from"], inner["size"]
        return {"results": self.catalog[off:off + size], "total": len(self.catalog)}

    def image_fn(self, url):
        self.image_calls += 1
        if self.photo_error:
            raise collect_raw.FetchError("403")
        return b"\xff\xd8\xff" + url.encode(), "image/jpeg"

    def classify_fn(self, data, media):
        self.api_calls += 1
        return "JP" if b"/jp-" in data else "ID"

    def go(self, now=None, **over):
        for k, v in over.items():
            setattr(self.cfg, k, v)
        return collect_raw.run(self.cfg, self.post_fn, self.image_fn, None if self.cfg.dry_run else self.classify_fn,
                               now=now or datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc), sleep=lambda s: None,
                               log=lambda *a: None)

    def test_search_body_matches_what_the_site_sends(self):
        b = collect_raw.search_body("Pikachu ex 234/193", 45, 45)
        self.assertEqual(b["path"], "/listing-search")
        self.assertEqual((b["body"]["from"], b["body"]["size"], b["body"]["query"]), (45, 45, "Pikachu ex 234/193"))
        self.assertEqual(b["body"]["filters"], {"web": True, "add_to_cart": True, "special_daytop": False, "safe_search": True})

    def test_listing_uses_the_discounted_price_and_marks_unavailable_items(self):
        l = collect_raw.to_listing(API_CATALOG[1])
        self.assertEqual((l["ask"], l["list"], l["stock"], l["url"]), (380000, 400000, 1, "https://toco.id/listing/a-2"))
        self.assertEqual(l["image"], "https://img.example/id-2.webp")
        self.assertEqual(collect_raw.to_listing(api_item("x", "t", 1, archived=True))["stock"], 0)
        self.assertEqual(collect_raw.to_listing(api_item("x", "t", 1, show=False))["stock"], 0)
        self.assertIsNone(collect_raw.to_listing(API_CATALOG[0])["list"])

    def test_crawl_pages_with_from_and_size(self):
        items, diag = collect_raw.crawl(["q"], self.post_fn, 5, 4, 0, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual((len(items), diag[0]["pages"], diag[0]["total"]), (9, 3, 9))
        self.assertEqual([p["body"]["from"] for p in self.posts], [0, 4, 8])

    def test_full_run_counts_only_indonesian_raw_cards(self):
        raw, summary = self.go()
        self.assertEqual(validate_raw.validate(raw)[0], [])
        card = raw["cards"][0]
        self.assertEqual((card["name"], card["stats"]["n"], card["stats"]["min"], card["stats"]["max"]),
                         ("Pikachu ex", 2, 380000, 420000))
        self.assertEqual(card["stats"]["median"], 400000)
        self.assertTrue(all(l["lang"] == "ID" and l["lang_by"] == "image" for l in card["listings"]))
        self.assertEqual(raw["unmatched"], {"count": 2, "language_unverified": 0})
        self.assertEqual((summary["jenis_product"], summary["jenis_slab"]), (1, 1))
        self.assertEqual(self.api_calls, 3)  # a-1, a-2, a-3; the Japanese-by-text and the sold-out ones cost nothing

    def test_listings_carry_their_photo(self):
        raw, _ = self.go()
        photos = {l["photo"] for l in raw["cards"][0]["listings"]}
        self.assertEqual(photos, {"https://img.example/id-1.webp", "https://img.example/id-2.webp"})

    def test_history_gets_one_point_per_wib_day_and_a_trend(self):
        day1 = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)     # 3 Oct 08:00 WIB
        raw1, _ = self.go(now=day1)
        self.assertEqual([h["date_wib"] for h in raw1["cards"][0]["history"]], ["2026-10-03"])
        self.catalog = [api_item("a-1", "Pikachu EX SAR MA3 kartu pokemon tcg indo", 462000, photo="https://img.example/id-1.webp"),
                        api_item("a-2", "Pikachu ex SAR 234/193 MA3 Indo", 440000, special=418000, photo="https://img.example/id-2.webp")]
        self.go(now=datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc))        # +7 days, prices +10%
        self.catalog = [api_item("a-1", "Pikachu EX SAR MA3 kartu pokemon tcg indo", 484000, photo="https://img.example/id-1.webp"),
                        api_item("a-2", "Pikachu ex SAR 234/193 MA3 Indo", 440000, special=418000, photo="https://img.example/id-2.webp")]
        raw3, _ = self.go(now=datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc))  # same WIB day: replaces, not appends
        hist = raw3["cards"][0]["history"]
        self.assertEqual([h["date_wib"] for h in hist], ["2026-10-03", "2026-10-10"])
        self.assertEqual((hist[0]["median"], hist[1]["median"]), (400000, 451000))
        self.assertEqual(validate_raw.validate(raw3)[0], [])
        self.assertEqual(build_site.trend_of(hist), {"days": 7, "pct": 12.8})

    def test_dry_run_records_no_history(self):
        self.go(dry_run=True)
        db = sqlite3.connect(self.cfg.db)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0], 0)
        finally:
            db.close()

    def test_language_is_cached_per_listing(self):
        self.go()
        first = self.api_calls
        self.go()
        self.assertEqual(self.api_calls, first)

    def test_dry_run_makes_no_photo_or_api_calls(self):
        raw, summary = self.go(dry_run=True)
        self.assertEqual((self.api_calls, self.image_calls), (0, 0))
        self.assertEqual(raw["cards"], [])
        self.assertEqual(raw["unmatched"]["language_unverified"], 3)

    def test_check_budget_limits_cost(self):
        raw, summary = self.go(max_checks=1)
        self.assertEqual(self.api_calls, 1)
        self.assertEqual(raw["unmatched"]["language_unverified"], 2)

    def test_photo_failure_falls_back_to_an_explicit_id_in_the_title(self):
        self.photo_error = True
        raw, summary = self.go()
        card = raw["cards"][0]
        self.assertEqual(card["stats"]["n"], 2)
        self.assertTrue(all(l["lang_by"] == "title" for l in card["listings"]))
        self.assertEqual(raw["unmatched"]["language_unverified"], 1)  # a-3 has no language in its text
        self.assertEqual(validate_raw.validate(raw)[0], [])

    def test_text_mode_needs_no_key_and_counts_only_listings_that_say_indonesian(self):
        raw, summary = self.go(lang_mode="text")
        self.assertEqual((self.api_calls, self.image_calls), (0, 0))
        card = raw["cards"][0]
        self.assertEqual(card["stats"]["n"], 2)  # a-1 "indo" and a-2 "Indo"; a-3 says nothing about language
        self.assertTrue(all(l["lang_by"] == "title" and l["lang"] == "ID" for l in card["listings"]))
        self.assertEqual(raw["unmatched"]["language_unverified"], 1)
        self.assertEqual(validate_raw.validate(raw)[0], [])

    def test_text_mode_result_is_replaced_by_a_photo_check_later(self):
        self.go(lang_mode="text")
        raw, _ = self.go(lang_mode="photo")  # title answers are rechecked after a day
        self.assertEqual(self.api_calls, 1)  # same day: only the listing text mode could not decide is checked
        raw, _ = self.go(lang_mode="photo", now=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(self.api_calls, 3)  # a day later the two title-based answers are checked against their photos
        self.assertTrue(all(l["lang_by"] == "image" for l in raw["cards"][0]["listings"]))

    def test_db_report_lists_counted_listings_and_other_card_numbers(self):
        self.go(lang_mode="text")
        db = sqlite3.connect(self.cfg.db)
        lines = []
        try:
            res = db_report.report(db, 48, 5, now=datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc), out=lines.append)
        finally:
            db.close()
        text = "\n".join(lines)
        self.assertEqual((res["counted"], res["listings"]), (2, 9))
        self.assertIn("MA3 234/193 | Rp380.000 | nomor", text)
        self.assertIn("== Belum bisa didata", text)

    def catalog_cards(self, raw):
        return {(c["set_code"], c["card_number"]): c for c in raw["cards"] if c.get("catalog")}

    def test_catalog_adds_untracked_cards_known_by_set_and_number(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        raw, summary = self.go(lang_mode="text", catalog=True)
        self.assertEqual(validate_raw.validate(raw)[0], [])
        cards = self.catalog_cards(raw)
        self.assertEqual(sorted(cards), [("MA3", "114/193"), ("MA3", "71/193"), ("MA4", "128/123")])
        fez = cards[("MA3", "114/193")]
        self.assertEqual((fez["name"], fez["rarity"], fez["stats"]["n"], fez["stats"]["min"], fez["stats"]["max"]),
                         ("Fezandipiti ex", "RR", 3, 12000, 18000))
        self.assertTrue(all(l["conf"] == "katalog" and l["lang_by"] == "title" for l in fez["listings"]))
        self.assertEqual(raw["coverage"]["catalog_cards"], 3)
        self.assertFalse(raw["cards"][0].get("catalog"))  # tracked cards come first
        self.assertEqual(summary["cocok_katalog"], 7)     # k-1..k-5 and k-7/k-8; k-6 shows no set code and its total is unknown

    def test_db_report_lists_catalog_cards_with_guessed_names(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        self.go(lang_mode="text", catalog=True)
        db = sqlite3.connect(self.cfg.db)
        lines = []
        try:
            res = db_report.report(db, 48, 5, now=datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc), out=lines.append)
        finally:
            db.close()
        self.assertEqual(res["catalog_cards"], 3)
        self.assertIn("MA3 114/193 | Fezandipiti ex | 3 | Rp12.000 - Rp15.000 - Rp18.000", "\n".join(lines))

    def test_catalog_off_keeps_only_tracked_cards(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        raw, _ = self.go(lang_mode="text", catalog=False)
        self.assertEqual(self.catalog_cards(raw), {})
        self.assertEqual(raw["coverage"]["catalog_cards"], 0)

    def test_catalog_listings_are_language_checked_from_the_photo(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        raw, _ = self.go(catalog=True)
        absol = self.catalog_cards(raw)[("MA1", "86/126")]
        self.assertEqual([(l["ask_idr"], l["lang_by"]) for l in absol["listings"]], [(8000, "image")])  # the JP photo is dropped
        self.assertEqual(validate_raw.validate(raw)[0], [])

    def test_tracked_listings_are_checked_before_catalog_ones_when_the_budget_is_short(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        raw, _ = self.go(catalog=True, max_checks=3)
        self.assertEqual(raw["cards"][0]["stats"]["n"], 2)  # the tracked Pikachu got the three checks
        self.assertEqual(self.catalog_cards(raw).get(("MA1", "86/126")), None)

    def test_sold_out_catalog_listing_is_not_counted(self):
        self.catalog = [CATALOG_EXTRA[-1]]
        raw, _ = self.go(lang_mode="text", catalog=True)
        self.assertEqual(raw["cards"], [])

    def test_set_is_learned_from_the_card_total_only_when_it_is_unambiguous(self):
        titles = ["A ex MA3 | 1/193 RR Indonesia", "B ex MA3 | 2/193 RR Indonesia", "C ex MA3 | 3/193 RR Indonesia",
                  "D ex M2a 4/193 Japanese", "E ex MA9 5/200 Indonesia", "F ex MA8 6/200 Indonesia"]
        self.assertEqual(collect_raw.denominator_sets(titles), {"193": "MA3"})
        self.assertEqual(collect_raw.catalog_key("Fez ex 114/193 RR", {"193": "MA3"}), ("MA3", "114/193"))
        self.assertIsNone(collect_raw.catalog_key("Fez ex 114/777 RR", {"193": "MA3"}))
        self.assertIsNone(collect_raw.catalog_key("Booster Box MA3 Indonesia", {}))

    def official_catalog(self):
        """A catalog.json as tools/fetch_catalog.py writes it: MA3 cards 114 (listed), 115 and 116 (common), 117 (SAR), 71 (listed)."""
        cards = [{"set": "MA3", "number": f"{n:03d}/193", "num": n, "den": 193, "name": name, "rarity": rarity, "image": f"https://o/{n}.png", "id": n}
                 for n, name, rarity in ((114, "Fezandipiti ex", "RR"), (115, "Foo", "C"), (116, "Bar", "C"), (117, "Rare Thing", "SAR"),
                                         (71, "Gardevoir ex Resmi", "RR"))]
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": [], "cards": cards}, fh)
        prices = os.path.join(self.tmp.name, "default_prices.json")
        with open(prices, "w", encoding="utf-8") as fh:
            json.dump({"by_rarity": {"C": 500}}, fh)
        return path, prices

    def test_common_cards_without_a_listing_get_the_base_price_and_listed_ones_keep_theirs(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA + [api_item("k-10", "Bar MA3 | 116/193 C Pokemon TCG Indonesia", 3000)]
        path, prices = self.official_catalog()
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=prices)
        self.assertEqual(validate_raw.validate(raw)[0], [])
        cards = {(c["set_code"], c["card_number"]): c for c in raw["cards"]}
        foo = cards[("MA3", "115/193")]
        self.assertEqual((foo["name"], foo["rarity"], foo["price_basis"], foo["stats"], foo["listings"]),
                         ("Foo", "C", "default", {"n": 0, "min": 500, "p25": 500, "median": 500, "p75": 500, "max": 500}, []))
        bar = cards[("MA3", "116/193")]                       # a listing exists: its price, not the base price
        self.assertEqual((bar["stats"]["median"], bar["stats"]["n"], "price_basis" in bar), (3000, 1, False))
        self.assertNotIn(("MA3", "117/193"), cards)           # a SAR nobody lists gets no invented price
        self.assertEqual(cards[("MA3", "71/193")]["name"], "Gardevoir ex Resmi")        # official name replaces the title guess
        self.assertEqual(cards[("MA3", "114/193")]["rarity"], "RR")
        self.assertEqual(raw["coverage"]["default_priced_cards"], 1)
        self.assertEqual(raw["coverage"]["cards"], len(raw["cards"]))
        self.assertTrue(all(c["stats"]["min"] > 0 for c in raw["cards"]))                 # nothing is priced at 0

    def test_foil_versions_are_separate_cards_and_only_the_regular_card_gets_the_base_price(self):
        def card(set_, n, den, name, rarity, cid):
            return {"set": set_, "number": f"{n:03d}/{den}", "num": n, "den": den, "name": name, "rarity": rarity, "image": f"https://o/{cid}.png", "id": cid}
        cards = [card("MA3", 1, 193, "Pinsir", None, 1), card("MA3", 1, 193, "Pinsir", None, 2), card("MA3", 1, 193, "Pinsir", None, 3),  # regular + two holo versions
                 card("MA3", 2, 193, "Yanma", None, 4), card("MA3", 2, 193, "Yanma", "C", 5),                                         # a later entry carries the rarity
                 card("MA3", 250, 193, "Mega Dragonite ex", None, 6),                                                                 # above the printed total: a secret card
                 card("MA6", 1, 130, "Exeggcute", None, 7), card("MA6", 5, 30, "Pikachu", None, 8),                                   # n/30 side series
                 card("MA6", 2, 130, "Volbeat", "AR", 9), card("MA6", 3, 130, "Illumise", None, 10),
                 card("AC3D", 1, 180, "Deck card", None, 11)]
        sets = [{"code": "MA3", "product": "High Class Booster Pack", "fetched": 6}, {"code": "MA6", "product": "Booster Pack", "fetched": 4},
                {"code": "AC3D", "product": "Starter Deck GX", "fetched": 1}]
        idx = build_site.official_index(cards, sets)
        self.assertEqual(sorted(idx), [("ma3", "1/193", ""), ("ma3", "1/193", "element"), ("ma3", "1/193", "poke_ball"), ("ma3", "2/193", ""),
                                       ("ma3", "2/193", "poke_ball"), ("ma3", "250/193", ""), ("ma6", "1/130", ""), ("ma6", "2/130", ""),
                                       ("ma6", "3/130", ""), ("ma6", "5/30", "")])
        self.assertEqual((idx[("ma3", "1/193", "poke_ball")]["id"], idx[("ma3", "1/193", "element")]["id"]), (2, 3))   # the site's order; sets.json names MA3's third one Elemen
        self.assertEqual(idx[("ma3", "2/193", "")]["rarity"], "C")                                                              # inherited from the later entry
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": sets, "cards": cards}, fh)
        prices = os.path.join(self.tmp.name, "default_prices.json")
        with open(prices, "w", encoding="utf-8") as fh:
            json.dump({"by_rarity": {"C": 500, "-": 400}}, fh)
        self.catalog = API_CATALOG
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=prices)
        got = {(c["set_code"], c["card_number"]): c["stats"]["median"] for c in raw["cards"] if c.get("price_basis") == "default"}
        self.assertEqual(got, {("MA3", "1/193"): 400, ("MA3", "2/193"): 500, ("MA6", "1/130"): 400, ("MA6", "3/130"): 400})
        self.assertFalse(any(c.get("variant") for c in raw["cards"] if c.get("price_basis")))   # a holo version is never base-priced
        self.assertEqual(validate_raw.validate(raw)[0], [])

    def test_a_set_where_no_card_has_a_rarity_mark_gets_no_base_price(self):
        def card(set_, n, den, name, rarity, cid):
            return {"set": set_, "number": f"{n:03d}/{den}", "num": n, "den": den, "name": name, "rarity": rarity, "image": f"https://o/{cid}.png", "id": cid}
        cards = [card("MA3", 1, 193, "Pinsir", None, 1), card("MA3", 2, 193, "Rare Thing", "SAR", 2),            # High Class: most cards unmarked, a few marked
                 card("PRM1", 1, 20, "Promo Ex", None, 3), card("PRM1", 2, 20, "Promo Two", None, 4)]             # a promo set: every card unmarked, ex cards included
        sets = [{"code": "MA3", "product": "High Class Booster Pack", "fetched": 2}, {"code": "PRM1", "product": "Promo Pack", "fetched": 2}]
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": sets, "cards": cards}, fh)
        prices = os.path.join(self.tmp.name, "default_prices.json")
        with open(prices, "w", encoding="utf-8") as fh:
            json.dump({"by_rarity": {"C": 500, "-": 500}}, fh)
        self.catalog = API_CATALOG
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=prices)
        got = {(c["set_code"], c["card_number"]) for c in raw["cards"] if c.get("price_basis") == "default"}
        self.assertEqual(got, {("MA3", "1/193")})                          # the unmarked card of a High Class set is a common; nothing in the promo set is guessed

    def test_the_order_of_versions_can_be_set_per_set(self):
        def card(n, cid):
            return {"set": "MA3", "number": "001/193", "num": 1, "den": 193, "name": "Pinsir", "rarity": None, "image": f"https://o/{cid}.png", "id": cid}
        cards = [card(1, 17239), card(1, 17574), card(1, 17575)]
        by = {v: c["id"] for (_, _, v), c in build_site.official_index(cards, [], {"MA3": ["", "poke_ball", "element"]}).items()}
        self.assertEqual(by, {"": 17239, "poke_ball": 17574, "element": 17575})
        self.assertEqual({v for (_, _, v) in build_site.official_index(cards, [], {})}, {"", "poke_ball", "master_ball"})   # the default order
        from_file = build_site.official_index(cards, [])                                                                    # tools/sets.json says MA3 is regular, Poke Ball, Elemen
        self.assertEqual({v for (_, _, v) in from_file}, {"", "poke_ball", "element"})

    def test_a_set_code_that_is_not_an_official_set_is_a_typo_and_the_printed_total_decides(self):
        sets_by_total = {"187": "SV8A"}
        known = {"SV8A", "MA3"}
        self.assertEqual(collect_raw.catalog_key("Dudunsparce 130/187 sv82 Kartu TCG Pokemon Indonesia", sets_by_total, known), ("SV8A", "130/187"))
        self.assertEqual(collect_raw.catalog_key("Dudunsparce 130/187 sv08a Indonesia", sets_by_total, known)[0], "SV8A")
        self.assertEqual(collect_raw.catalog_key("Dudunsparce 130/187 sv82 Indonesia", {}, known), None)      # no total to go by either
        self.assertEqual(collect_raw.catalog_key("Dudunsparce 130/187 sv82 Indonesia", sets_by_total)[0], "SV82")   # without the official list nothing is judged

    def test_versions_missing_on_the_official_site_are_added_where_yuyutei_lists_them(self):
        def card(set_, n, cid, rarity=None):
            return {"set": set_, "number": f"{n:03d}/187", "num": n, "den": 187, "name": f"Card {n}", "rarity": rarity, "image": f"https://o/{cid}.png", "id": cid}
        cards = [card("SV8A", 1, 1), card("SV8A", 1, 2), card("SV8A", 2, 3, "RR"),                      # unmarked: regular + Poke Ball on the site; RR: one entry
                 card("SV2A", 5, 4, "C"), card("SV2A", 6, 5, "U"), card("MA3", 1, 6)]
        jp = {"cards": [jp_card("sv08a", "001/187", "ピカチュウ(マスターボール柄/ミラー仕様)", 600), jp_card("sv08a", "001/187", "ピカチュウ(モンスターボール柄/ミラー仕様)", 400),
                        jp_card("sv08a", "001/187", "ピカチュウ", 50), jp_card("sv08a", "002/187", "ライチュウex", 900),
                        jp_card("sv02a", "005/187", "フシギダネ(モンスターボール柄/ミラー仕様)", 300), jp_card("sv02a", "005/187", "フシギダネ(マスターボール柄/ミラー仕様)", 500),
                        jp_card("m02a", "001/187", "ヒビキのカイロス(ロケット団マーク/ミラー仕様)", 120)]}
        extras = build_site.jp_versions(jp, {"SV8A": "sv08a", "SV2A": "sv02a"})
        self.assertEqual(extras, {("SV8A", "1/187"): {"master_ball", "poke_ball"}, ("SV2A", "5/187"): {"poke_ball", "master_ball"}})   # unmapped sets and the Rocket mirror add nothing
        idx = build_site.official_index(cards, [], {"SV8A": ["", "poke_ball"]}, extras)
        self.assertEqual(sorted(idx), [("ma3", "1/187", ""), ("sv2a", "5/187", ""), ("sv2a", "5/187", "master_ball"), ("sv2a", "5/187", "poke_ball"),
                                       ("sv2a", "6/187", ""), ("sv8a", "1/187", ""), ("sv8a", "1/187", "master_ball"), ("sv8a", "1/187", "poke_ball"),
                                       ("sv8a", "2/187", "")])
        added = idx[("sv8a", "1/187", "master_ball")]
        self.assertEqual((added["image"], added["extra"], idx[("sv8a", "1/187", "poke_ball")]["id"]), (None, True, 2))   # the listed Poke Ball is the official entry
        self.assertFalse(idx[("sv2a", "5/187", "")].get("extra"))
        plain = build_site.official_index(cards, [])                                                                      # without Yuyu-tei data nothing is added
        self.assertNotIn(("sv8a", "1/187", "master_ball"), plain)
        self.assertEqual({k[2] for k in plain if k[:2] == ("sv2a", "5/187")}, {""})

    def test_the_board_shows_the_yuyutei_price_beside_the_indonesian_one_and_adds_the_ball_versions(self):
        cards = [{"set": "SV8A", "number": "013/187", "num": 13, "den": 187, "name": "Rellor", "rarity": None, "image": f"https://o/{i}.png", "id": i} for i in (1, 2)]
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": [{"code": "SV8A", "product": "High Class", "fetched": 2}], "cards": cards}, fh)
        jp = {"generated_at": "2026-10-06T01:00:00+00:00", "card_url": "https://yuyu-tei.jp/sell/poc/card/{set}/{id}",
              "cards": [jp_card("sv08a", "013/187", "コロモリ", 30, id=7), jp_card("sv08a", "013/187", "コロモリ(モンスターボール柄/ミラー仕様)", 120, id=8),
                        jp_card("sv08a", "013/187", "コロモリ(マスターボール柄/ミラー仕様)", 400, id=9, in_stock=False)]}
        raw = good_raw()
        raw["cards"] = []
        data = build_site.build(tiny_latest(), raw, json.load(open(path, encoding="utf-8")), jp)
        rows = {r["variant"]: r for r in data["raw_cards"] if r["number"] == "013/187"}
        self.assertEqual(sorted(rows), ["", "master_ball", "poke_ball"])
        self.assertEqual([(rows[v]["jp"]["yen"], rows[v]["jp"]["in_stock"]) for v in ("", "poke_ball", "master_ball")], [(30, True), (120, True), (400, False)])
        self.assertEqual(rows["master_ball"]["jp"]["url"], "https://yuyu-tei.jp/sell/poc/card/sv08a/9")
        self.assertEqual((rows["master_ball"]["basis"], rows["master_ball"]["n"], rows["master_ball"]["image"]), (None, 0, None))   # no Indonesian listing yet
        sv8a = next(m for m in data["sets"] if m["code"] == "SV8A")
        self.assertEqual((sv8a["jp_set"], sv8a["jp_cards"]), ("sv08a", 3))
        self.assertEqual(data["jp_meta"]["cards"], 3)
        again = build_site.build(tiny_latest(), raw, json.load(open(path, encoding="utf-8")))                                          # without --jp nothing changes but the missing column
        self.assertEqual(sorted(r["variant"] for r in again["raw_cards"]), ["", "poke_ball"])
        self.assertIsNone(again["jp_meta"])
        self.assertTrue(all(r["jp"] is None for r in again["raw_cards"]))

    def test_a_listing_that_names_the_ball_pattern_is_its_own_card_with_its_own_price_and_history(self):
        cards = [{"set": "MA3", "number": "002/193", "num": 2, "den": 193, "name": "Yanma", "rarity": None, "image": "https://o/1.png", "id": 1},
                 {"set": "MA3", "number": "002/193", "num": 2, "den": 193, "name": "Yanma", "rarity": None, "image": "https://o/2.png", "id": 2},
                 {"set": "MA3", "number": "002/193", "num": 2, "den": 193, "name": "Yanma", "rarity": None, "image": "https://o/3.png", "id": 3},
                 {"set": "MA3", "number": "010/193", "num": 10, "den": 193, "name": "Poke Ball", "rarity": "U", "image": "https://o/4.png", "id": 4}]
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": [{"code": "MA3", "product": "High Class", "fetched": 4}], "cards": cards}, fh)
        self.catalog = API_CATALOG + [
            api_item("v-1", "Yanma MA3 002/193 Kartu Pokemon Indonesia", 3000),
            api_item("v-2", "Yanma MA3 002/193 Holo Pokeball Indonesia", 10000),
            api_item("v-3", "Yanma MA3 002/193 HOLO MASTERBALL Indonesia", 40000),
            api_item("v-4", "Poke Ball MA3 010/193 Indonesia Trainer", 2000)]    # the trainer card Poke Ball: not a foil version of anything
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=None)
        found = {(c["set_code"], c["card_number"], c.get("variant", "")): c for c in raw["cards"] if c.get("catalog")}
        self.assertEqual(sorted(found), [("MA3", "10/193", ""), ("MA3", "2/193", ""), ("MA3", "2/193", "master_ball"), ("MA3", "2/193", "poke_ball")])
        self.assertEqual([found[("MA3", "2/193", v)]["stats"]["median"] for v in ("", "poke_ball", "master_ball")], [3000, 10000, 40000])
        self.assertEqual(validate_raw.validate(raw)[0], [])
        db = sqlite3.connect(self.cfg.db)
        try:
            days = {r[0] for r in db.execute("SELECT card_number FROM snapshot WHERE card_set='MA3'")}
        finally:
            db.close()
        self.assertIn("2/193#master_ball", days)                                  # each version keeps its own history

    def ma3_catalog(self):
        cards = [{"set": "MA3", "number": "002/193", "num": 2, "den": 193, "name": "Yanma", "rarity": None, "image": f"https://o/{i}.png", "id": i} for i in (1, 2, 3)]
        cards += [{"set": "MA3", "number": "005/193", "num": 5, "den": 193, "name": "Sableye", "rarity": "RR", "image": "https://o/9.png", "id": 9}]
        cards += [{"set": "MA3", "number": f"{n:03d}/193", "num": n, "den": 193, "name": name, "rarity": None, "image": f"https://o/{n}.png", "id": 100 + n}
                  for n, name in ((54, "Tapu Koko"), (86, "Lunatone"), (87, "Solrock"))]
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": [{"code": "MA3", "product": "High Class", "fetched": len(cards)}], "cards": cards}, fh)
        return path

    def test_a_listing_that_only_says_holo_on_a_number_with_foil_versions_is_a_holo_card_not_the_regular_one(self):
        self.catalog = API_CATALOG + [
            api_item("h-1", "Yanma MA3 002/193 Kartu Pokemon Indonesia", 3000),
            api_item("h-2", "Yanma MA3 002/193 Holo Indonesia", 9000),                  # which pattern is not said
            api_item("h-3", "Yanma MA3 002/193 Holo Pokeball Indonesia", 10000),        # a named pattern keeps its own version
            api_item("h-4", "Sableye RR MA3 005/193 holo Indonesia", 20000)]            # this number has no foil versions: holo is just the card
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=self.ma3_catalog(), default_prices=None)
        found = {(c["set_code"], c["card_number"], c.get("variant", "")): c for c in raw["cards"] if c.get("catalog")}
        self.assertEqual(sorted(k for k in found if k[1] in ("2/193", "5/193")),
                         [("MA3", "2/193", ""), ("MA3", "2/193", "holo"), ("MA3", "2/193", "poke_ball"), ("MA3", "5/193", "")])
        self.assertEqual([found[("MA3", "2/193", v)]["stats"]["median"] for v in ("", "holo", "poke_ball")], [3000, 9000, 10000])
        self.assertEqual(found[("MA3", "5/193", "")]["stats"]["median"], 20000)
        self.assertEqual(validate_raw.validate(raw)[0], [])

    def test_asks_far_above_the_yuyutei_price_are_shown_but_not_counted(self):
        jp = os.path.join(self.tmp.name, "jp_prices.json")
        with open(jp, "w", encoding="utf-8") as fh:
            json.dump({"cards": [jp_card("m02a", "054/193", "タプ・コケコ", 30), jp_card("m02a", "086/193", "ルナトーン", 80), jp_card("m02a", "087/193", "ソルロック", 1000)]}, fh)
        rates = os.path.join(self.tmp.name, "rates.json")
        with open(rates, "w", encoding="utf-8") as fh:
            json.dump(rates_file(113.0, 16000.0), fh)
        self.catalog = API_CATALOG + [
            api_item("o-1", "Tapu Koko MA3 054/193 Pokemon TCG Indonesia", 7600),
            api_item("o-2", "Tapu Koko MA3 054/193 Kartu Pokemon Indonesia", 495000),     # 145x the Yuyu-tei price
            api_item("o-3", "Lunatone MA3 086/193 Pokemon TCG Indonesia", 300000),        # the only listing, and far above
            api_item("o-4", "Solrock MA3 087/193 Pokemon TCG Indonesia", 60000)]          # above Rp50.000 but the card sells for Rp113.000 in Japan
        path = self.ma3_catalog()
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=None, jp_json=jp, rates_json=rates)
        found = {c["card_number"]: c for c in raw["cards"] if c.get("catalog") and c["set_code"] == "MA3"}
        tapu = found["54/193"]
        self.assertEqual((tapu["stats"]["n"], tapu["stats"]["median"], [l["ask_idr"] for l in tapu["listings"]]), (1, 7600, [7600]))
        self.assertEqual([(o["ask_idr"], o["conf"]) for o in tapu["outliers"]], [(495000, "katalog")])
        self.assertNotIn("86/193", found)                                              # nothing but an outlier: no price from listings
        self.assertEqual(found["87/193"]["stats"]["median"], 60000)
        self.assertNotIn("outliers", found["87/193"])
        self.assertEqual(raw["coverage"]["outlier_listings"], 2)
        errors, _ = validate_raw.validate(raw)
        self.assertEqual(errors, [])
        again, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=None, jp_json=jp, rates_json=None)      # no yen rate: nothing judged
        self.assertEqual(again["coverage"]["outlier_listings"], 0)
        self.assertEqual({c["card_number"]: c["stats"]["n"] for c in again["cards"] if c.get("catalog") and c["set_code"] == "MA3"}["54/193"], 2)
        broken = json.loads(json.dumps(raw))
        next(c for c in broken["cards"] if c.get("outliers"))["outliers"][0]["conf"] = "nomor"
        self.assertTrue(any("outlier listing must be" in e for e in validate_raw.validate(broken)[0]))

    def test_a_seller_who_writes_the_wrong_total_still_lands_on_the_official_card(self):
        cards = [{"set": "SV9S", "number": "001/139", "num": 1, "den": 139, "name": "Caterpie", "rarity": "C", "image": "i", "id": 1},
                 {"set": "SV9S", "number": "002/139", "num": 2, "den": 139, "name": "Metapod", "rarity": "C", "image": "i", "id": 2},
                 {"set": "MA6", "number": "001/130", "num": 1, "den": 130, "name": "Exeggcute", "rarity": None, "image": "i", "id": 3}]
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "sets": [], "cards": cards}, fh)
        self.catalog = API_CATALOG + [
            api_item("w-1", "Caterpie 1/185 SV9S Pokemon TCG Indonesia", 4000),            # wrong total: official is 001/139
            api_item("w-2", "Metapod 002/139 SV9S Pokemon TCG Indonesia", 3000),           # right total
            api_item("w-3", "Pikachu 1/30 MA6 30th Anniversary Pokemon TCG Indonesia", 25000)]  # side series: not Exeggcute 001/130
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=None)
        found = {(c["set_code"], c["card_number"]): c for c in raw["cards"] if c.get("catalog")}
        self.assertEqual(sorted(found), [("MA6", "1/30"), ("SV9S", "1/139"), ("SV9S", "2/139")])
        self.assertEqual(found[("SV9S", "1/139")]["name"], "Caterpie")                      # one card, under the official number
        self.assertEqual(found[("SV9S", "1/139")]["stats"]["median"], 4000)

    def test_without_the_official_catalog_nothing_is_added(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        raw, _ = self.go(lang_mode="text", catalog=True, catalog_json=None, default_prices=None)
        self.assertEqual(raw["coverage"]["default_priced_cards"], 0)
        self.assertFalse(any(c.get("price_basis") for c in raw["cards"]))

    def test_the_base_price_does_not_enter_history_or_snapshots(self):
        self.catalog = API_CATALOG + CATALOG_EXTRA
        path, prices = self.official_catalog()
        self.go(lang_mode="text", catalog=True, catalog_json=path, default_prices=prices)
        db = sqlite3.connect(self.cfg.db)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshot WHERE card_number='115/193'").fetchone()[0], 0)
        finally:
            db.close()

    def test_set_crawl_pages_until_the_engine_runs_out_of_that_set(self):
        calls, items = [], {}

        def post(body):  # the first 90 results of every MA9 query name MA9, then the engine drifts to other cards
            inner = body["body"]
            calls.append((inner["query"], inner["from"]))
            rows = [api_item(f"s-{i}", f"Card {i} ex MA9 | {i}/99 RR Indonesia" if i < 90 else f"Other card {i}", 1000 + i)
                    for i in range(inner["from"], inner["from"] + inner["size"])]
            return {"results": rows, "total": 5000}
        used = collect_raw.crawl_sets(["MA9"], items, post, 45, 0, max_pages=25, budget=1000, min_share=0.3,
                                      sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(used, len(collect_raw.SET_SUFFIXES) * 3)            # pages 0, 1 relevant; page 2 irrelevant, then stop
        self.assertEqual(len(items), 135)                                    # the same slugs from every query count once
        self.assertEqual({q for q, _ in calls}, {f"MA9 {x}".strip() for x in collect_raw.SET_SUFFIXES})

    def test_set_crawl_spreads_a_small_budget_over_all_queries(self):
        calls = []

        def post(body):
            calls.append(body["body"]["query"])
            return {"results": [api_item(f"b-{len(calls)}-{i}", f"Card MA9 {i}", 1000) for i in range(45)], "total": 5000}
        used = collect_raw.crawl_sets(["MA9"], {}, post, 45, 0, max_pages=25, budget=5, min_share=0.3,
                                      sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual((used, len(set(calls))), (5, 5))                    # five different queries, not five pages of one

    def test_set_crawl_stops_on_the_page_limit_and_on_errors(self):
        def failing(body):
            raise collect_raw.FetchError("boom")
        self.assertEqual(collect_raw.crawl_sets(["MA9"], {}, failing, 45, 0, 25, 100, 0.3, sleep=lambda s: None,
                                                log=lambda *a: None), 0)
        pages = []

        def endless(body):
            pages.append(body["body"]["from"])
            return {"results": [api_item(f"e-{body['body']['from']}-{i}-{len(pages)}", "x MA9", 1000) for i in range(45)], "total": 9999}
        collect_raw.crawl_sets(["MA9"], {}, endless, 45, 0, 4, 1000, 0.3, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(max(pages), 3 * 45)                                  # --set-pages 4 means pages 0..3

    def test_top_sets_counts_indonesian_raw_cards_only(self):
        listings = [{"title": t, "description": ""} for t in (
            "A ex MA3 | 1/193 RR Indonesia", "B ex MA3 | 2/193 RR Indonesia", "C ex MA1 3/126 RR Indonesia",
            "D ex MA1 4/126 RR Indonesia", "E ex M2a 5/193 Japanese", "F ex MA7 6/100 Indonesia",
            "Booster Box MA3 Indonesia", "G EGS 10 MA3 Indonesia")]
        self.assertEqual(collect_raw.top_sets(listings, 5), ["MA3", "MA1"])  # MA7 once; the box, the slab and the JP card do not count
        self.assertEqual(collect_raw.top_sets(listings, 1), ["MA3"])

    def test_run_with_set_crawl_finds_cards_the_tracked_queries_missed(self):
        def post(body):
            inner = body["body"]
            self.posts.append(body)
            pool = CATALOG_EXTRA if inner["query"].startswith("MA3") else API_CATALOG
            return {"results": pool[inner["from"]:inner["from"] + inner["size"]], "total": len(pool)}
        self.post_fn = post
        raw, _ = self.go(lang_mode="text", catalog=True, sets=0)
        self.assertEqual(self.catalog_cards(raw), {})
        logs = []
        self.cfg.lang_mode, self.cfg.catalog, self.cfg.sets = "text", True, 1
        self.cfg.set_budget, self.cfg.set_pages, self.cfg.set_min_share = 50, 3, 0.3
        raw, _ = collect_raw.run(self.cfg, post, self.image_fn, None, now=datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc),
                                 sleep=lambda s: None, log=logs.append)
        self.assertEqual(sorted(self.catalog_cards(raw)), [("MA3", "114/193"), ("MA3", "71/193"), ("MA4", "128/123")])
        self.assertTrue(any("telusuri set: MA3" in l for l in logs))
        self.assertEqual(validate_raw.validate(raw)[0], [])

    def test_no_listings_writes_nothing(self):
        self.catalog = []
        raw, summary = self.go()
        self.assertIsNone(raw)

    def test_sample_check_prints_photo_language_next_to_the_text_guess(self):
        lines = []
        n = collect_raw.sample_check(self.cfg, self.post_fn, self.image_fn, self.classify_fn, log=lines.append)
        self.assertEqual(n, 3)
        self.assertIn("foto=ID | teks=ID | jenis=raw | Rp420,000 | Pikachu EX SAR MA3 kartu pokemon tcg indo", lines[0])
        self.assertTrue(lines[2].startswith("foto=JP | teks=unknown | jenis=raw"))

    def test_real_http_post_reaches_the_search_endpoint(self):
        site = FakeToco()
        try:
            data = collect_raw.make_post(site.base + "/api/toco-api/search")(collect_raw.search_body("x", 0, 2))
        finally:
            site.server.shutdown()
        self.assertEqual((data["total"], [r["slug"] for r in data["results"]]), (len(API_CATALOG), ["a-1", "a-2"]))

    def test_helpers(self):
        self.assertEqual(collect_raw.normalize_lang("ID"), "ID")
        self.assertEqual(collect_raw.normalize_lang("The text is Japanese."), "JP")
        self.assertEqual(collect_raw.normalize_lang("hmm"), "UNCLEAR")
        self.assertEqual(collect_raw.sniff_media_type(b"\x89PNG\r\n\x1a\nxx"), "image/png")
        self.assertIsNone(collect_raw.sniff_media_type(b"not an image"))
        self.assertEqual([collect_raw.quantile([1, 2, 3, 4], p) for p in (0, .25, .5, 1)], [1, 2, 2, 4])


class FakeOpenAI:
    """Local stand-in for POST /v1/chat/completions and GET /v1/models."""

    def __init__(self, answers, fail=None):
        self.seen, self.answers, self.fail = [], list(answers), fail
        outer = self

        class H(BaseHTTPRequestHandler):
            def send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send(200, {"object": "list", "data": [{"id": "model-b", "object": "model", "created": 1, "owned_by": "x"},
                                                           {"id": "model-a", "object": "model", "created": 1, "owned_by": "x"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if outer.fail:
                    return self.send(*outer.fail)
                self.send(200, {"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": outer.answers.pop(0)}}],
                                "usage": {"prompt_tokens": 800, "completion_tokens": 3, "total_tokens": 803}})

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()


@unittest.skipUnless(importlib.util.find_spec("openai"), "openai package not installed")
class OpenAIProvider(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20

    def use(self, **kw):
        self.fake = FakeOpenAI(**kw)
        self.addCleanup(self.fake.close)
        saved = {k: os.environ.get(k) for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY")}
        os.environ.update(OPENAI_BASE_URL=self.fake.url, OPENAI_API_KEY="test-key-not-real")

        def restore():
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)

    def test_sends_the_photo_as_a_data_url_and_reads_the_language(self):
        self.use(answers=["ID", "The text is Japanese.", "???"])
        clf = collect_raw.make_vision_classifier("some-model", "openai")
        self.assertEqual([clf(self.PNG, "image/png") for _ in range(3)], ["ID", "JP", "UNCLEAR"])
        req = self.fake.seen[0]
        self.assertEqual((req["path"], req["auth"], req["body"]["model"]),
                         ("/v1/chat/completions", "Bearer test-key-not-real", "some-model"))
        parts = req["body"]["messages"][0]["content"]
        self.assertEqual([p["type"] for p in parts], ["text", "image_url"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBORw0KGgo"))
        self.assertEqual(dict(clf.usage), {"input_tokens": 2400, "output_tokens": 9})

    def test_bad_key_missing_model_and_no_balance_stop_the_run_with_a_clear_message(self):
        cases = [((401, {"error": {"message": "bad key", "type": "invalid_request_error", "code": "invalid_api_key"}}), "OPENAI_API_KEY"),
                 ((404, {"error": {"message": "no such model", "type": "invalid_request_error", "code": "model_not_found"}}), "Model tidak ditemukan"),
                 ((429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}}), "Saldo API OpenAI")]
        for fail, expected in cases:
            self.use(answers=[], fail=fail)
            clf = collect_raw.make_vision_classifier("m", "openai")
            with self.assertRaises(SystemExit) as ctx:
                clf(self.PNG, "image/png")
            self.assertIn(expected, str(ctx.exception))

    def test_a_server_error_only_postpones_that_listing(self):
        self.use(answers=[], fail=(500, {"error": {"message": "boom", "type": "server_error"}}))
        clf = collect_raw._openai_classifier("m", max_retries=0)
        self.assertIsNone(clf(self.PNG, "image/png"))

    def test_list_models_prints_sorted_ids(self):
        self.use(answers=[])
        self.assertEqual(collect_raw.list_openai_models(), ["model-a", "model-b"])

    def test_cli_picks_the_provider_default_model(self):
        self.use(answers=[])
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(collect_raw.__file__), "collect_raw.py"),
                              "--provider", "openai", "--list-models"], capture_output=True, text=True,
                             env={**os.environ}).stdout
        self.assertEqual(out.split(), ["model-a", "model-b"])


class Probe(unittest.TestCase):
    LISTING = ('<html><head><title>Pikachu ex SAR | toco</title>'
               '<meta property="og:image" content="https://cdn.example/p1.jpg"></head>'
               '<body><p>Kartu EGS asli. Nomor seri slab: EG1234567 grade 10.</p></body></html>')

    def run_probe(self, get):
        lines = []
        code = probe_toco.probe(get=get, sleep=lambda s: None, out=lambda *a: lines.append(" ".join(map(str, a))))
        return code, "\n".join(lines)

    def test_report_covers_page_param_photo_and_serial(self):
        def get(url):
            if "/listing/" in url and "search" not in url:
                return 200, self.LISTING, ""
            if "&page=2" in url:
                return 200, '<a href="/id/listing/other-1790000000999-ff00">Sisa 1 Lain Rp1.000 Kota X</a>', ""
            if "&p=2" in url or "&halaman=2" in url:
                return 200, fake_page(), ""
            return 200, fake_page(), ""
        code, text = self.run_probe(get)
        self.assertEqual(code, 0)
        self.assertIn("listing terbaca=7", text)
        self.assertIn("dengan foto=7", text)
        self.assertIn("&page=2 -> status=200 listing=1 baru dibanding halaman 1=1", text)
        self.assertIn("&p=2 -> status=200 listing=7 baru dibanding halaman 1=0", text)
        self.assertIn("og:image: https://cdn.example/p1.jpg", text)
        self.assertIn("EG1234567", text)

    def test_blocked_or_offline(self):
        code, text = self.run_probe(lambda url: (0, "", "URLError: timed out"))
        self.assertEqual(code, 1)
        self.assertIn("diblokir", text)

    def test_flags_challenge_page(self):
        code, text = self.run_probe(lambda url: (403, "<html><title>Just a moment...</title></html>", "HTTP 403"))
        self.assertIn("PERHATIAN", text)


class ProbePokemonCard(unittest.TestCase):
    PAGES = {
        "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /admin/\n"),
        "/id/card-search/": (200, "text/html", """<html><head><title>Cari Kartu</title>
            <script src="/static/app.js"></script>
            <script>var expansionList = ["MA1","MA2"]; fetch('/api/cards/list.json');</script></head><body>
            <form><input type="checkbox" name="expansionCodes" value="MA1"><input type="checkbox" name="expansionCodes" value="MA2">
            <select name="rarity"><option value="all">Semua</option><option value="RR">RR</option></select></form>
            <a href="/id/card-search/list/?pageNo=1&expansionCodes=MA1">Evolusi Mega</a></body></html>"""),
        "/id/card-search/list/?pageNo=1": (200, "text/html", """<html><head><title>Daftar</title></head><body>
            <a href="/id/card-search/detail/101/"><img src="/id/card-img/id00000101.png"></a>
            <a href="/id/card-search/detail/102/"><img data-src="/id/card-img/id00000102.png"></a>
            <a href="/id/card-search/list/?pageNo=2">2</a><p>Hasil 1 - 2 dari 184</p></body></html>"""),
        "/id/card-search/list/?pageNo=2&expansionCodes=MA1": (404, "text/html", "<html>tidak ada</html>"),
        "/id/card-search/detail/101/": (200, "text/html", "<html><title>Mega Gardevoir ex</title><body><h1>Mega Gardevoir ex</h1><p>060/126 RR</p></body></html>"),
    }

    def setUp(self):
        self.pages = pages = dict(self.PAGES)  # a copy: a test may replace pages without touching the others

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                code, ctype, body = pages.get(self.path, (404, "text/plain", "no"))
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()

    def run_probe(self, base=None):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            probe_pokemon_card.main(["--base", base or self.base, "--delay", "0"])
        return buf.getvalue()

    def test_reports_links_images_controls_script_hints_and_a_card_page(self):
        out = self.run_probe()
        self.assertIn("robots.txt: status 200", out)
        self.assertIn("Disallow: /admin/", out)
        self.assertIn("/id/card-search/detail/N/", out)                 # link shape, numbers collapsed
        self.assertIn("/id/card-img/idN.png", out)                      # image shape, src and data-src both read
        self.assertIn("expansionCodes: 2 nilai, mis. ['MA1', 'MA2']", out)
        self.assertIn("rarity: 2 nilai", out)
        self.assertIn("petunjuk skrip: fetch('/api/cards/list.json')", out)
        self.assertIn("status 404", out)                                # the filtered list variant is reported, not fatal
        self.assertIn("Mega Gardevoir ex 060/126 RR", out)              # text of the card page that was opened

    def test_snippets_show_the_markup_around_the_data(self):
        pages = self.pages
        pages["/id/card-search/list/?pageNo=1&expansionCodes=MA1"] = (200, "text/html", """<html><body><p>Hasil Pencarian 184 buah</p>
            <ul><li class="card"><a href="/id/card-search/detail/101/"><img data-original="/id/card-img/id00000101.png" alt="Mega Gardevoir ex"></a></li></ul></body></html>""")
        pages["/id/card-search/detail/101/"] = (200, "text/html", """<html><title>Mega Gardevoir ex | Situs</title><body><h1>Mega Gardevoir ex</h1>
            <span class="collectorNumber">060 / 126</span><span class="rarity"><img src="/rarity/RR.png" alt="RR"></span>
            <a href="/id/card-search/list/?expansionCodes=MA1">Evolusi Mega</a><img src="/id/card-img/id00000101.png"></body></html>""")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            probe_pokemon_card.main(["--base", self.base, "--delay", "0", "--snippets"])
        out = buf.getvalue()
        self.assertIn("kotak set", out)
        self.assertIn('expansionCodes=MA1">Evolusi Mega', out)
        self.assertIn('<li class="card"><a href="/id/card-search/detail/101/">', out)
        self.assertIn("Hasil Pencarian 184 buah", out)
        self.assertIn('<span class="collectorNumber">060 / 126</span>', out)
        self.assertIn('alt="RR"', out)

    def test_any_page_with_robots_and_matches_around_a_pattern(self):
        self.pages["/sell/poc/s/m2a"] = (200, "text/html", "<html><title>Daftar</title><body><div class='card'>001/193 SAR Pikachu ex <b>12,800 円</b> 在庫 3</div>"
                                         "<div class='card'>002/193 RR Foo <b>80 円</b></div></body></html>")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            probe_pokemon_card.main(["--url", self.base + "/sell/poc/s/m2a", "--find", r"\d[\d,]*\s*円", "--delay", "0"])
        out = buf.getvalue()
        self.assertEqual(out.count("robots.txt: status"), 1)
        self.assertIn("Disallow: /admin/", out)
        self.assertIn("<b>12,800 円</b>", out)
        self.assertIn("002/193 RR Foo", out)

    def test_brief_mode_prints_only_the_matches(self):
        self.pages["/sell/poc/s/m02a"] = (200, "text/html", "<html><body><h3>SAR</h3><div>001/193 Pikachu 12,800 円</div></body></html>")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            probe_pokemon_card.main(["--url", self.base + "/sell/poc/s/m02a", "--find", "SAR", "--brief", "--delay", "0"])
        out = buf.getvalue()
        self.assertIn("status 200", out)
        self.assertIn("<h3>SAR</h3>", out)
        self.assertNotIn("tautan:", out)                      # no overview of links and images

    def test_find_can_look_further_back_and_show_fewer_matches(self):
        header = "<h3 class='rarity'>SAR</h3>"
        self.pages["/sell/poc/s/m02a"] = (200, "text/html", "<html><body>" + header + "<p>" + "x" * 900 + "</p><a href='/sell/poc/card/m02a/1'>card</a><a href='/sell/poc/card/m02a/2'>card</a></body></html>")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            probe_pokemon_card.main(["--url", self.base + "/sell/poc/s/m02a", "--find", "sell/poc/card/m02a/", "--brief", "--before", "1200", "--after", "50", "--limit", "1", "--delay", "0"])
        out = buf.getvalue()
        self.assertIn(header, out)                                # the header 900+ characters before the card is in view
        self.assertEqual(out.count("  > "), 1)                   # --limit 1

    def test_unreachable_site_is_reported_not_raised(self):
        out = self.run_probe("http://127.0.0.1:9")
        self.assertIn("status 0", out)
        self.assertIn("tidak ada tautan kartu", out)

    def test_url_shapes(self):
        self.assertEqual(probe_pokemon_card.pattern("https://x/id/card-search/list/?pageNo=3&expansionCodes=MA1"),
                         "/id/card-search/list/?expansionCodes,pageNo")


def official_site():
    """Pages shaped like the real asia.pokemon-card.com markup seen on 2026-10-04 (home tiles, list items, card page)."""
    def tile(code, series, date, name):
        return (f'<li class="expansion"><a class="expansionLink" href="/id/card-search/list/?expansionCodes={code}">'
                f'<div class="leftColumn"><div class="imageContainer"><img src="/id/card-img/products/{code}.png"></div></div>'
                f'<div class="rightColumn"><div class="seriesBlock"><span class="series">{series}</span> <span class="name">{name}</span></div>'
                f'<time class="relaseDate" datetime="{date}"><span>{date}</span></time></div></a></li>')
    pages = {
        "/id/card-search/": '<ul class="expansionList">' + tile("MA6", "Evolusi Mega", "09-16-2026", "30th CELEBRATION")
                            + tile("SV8S", "Scarlet &amp; Violet", "11-01-2025", "Kilat Rasi") + '</ul>'
                            '<ul class="pagination"><li class="paginationItem current"><span>1</span></li>'
                            '<li class="paginationItem"><a href="/id/card-search/?pageNo=2">2</a></li></ul>',
        "/id/card-search/?pageNo=2": '<ul class="expansionList">' + tile("SM1", "Sun &amp; Moon", "01-01-2017", "Lama") + '</ul>',
    }

    def card(cid):
        return (f'<li class="card"> <a href="/id/card-search/detail/{cid}/"> <div class="imageContainer loading">'
                f'<img class="lazy" src data-original="/id/card-img/id{cid:08d}.png" alt > </div> </a> </li>')

    def listing(code, ids, page_no, total, pages):
        chunk = ids[(page_no - 1) * 20: page_no * 20]
        return (f'<div class="resultTotalPages"><p>Hasil Pencarian <span>{total}</span> buah <span>Halaman {page_no} / Total {pages} halaman</span></p></div>'
                f'<ul class="list">{"".join(card(c) for c in chunk)}</ul>')

    def detail(cid, name, number, code, product, extra=""):
        num = f'<span class="alpha"> J </span> <span class="collectorNumber"> {number} </span>' if number else ""
        return (f'<title>{name} | Situs Web Pelatih</title><h1 class="pageHeader cardDetail"> <span class="evolveMarker"> basic </span> {name} </h1>'
                f'<div class="cardDetailPage"><section class="imageColumn"><div class="cardImage"> <img src="/id/card-img/id{cid:08d}.png"> </div></section>'
                f'<section class="expansionColumn"><p><span class="expansionSymbol"><img src="/mark/x.png"></span> {num}</p></section>'
                f'<section class="expansionLinkColumn"><h3>Ada di produk</h3>{extra}'
                f'<a href="/id/card-search/list/?expansionCodes={code}"> Booster Pack &quot;{product}&quot; </a></section></div>')
    ma6 = list(range(1000, 1025))  # 25 cards: two list pages
    for i, cid in enumerate(ma6, 1):
        # card 1002 is an energy without a collector number; card 1003 was also printed in another product (listed first)
        pages[f"/id/card-search/detail/{cid}/"] = detail(
            cid, f"Kartu {i}", None if cid == 1002 else f"{i:03d}/130", "MA6", "30th CELEBRATION",
            extra='<a href="/id/card-search/list/?expansionCodes=MATL"> Booster Pack &quot;Lain&quot; </a>' if cid == 1003 else "")
    for n in (1, 2):
        pages[f"/id/card-search/list/?pageNo={n}&expansionCodes=MA6"] = listing("MA6", ma6, n, 25, 2)
    # rarity: code 1 = C, 2 = U, 4 = RR, 15 = SAR; the other codes have no cards in MA6 and answer with an error page
    rarity_of = {1: ma6[:12], 2: ma6[12:18], 4: ma6[18:22], 15: ma6[22:]}
    for rcode, ids in rarity_of.items():
        for n in range(1, (len(ids) - 1) // 20 + 2):
            pages[f"/id/card-search/list/?pageNo={n}&expansionCodes=MA6&rarity%5B%5D={rcode}"] = listing("MA6", ids, n, len(ids), (len(ids) - 1) // 20 + 1)
    pages["/id/card-search/"] += ("".join(f'<input type="checkbox" name="rarity[]" id="rarity_{c}" value="{c}"><label for="rarity_{c}">{l}</label>'
                                          for c, l in (("1", "C"), ("2", "U"), ("4", "RR"), ("15", "SAR"), ("11", "Tanpa Tanda"))))
    sv = [2000, 2001]
    pages["/id/card-search/list/?pageNo=1&expansionCodes=SV8S"] = listing("SV8S", sv, 1, 2, 1)
    for i, cid in enumerate(sv, 1):
        pages[f"/id/card-search/detail/{cid}/"] = detail(cid, f"Sv {i}", f"{i:03d}/182", "SV8S", "Kilat Rasi")
    return pages


class FetchCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pages, self.calls = official_site(), []
        self.args = argparse.Namespace(
            db=os.path.join(self.tmp.name, "c.sqlite"), out=os.path.join(self.tmp.name, "catalog.json"), delay=0, max_requests=None,
            sets=None, include=fetch_catalog.DEFAULT_INCLUDE, refresh_days=30, list_sets=False, list_rarities=False, rarities=True)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, url):
        path = url.replace(fetch_catalog.BASE, "")
        self.calls.append(path)
        return (200, self.pages[path]) if path in self.pages else (404, "")

    def go(self, **over):
        for k, v in over.items():
            setattr(self.args, k, v)
        self.logs = []
        return fetch_catalog.run(self.args, get=self.get, now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
                                 sleep=lambda s: None, log=self.logs.append)

    def test_home_tiles_give_code_series_and_release_date(self):
        tiles, last = fetch_catalog.parse_home(self.pages["/id/card-search/"])
        self.assertEqual((last, [(t["code"], t["series"], t["released"]) for t in tiles]),
                         (2, [("MA6", "Evolusi Mega", "2026-09-16"), ("SV8S", "Scarlet & Violet", "2025-11-01")]))
        self.assertIn("30th CELEBRATION", tiles[0]["texts"])

    def test_list_sets_reads_every_home_page_and_nothing_else(self):
        self.go(list_sets=True)
        self.assertEqual(self.calls, ["/id/card-search/", "/id/card-search/?pageNo=2"])
        self.assertTrue(any(l.startswith("SM1") and "2017-01-01" in l for l in self.logs))

    def test_full_run_collects_names_numbers_images_and_set_names(self):
        data = self.go(include=fetch_catalog.BOOSTER_ONLY)
        sets = {s["code"]: s for s in data["sets"]}
        self.assertEqual(sorted(sets), ["MA6", "SV8S"])                       # SM1 does not match the booster-only pattern
        self.assertEqual((sets["MA6"]["name"], sets["MA6"]["released"], sets["MA6"]["total"], sets["MA6"]["complete"]),
                         ("30th CELEBRATION", "2026-09-16", 25, True))        # product of MA6, not the other product on card 1003
        ma6 = [c for c in data["cards"] if c["set"] == "MA6"]
        self.assertEqual(len(ma6), 24)                                        # the card without a number is left out
        self.assertEqual((ma6[0]["number"], ma6[0]["name"], ma6[0]["num"], ma6[0]["den"], ma6[0]["evolve"]), ("001/130", "Kartu 1", 1, 130, "basic"))
        self.assertEqual(ma6[0]["image"], "/id/card-img/id00001000.png")
        with open(self.args.out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cards"], data["cards"])

    def test_rarity_comes_from_one_filtered_list_per_code(self):
        data = self.go(sets="MA6")
        by_number = {c["number"]: c["rarity"] for c in data["cards"] if c["set"] == "MA6"}
        self.assertEqual((by_number["001/130"], by_number["012/130"], by_number["013/130"], by_number["019/130"], by_number["025/130"]),
                         ("C", "C", "U", "RR", "SAR"))
        self.assertEqual(collections.Counter(by_number.values()), {"C": 11, "U": 6, "RR": 4, "SAR": 3})  # 1002 has no number
        self.assertTrue(any(l.strip() == "rarity: C 12, U 6, RR 4, SAR 3" for l in self.logs))          # shown per set in the run log
        asked = [c for c in self.calls if "rarity%5B%5D" in c]
        self.assertIn("/id/card-search/list/?pageNo=1&expansionCodes=MA6&rarity%5B%5D=11", asked)       # every code of the filter is asked

    def test_rarities_are_not_asked_again_once_known_and_can_be_skipped(self):
        self.go(sets="MA6")
        before = len(self.calls)
        self.go(sets="MA6")
        self.assertFalse(any("rarity%5B%5D" in c for c in self.calls[before:]))
        other = tempfile.mkdtemp()
        self.args.db = os.path.join(other, "x.sqlite")
        self.args.rarities = False
        count = len(self.calls)
        data = self.go(sets="MA6")
        self.assertFalse(any("rarity%5B%5D" in c for c in self.calls[count:]))
        self.assertTrue(all(c["rarity"] is None for c in data["cards"]))
        self.args.rarities = True

    def test_if_the_site_ignores_the_rarity_filter_nothing_is_stored(self):
        everything = self.pages["/id/card-search/list/?pageNo=1&expansionCodes=MA6"]
        for rcode in (1, 2):
            self.pages[f"/id/card-search/list/?pageNo=1&expansionCodes=MA6&rarity%5B%5D={rcode}"] = everything
            self.pages[f"/id/card-search/list/?pageNo=2&expansionCodes=MA6&rarity%5B%5D={rcode}"] = self.pages["/id/card-search/list/?pageNo=2&expansionCodes=MA6"]
        data = self.go(sets="MA6")
        self.assertTrue(all(c["rarity"] is None for c in data["cards"]))
        self.assertTrue(any("filter rarity tampaknya diabaikan" in l for l in self.logs))

    def test_a_stopped_run_continues_without_refetching(self):
        first = self.go(sets="MA6", max_requests=8)                            # home 2 + list 2 + 4 cards
        self.assertFalse(first["sets"][0]["complete"])
        self.assertEqual(first["sets"][0]["fetched"], 4)
        used_before = len(self.calls)
        second = self.go(sets="MA6", max_requests=None)
        self.assertTrue(second["sets"][0]["complete"])
        detail_calls = [c for c in self.calls[used_before:] if "/detail/" in c]
        self.assertEqual(len(detail_calls), 21)                                # only the 21 cards still missing
        self.assertFalse(any("list/?pageNo" in c and "rarity" not in c for c in self.calls[used_before:]))  # lists are fresh for 30 days

    def test_detail_page_without_a_number_or_with_odd_markup_does_not_crash(self):
        d = fetch_catalog.parse_detail("<html><h1 class='x'>nothing</h1></html>")
        self.assertEqual((d["name"], d["number"], d["image"], d["products"]), (None, None, None, {}))

    def test_a_missing_page_is_skipped_and_reported(self):
        del self.pages["/id/card-search/detail/1005/"]
        data = self.go(sets="MA6")
        self.assertEqual(len([c for c in data["cards"] if c["set"] == "MA6"]), 23)
        self.assertFalse(data["sets"][0]["complete"])
        self.assertTrue(any("status 404" in l for l in self.logs))

    def test_only_one_fetch_per_database_at_a_time(self):
        import fcntl
        held = open(self.args.db + ".lock", "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertIsNone(self.go(sets="MA6"))
            self.assertTrue(any("Sudah ada pengambilan katalog yang berjalan" in l for l in self.logs))
            self.assertEqual(self.calls, [])                                    # it did not touch the site either
        finally:
            held.close()
        self.assertIsNotNone(self.go(sets="MA6"))                               # the lock is released with the process or file

    def test_default_include_takes_every_expansion_and_booster_only_keeps_the_old_narrow_pattern(self):
        import re
        every = ("MA1", "MA6", "SV8S", "SV11S", "SV1A", "SV2A", "SV4A", "AC3A", "AC3B", "MATL", "MAAL", "M", "SV", "SVM", "SVK", "SVTL", "SVDS", "S12A", "AS1A", "AC3D")
        self.assertTrue(all(re.search(fetch_catalog.DEFAULT_INCLUDE, c) for c in every))                       # the board shows every official card with its picture
        pick = lambda code: bool(re.search(fetch_catalog.BOOSTER_ONLY, code))
        self.assertTrue(all(pick(c) for c in ("MA1", "MA6", "SV8S", "SV11S", "SV1A", "SV2A", "SV4A", "AC3A", "AC3B")))
        self.assertFalse(any(pick(c) for c in ("MATL", "MAAL", "M", "SV", "SVM", "SVK", "SVTL", "SVDS", "S12A", "AS1A", "AC3D")))

    def test_rarity_filter_labels_from_label_tags_or_image_alt(self):
        page = ('<input type="checkbox" name="rarity[]" id="r12" value="12"><label for="r12"> <img src="x.png" alt="RR"> </label>'
                '<input type="checkbox" name="rarity[]" id="r1" value="1"><label for="r1">Common</label>'
                '<input type="checkbox" name="rarity[]" value="22"><span>SAR</span> <input type="checkbox" name="other" value="5">')
        got = [(c, l) for c, l, _ in fetch_catalog.parse_rarity_filter(page)]
        self.assertEqual(got, [("12", "RR"), ("1", "Common"), ("22", "SAR")])

    def test_list_rarities_prints_codes_and_pieces(self):
        self.pages["/id/card-search/"] += '<input type="checkbox" name="rarity[]" id="r1" value="1"><label for="r1">Common</label>'
        self.go(list_rarities=True)
        self.assertTrue(any("rarity kode '1': label 'Common'" in l for l in self.logs))

    def test_product_name_takes_the_quoted_part(self):
        self.assertEqual(fetch_catalog.product_name('Booster Pack "Evolusi Mega"'), "Evolusi Mega")
        self.assertEqual(fetch_catalog.product_name("Starter Deck"), "Starter Deck")


def yuyutei_site():
    """Pages shaped like the Yuyu-tei markup seen on 2026-10-05 (set checkboxes, rarity headers, card-product blocks)."""
    def option(code, label):
        return (f'<li><div class="p-0"> <input type="checkbox" class="versPhone" name="vers[]" value="{code}" id="{code}VersPhone" onClick="x()"> '
                f'<label for="{code}VersPhone" class="label py-2 ps-2 pe-2">{label}</label></div></li>'
                f'<li><div class="p-0"> <input type="checkbox" name="vers[]" value="{code}" id="{code}" onClick="y()"> <label for="{code}" class="label">{label}</label></div></li>')

    def product(code, cid, number, rarity, name, yen, stock, sold_out=False):
        return (f'<div class="col-md"><div class="card-product position-relative mt-4 {"sold-out" if sold_out else ""} ">'
                f'<div class="starbtn"> <em></em> <img src="https://cdn.yuyu-tei.jp/images/star.svg" alt="Star"/></div> '
                f'<a href="https://yuyu-tei.jp/sell/poc/card/{code}/{cid}"><div class="position-relative product-img"> '
                f'<img src="https://card.yuyu-tei.jp/poc/100_140/{code}/{cid}.jpg" alt="{number} {rarity} {name}" class="card img-fluid"/></div> </a> '
                f'<span class="d-block border border-dark p-1 w-100 text-center my-2">{number}</span> '
                f'<a href="https://yuyu-tei.jp/sell/poc/card/{code}/{cid}"><h4 class="text-primary fw-bold">{name}</h4> </a> '
                f'<strong class="d-block text-end "> {yen:,} 円 </strong><div class="form-check p-0"> '
                f'<label class="form-check-label fw-bold float-start cart_sell_zaiko" for="flexRadioDefault1"> 在庫 : {stock} </label></div></div></div>')

    def header(rarity):
        return f'<h3 class="text-primary fs-4 shadow fw-bold"> <span class="py-2 d-inline-block px-2 me-2 text-white fw-bold">{rarity}</span> Card List</h3>'
    m02a = ('<div class="filters"><span class="px-1">SAR</span></div><div class="py-4 cards-list" id="card-list3">' + header("MUR")
            + '<div class="row mt-2" id="card-lits">' + product("m02a", 10486, "250/193", "MUR", "メガカイリューex", 24800, "×", sold_out=True) + '</div>'
            + header("SAR") + '<div class="row mt-2">' + product("m02a", 10487, "249/193", "SAR", "ピカチュウex", 12800, "3 点")
            + product("m02a", 10488, "248/193", "SAR", "ゼクロムN", 9800, "12 点") + '</div>'
            + header("C") + '<div class="row mt-2">' + product("m02a", 10489, "001/193", "C", "ピンシア", 80, "20 点")
            + '</div></div><div class="carousel">' + product("m06a", 10136, "136/103", "AR", "ピカチュウ", 6980, "5 点") + '</div>')
    return {"/top/poc": "<html><form>" + option("m02a", "[M2a] ハイクラスパック MEGAドリームex") + option("m01l", "[M1L] 拡張パック メガブレイブ")
                       + option("sv11b", "[SV11B] 拡張パック ブラックボルト") + option("s08a", "[S8a] 拡張パック 25th") + "</form></html>",
            "/sell/poc/s/m02a": "<html>" + m02a + "</html>",
            "/sell/poc/s/m01l": "<html><div id=\"card-list3\">" + header("RR") + product("m01l", 1, "001/063", "RR", "フシギバナex", 480, "7 点") + "</div></html>"}


class FetchRates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "rates.json")
        self.args = argparse.Namespace(out=self.out, delay=0, max_age_hours=None)
        self.now = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        self.calls = []
        g = lambda cur, price: f'<div class="rPF6Lc" data-source="{cur}" data-target="IDR" data-last-price="{price}" data-x="1"><div class="YMlKec fxKbKc">{price}</div></div>'
        self.pages = {fetch_rates.GOOGLE.format(cur="JPY"): (200, g("JPY", "106.12")), fetch_rates.GOOGLE.format(cur="USD"): (200, g("USD", "16,250.5")),
                      fetch_rates.ECB.format(cur="JPY"): (200, '{"base":"JPY","rates":{"IDR":106.0}}'), fetch_rates.ECB.format(cur="USD"): (200, '{"base":"USD","rates":{"IDR":16240.0}}')}

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url, (404, ""))

    def go(self, **over):
        for k, v in over.items():
            setattr(self.args, k, v)
        self.logs = []
        return fetch_rates.run(self.args, get=self.get, now=self.now, sleep=lambda s: None, log=self.logs.append)

    def test_google_parsing(self):
        self.assertEqual(fetch_rates.parse_google('<div data-last-price="106.12">'), 106.12)
        self.assertEqual(fetch_rates.parse_google('<div class="YMlKec fxKbKc">16,250.50</div>'), 16250.5)
        self.assertIsNone(fetch_rates.parse_google("<html>consent.google.com</html>"))
        self.assertEqual(fetch_rates.parse_ecb('{"rates":{"IDR":105.5}}'), 105.5)
        self.assertIsNone(fetch_rates.parse_ecb("nope"))

    def test_google_is_used_when_it_agrees_with_the_ecb(self):
        data = self.go()
        self.assertEqual({c: (r["idr"], r["source"]) for c, r in data["rates"].items()}, {"JPY": (106.12, "google"), "USD": (16250.5, "google")})
        with open(self.out, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["rates"]["JPY"]["ecb"], saved["rates"]["JPY"]["fetched_at"][:10]), (106.0, "2026-10-06"))
        self.assertEqual(len(self.calls), 4)

    def test_the_ecb_rate_is_used_when_google_cannot_be_read_or_disagrees(self):
        self.pages[fetch_rates.GOOGLE.format(cur="JPY")] = (200, "<html>Before you continue to Google consent</html>")
        self.pages[fetch_rates.GOOGLE.format(cur="USD")] = (200, '<div data-last-price="19000.0">')                  # 17% above the ECB rate
        data = self.go()
        self.assertEqual({c: (r["idr"], r["source"]) for c, r in data["rates"].items()}, {"JPY": (106.0, "ecb"), "USD": (16240.0, "ecb")})
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "google_JPY.html")))                               # the page that could not be read is kept
        self.assertTrue(any("kurs tidak terbaca" in l and "consent" in l for l in self.logs))

    def test_a_rate_outside_the_sane_range_is_never_used(self):
        self.assertEqual(fetch_rates.choose("JPY", 0.0071, None), (None, None, "tidak ada kurs yang masuk akal"))     # the inverse pair (yen per rupiah)
        self.assertEqual(fetch_rates.choose("USD", 16250.0, 16200.0)[1], "google")
        self.assertEqual(fetch_rates.choose("USD", 1.0, 16200.0)[1], "ecb")

    def test_nothing_new_keeps_the_old_file_and_a_fresh_file_is_not_fetched_again(self):
        self.go()
        with open(self.out, encoding="utf-8") as fh:
            before = fh.read()
        self.calls.clear()
        self.pages.clear()                                                    # every source down
        later = fetch_rates.run(self.args, get=self.get, now=self.now + timedelta(days=1), sleep=lambda s: None, log=self.logs.append)
        with open(self.out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)                               # the old rates stay, nothing is rewritten
        self.assertEqual(later["rates"]["JPY"]["idr"], 106.12)
        self.calls.clear()
        self.go(max_age_hours=20)                                             # 0 hours old: no request at all
        self.assertEqual(self.calls, [])
        fetch_rates.run(self.args, get=self.get, now=self.now + timedelta(hours=21), sleep=lambda s: None, log=self.logs.append)
        self.assertEqual(len(self.calls), 4)                                  # 21 hours old: tried again

    def test_without_any_rate_there_is_no_file_and_the_exit_code_says_so(self):
        self.pages.clear()
        self.assertIsNone(self.go())
        self.assertFalse(os.path.exists(self.out))
        with mock.patch.object(fetch_rates, "http_get", lambda url: (0, "")):
            self.assertEqual(fetch_rates.main(["--out", self.out, "--delay", "0"]), 1)


class FetchJpPrices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pages, self.calls = yuyutei_site(), []
        self.args = argparse.Namespace(out=os.path.join(self.tmp.name, "jp.json"), delay=0, sets=None, include=fetch_jp_prices.DEFAULT_INCLUDE,
                                       max_requests=None, max_age_hours=None, list_sets=False)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, url):
        path = url.replace(fetch_jp_prices.BASE, "")
        self.calls.append(path)
        return (200, self.pages[path]) if path in self.pages else (404, "")

    def go(self, **over):
        for k, v in over.items():
            setattr(self.args, k, v)
        self.logs = []
        return fetch_jp_prices.run(self.args, get=self.get, now=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), sleep=lambda s: None, log=self.logs.append)

    def test_set_list_has_codes_and_names_once_each(self):
        sets = fetch_jp_prices.parse_set_list(self.pages["/top/poc"])
        self.assertEqual([c for c, _ in sets], ["m02a", "m01l", "sv11b", "s08a"])
        self.assertEqual(dict(sets)["m02a"], "[M2a] ハイクラスパック MEGAドリームex")

    def test_cards_with_number_rarity_name_price_stock_and_links(self):
        cards = fetch_jp_prices.parse_set_page(self.pages["/sell/poc/s/m02a"], "m02a")
        self.assertEqual([(c["number"], c["rarity"], c["name"], c["yen"], c["stock"]) for c in cards],
                         [("250/193", "MUR", "メガカイリューex", 24800, 0), ("249/193", "SAR", "ピカチュウex", 12800, 3),
                          ("248/193", "SAR", "ゼクロムN", 9800, 12), ("001/193", "C", "ピンシア", 80, 20)])
        self.assertEqual((cards[1]["url"], cards[1]["image"], cards[1]["id"]),
                         ("https://yuyu-tei.jp/sell/poc/card/m02a/10487", "https://card.yuyu-tei.jp/poc/100_140/m02a/10487.jpg", 10487))

    def test_cards_of_other_sets_shown_on_the_page_are_left_out(self):
        cards = fetch_jp_prices.parse_set_page(self.pages["/sell/poc/s/m02a"], "m02a")
        self.assertNotIn("m06a", {c["set"] for c in cards})

    def test_default_run_takes_mega_and_sv_sets_not_the_old_s_series(self):
        data = self.go()
        self.assertEqual([s["code"] for s in data["sets"]], ["m02a", "m01l"])        # sv11b has no page in the fake site: reported, skipped
        self.assertEqual(len(data["cards"]), 5)
        self.assertTrue(any("sv11b: status 404" in l for l in self.logs))
        self.assertNotIn("/sell/poc/s/s08a", self.calls)
        with open(self.args.out, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["currency"], saved["condition"], saved["generated_at"][:10]), ("JPY", "傷無し", "2026-10-05"))

    def test_markup_with_line_breaks_and_other_attribute_order_is_read_the_same(self):
        plain = self.pages["/sell/poc/s/m02a"]
        spaced = plain.replace('<div class="card-product', '<div\n   class="card-product').replace('<span class="d-block border', '<span\n class="d-block border')
        tabbed = plain.replace('<div class="card-product position-relative', '<div id="x" class="mt-4 card-product position-relative')
        want = fetch_jp_prices.parse_set_page(plain, "m02a")
        self.assertEqual(len(want), 4)
        for page in (spaced, tabbed):
            self.assertEqual(fetch_jp_prices.parse_set_page(page, "m02a"), want)
        self.assertEqual(fetch_jp_prices.parse_set_page(spaced, "m02a")[0]["stock"], 0)       # sold-out is read from the card's own tag

    def test_foil_pattern_comes_from_the_bracket_in_the_name_and_a_circle_means_in_stock(self):
        self.assertEqual(fetch_jp_prices.split_name("ヒビキのカイロス"), ("ヒビキのカイロス", "", ""))
        self.assertEqual(fetch_jp_prices.split_name("ヤンヤンマ(エネルギーマーク柄/ミラー仕様)"), ("ヤンヤンマ", "エネルギーマーク柄/ミラー仕様", "element"))
        self.assertEqual(fetch_jp_prices.split_name("ヤンヤンマ（ボール柄/ミラー仕様）"), ("ヤンヤンマ", "ボール柄/ミラー仕様", "poke_ball"))
        self.assertEqual(fetch_jp_prices.split_name("ピカチュウ(マスターボールミラー)")[2], "master_ball")
        self.assertEqual(fetch_jp_prices.split_name("シロナのロズレイド(R仕様)"), ("シロナのロズレイド", "R仕様", ""))
        page = self.pages["/sell/poc/s/m02a"].replace("ピンシア", "ピンシア(ボール柄/ミラー仕様)").replace("在庫 : 20 点", "在庫 : ◯")
        cards = {c["number"]: c for c in fetch_jp_prices.parse_set_page(page, "m02a")}
        self.assertEqual([(c["name"], c["variant"], c["in_stock"], c["stock"]) for c in (cards["001/193"],)], [("ピンシア", "poke_ball", True, None)])
        self.assertEqual((cards["250/193"]["in_stock"], cards["250/193"]["stock"]), (False, 0))     # sold out
        self.assertEqual((cards["249/193"]["in_stock"], cards["249/193"]["stock"]), (True, 3))

    def test_the_saved_file_is_compact_one_card_per_line_without_derivable_fields(self):
        self.go(sets="m02a")
        with open(self.args.out, encoding="utf-8") as fh:
            raw = fh.read()
        lines = raw.splitlines()
        self.assertEqual(len(lines), 6)                                                # header line, 4 cards, closing
        data = json.loads(raw)
        self.assertEqual(len(data["cards"]), 4)
        self.assertNotIn("url", data["cards"][0])
        self.assertNotIn("image", data["cards"][0])
        self.assertEqual(data["card_url"].format(**data["cards"][1]), "https://yuyu-tei.jp/sell/poc/card/m02a/10487")

    def test_a_fresh_file_is_not_fetched_again_and_a_poor_day_does_not_shrink_it(self):
        self.go()
        self.assertGreater(len(self.calls), 3)
        self.calls.clear()
        self.args.max_age_hours = 20
        now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        fetch_jp_prices.run(self.args, get=self.get, now=now + timedelta(hours=5), sleep=lambda s: None, log=self.logs.append)
        self.assertEqual(self.calls, [])                                                    # 5 hours old
        data = fetch_jp_prices.run(self.args, get=self.get, now=now + timedelta(hours=21), sleep=lambda s: None, log=self.logs.append)
        self.assertGreater(len(self.calls), 3)                                              # 21 hours old: fetched again
        self.assertEqual(len(data["cards"]), 5)
        with open(self.args.out, encoding="utf-8") as fh:                                   # an earlier, bigger file
            bigger = json.load(fh)
        bigger["cards"] = bigger["cards"] * 4
        fetch_jp_prices.write_json(self.args.out, bigger)
        self.args.max_age_hours = None
        kept = self.go()
        self.assertEqual(len(kept["cards"]), 20)                                            # 5 new cards against 20 before: the old file stays
        self.assertTrue(any("berkas lama dipertahankan" in l for l in self.logs))
        with open(self.args.out, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["cards"]), 20)

    def test_a_saved_page_can_be_checked_without_any_request(self):
        path = os.path.join(self.tmp.name, "yuyutei_m02a.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.pages["/sell/poc/s/m02a"])
        logs = []
        cards = fetch_jp_prices.parse_saved(path, "m02a", log=logs.append)
        self.assertEqual(len(cards), 4)
        self.assertIn("4 kartu, 3 ada stok", logs[0])
        self.assertEqual(self.calls, [])
        self.assertEqual(fetch_jp_prices.main(["--parse-file", path, "--sets", "m02a"]), 0)

    def test_a_page_with_no_readable_cards_is_saved_and_described(self):
        self.pages["/sell/poc/s/m01l"] = '<html><div id="card-list3"><div class="card-product x"><a href="https://yuyu-tei.jp/sell/poc/card/m09/5">x</a></div></div></html>'
        data = self.go(sets="m01l")
        self.assertEqual(data["cards"], [])
        say = next(l for l in self.logs if "tidak ada kartu terbaca" in l)
        self.assertIn("1 blok card-product", say)
        self.assertIn("'m09': 1", say)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "yuyutei_m01l.html")))

    def test_a_closed_pipe_is_not_an_error(self):
        def closed(*a, **k):
            raise BrokenPipeError
        with mock.patch.object(fetch_jp_prices, "run", closed):
            self.assertEqual(fetch_jp_prices.main(["--list-sets"]), 0)

    def test_list_sets_makes_one_request(self):
        self.go(list_sets=True)
        self.assertEqual(self.calls, ["/top/poc"])
        self.assertTrue(any(l.startswith("m02a") and "MEGAドリームex" in l for l in self.logs))

    def test_budget_and_explicit_sets(self):
        data = self.go(sets="m02a,m01l", max_requests=1)
        self.assertEqual([s["code"] for s in data["sets"]], ["m02a"])
        self.assertTrue(any("anggaran permintaan habis" in l for l in self.logs))


class ProbeNext(unittest.TestCase):
    HTML = ('<html><body><div id="__next"></div>'
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{}},"page":"/search","query":{"q":"egs"},"buildId":"abc123","locale":"id"}</script>'
            '<script src="/_next/static/chunks/pages/search-1a2b.js"></script></body></html>')
    JS = ('fetch("https://api.toco.id/v2/products/search?q="+q);var u="/api/listing/detail";'
          'var d="https://www.w3.org/2000/svg";var x="https://cdn.toco.id/img/logo.png";')

    def test_finds_data_endpoints(self):
        lines = []

        def get(url, limit=0):
            if "search-1a2b.js" in url:
                return 200, self.JS, ""
            if "_next/data/abc123/search.json" in url:
                return 200, '{"pageProps":{}}', ""
            return 200, self.HTML, ""
        probe_toco_next.run(get=get, sleep=lambda s: None, out=lambda *a: lines.append(" ".join(map(str, a))))
        text = "\n".join(lines)
        self.assertIn("buildId=abc123", text)
        self.assertIn("https://api.toco.id/v2/products/search?q=", text)
        self.assertIn("/api/listing/detail", text)
        self.assertNotIn("w3.org", text)
        self.assertIn("/_next/data/abc123/search.json?q=egs -> status=200", text)


# A fake site that behaves like toco: the search page is an empty shell and JavaScript fills in the
# listings from a data request. Used only when Playwright and a Chrome binary are available.
FAKE_PAGES = {
    "1": [("a-1", "id-1", "Sisa 1 Pikachu ex SAR 234/193 MA3 ID NM Rp420.000 Kota Jambi"),
          ("a-2", "id-2", "Sisa 2 Pikachu ex SAR 234/193 MA3 Indo Rp380.000 Kota Surabaya"),
          ("a-3", "jp-3", "Sisa 1 Pikachu ex SAR 234/193 MA3 Rp300.000 Kota Bandung")],
    "2": [("b-1", "id-4", "Sisa 1 Mega Venusaur ex SAR 173/126 MA1 ID Rp120.000 Kota Jambi"),
          ("b-2", "id-5", "Sisa 1 Mega Venusaur ex SAR 173/126 MA1 Indo Rp100.000 Kota Depok")],
}
SHELL = ("<html><body><div id=app></div><script>"
         "const p=Number(new URLSearchParams(location.search).get('page')||'1');"
         "const call=(path,body)=>fetch('/api/toco-api/search',{method:'POST',headers:{'Content-Type':'application/json'},"
         "body:JSON.stringify({path:path,body:body})}).then(r=>r.json());"
         "setTimeout(()=>{call('/store-search',{query:'egs',limit:3,offset:0});"
         "call('/product-search',{query:'egs',limit:3,offset:(p-1)*3}).then(d=>{"
         "document.getElementById('app').innerHTML=d.results.map(i=>`<a href='/id/listing/${i.slug}-17900000000${p}0-ab12'>"
         "<img src='/img/${i.img}.jpg'><span>${i.text}</span></a>`).join('')})},300)</script></body></html>")
LISTING = ("<html><head><title>Pikachu ex SAR | toco</title><meta property='og:image' content='/img/id-1.jpg'></head>"
           "<body><img src='/img/big.png'><p>Kondisi Bekas. Deskripsi: kartu asli. Nomor seri slab: EG12345</p>"
           "<script id='__NEXT_DATA__' type='application/json'>"
           '{"props":{"pageProps":{"adDetail":{"title":"Pikachu ex SAR","description":"Kartu asli CHINESE",'
           '"condition":"used","images":["__HOST__/img/big.png","__HOST__/img/b2.png"]}}}}'
           "</script></body></html>")


def png_bytes(w=300, h=300):
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\x20\x40\x80" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def chrome_path():
    for path in (os.environ.get("EGS_CHROME"), "/opt/pw-browsers/chromium", "/usr/bin/google-chrome"):
        if path and os.path.exists(path):
            return path
    return None


class FakeToco:
    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                if u.path == "/search":
                    body, ctype = SHELL.encode(), "text/html"
                elif "/listing/" in u.path:
                    body, ctype = LISTING.replace("__HOST__", "http://" + self.headers["Host"]).encode(), "text/html"
                elif u.path.startswith("/img/"):
                    body, ctype = png_bytes(), "image/png"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                if req.get("path") == "/store-search":
                    out = {"results": [{"toco_name": "muslim tcg"}, {"toco_name": "lain"}], "total": 2, "query": "egs"}
                elif req.get("path") == "/listing-search":
                    inner = req.get("body") or {}
                    off, size = int(inner.get("from", 0)), int(inner.get("size", 45))
                    out = {"results": API_CATALOG[off:off + size], "total": len(API_CATALOG), "query": inner.get("query")}
                else:
                    inner = req.get("body") or {}
                    page = str(int(inner.get("offset", inner.get("from", 0))) // 3 + 1)
                    items = [{"slug": s, "img": i, "text": t} for s, i, t in FAKE_PAGES.get(page, [])]
                    out = {"results": items, "total": 5, "query": "egs", "filters": None}
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"


class FieldsProbe(unittest.TestCase):
    def test_lists_fields_and_runs_card_queries_over_plain_http(self):
        site = FakeToco()
        try:
            lines = []
            code = probe_toco_fields.run(out=lambda *a: lines.append(" ".join(map(str, a))), sleep=lambda s: None,
                                         base=site.base, queries=["Pikachu ex 234/193", "zzz"])
        finally:
            site.server.shutdown()
        text = "\n".join(lines)
        self.assertEqual(code, 0)
        self.assertIn("slug = a-1", text)
        self.assertIn("[Pikachu ex 234/193] total=9 diterima=9", text)
        self.assertIn("[zzz] total=9 diterima=9", text)

    def test_reports_failure(self):
        lines = []
        code = probe_toco_fields.run(out=lambda *a: lines.append(" ".join(map(str, a))), sleep=lambda s: None,
                                     base="http://127.0.0.1:9", queries=[])
        self.assertEqual(code, 1)
        self.assertIn("gagal", "\n".join(lines))


HAVE_BROWSER = importlib.util.find_spec("playwright") is not None and chrome_path() is not None


@unittest.skipUnless(HAVE_BROWSER, "Playwright dan Chrome tidak tersedia")
class BrowserRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["EGS_CHROME"] = chrome_path()
        cls.site = FakeToco()

    @classmethod
    def tearDownClass(cls):
        cls.site.server.shutdown()

    def test_board_shows_raw_trend_and_history_without_script_errors(self):
        from playwright.sync_api import sync_playwright
        raw = good_raw()
        raw["cards"][0]["history"] = [{"date_wib": "2026-10-03", "n": 3, "median": 100000, "min": 80000},
                                      {"date_wib": "2026-10-10", "n": 3, "median": 112000, "min": 80000}]
        html = build_site.render(build_site.build(tiny_latest(), raw), True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True,
                                        args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("file://" + path)
            page.wait_for_timeout(500)
            text = page.evaluate("() => document.body.innerText")
            bars = page.evaluate("() => document.querySelectorAll('.spark span').length")
            browser.close()
        self.assertEqual(errors, [])
        self.assertIn("↑ 12% dalam 7 hari", text)
        self.assertEqual(bars, 2)

    def test_board_groups_raw_cards_by_set_with_names_search_and_egs(self):
        from playwright.sync_api import sync_playwright
        import copy
        raw = good_raw()  # the tracked Mega Venusaur ex MA1 173/126, which has an EGS 10 slab in tiny_latest()
        base = raw["cards"][0]

        def add(code, number, name, rarity="RR"):
            c = copy.deepcopy(base)
            c.update(name=name, set_code=code, card_number=number, catalog=True, rarity=rarity)
            for l in c["listings"]:
                l["conf"] = "katalog"
                l["url"] += f"-{code}-{number.replace('/', '_')}"
                l["photo"] = f"https://img.example/{code}-{number.replace('/', '_')}.webp"
            raw["cards"].append(c)
        for i in range(130, 0, -1):  # added in reverse: the page must still show 1/193, 2/193, 3/193 first
            add("MA3", f"{i}/193", f"Karu {i}")
        for i in range(1, 6):
            add("XY1", f"{i}/50", f"Baru {i}")  # a set that is not in sets.json
        raw["coverage"].update(cards=len(raw["cards"]), listings_counted=3 * len(raw["cards"]), catalog_cards=135)
        self.assertEqual(validate_raw.validate(raw)[0], [])
        data = build_site.build(tiny_latest(), raw)
        self.assertEqual([m["code"] for m in data["sets"]], ["MA3", "MA1", "XY1"])  # MEGA family newest first, a set outside the known families last
        html = build_site.render(data, True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        gif = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True,
                                        args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://img.example/**", lambda route: route.fulfill(status=200, content_type="image/gif", body=gif))
            page.goto("file://" + path)
            page.wait_for_timeout(300)
            ev = page.evaluate
            rows = lambda: ev("() => document.querySelectorAll('#rawonly-body tbody tr').length")
            cells = lambda i: ev("(i) => [...document.querySelectorAll('#rawonly-body tbody tr')[i].children].map(td => td.textContent)", i)
            chips = ev("() => [...document.querySelectorAll('#rawonly-sets .chip')].map(c => c.textContent)")
            first = {"info": ev("() => document.querySelector('#rawonly-info').innerText"), "rows": rows(),
                     "numbers": [cells(i)[1] for i in range(3)],
                     "count": ev("() => document.querySelector('#rawonly .count').textContent"),
                     "photos": ev("() => document.querySelectorAll('#rawonly-body td.ph img').length")}
            page.click("#rawonly-sets .chip:has-text('XY1')")
            other = {"info": ev("() => document.querySelector('#rawonly-info').innerText"), "rows": rows(), "head": ev("() => [...document.querySelectorAll('#rawonly-body th')].map(t => t.textContent)")}
            page.click("#rawonly-sets .chip:has-text('MA1 ')")
            tracked = cells(0)
            page.fill("#rawonly-tools input", "karu 77")
            searched = {"rows": rows(), "set": cells(0)[1], "info": ev("() => document.querySelector('#rawonly-info').innerText")}
            page.fill("#rawonly-tools input", "")
            page.click("#rawonly-sets .chip:has-text('MA3')")
            page.click("#rawonly-body button")
            expanded = rows()
            browser.close()
        self.assertEqual(errors, [])
        self.assertEqual(chips[0], "Semua set136")                                                    # 130 + 5 + the tracked one
        self.assertTrue(any(c.startswith("MA3 · Evolusi Mega Impian ex") and "130" in c for c in chips))
        self.assertTrue(any(c.startswith("XY1") and "Evolusi" not in c for c in chips))
        self.assertIn("MA3 · Evolusi Mega Impian ex", first["info"])
        self.assertIn("dirilis 30 Jan 2026", first["info"])
        self.assertIn("130 dari 250 kartu punya harga RAW", first["info"])
        self.assertEqual((first["rows"], first["count"], first["photos"]), (80, "80 dari 130 kartu", 80))
        self.assertEqual(first["numbers"], ["1/193", "2/193", "3/193"])
        self.assertIn("nama belum diketahui", other["info"])
        self.assertEqual(other["rows"], 5)
        self.assertNotIn("Set", other["head"])
        self.assertIn("dipantau", tracked[2])
        self.assertIn("EGS 10 Rp650.000", tracked[8])
        self.assertEqual((searched["rows"], searched["set"]), (1, "MA3"))
        self.assertIn("Hasil pencarian", searched["info"])
        self.assertEqual(expanded, 130)

    def test_board_orders_sets_by_their_dearest_card_and_cards_dearest_first_with_a_switch_back_to_numbers(self):
        from playwright.sync_api import sync_playwright
        import copy
        raw, catalog = board_with_official_catalog()
        listed = next(c for c in raw["cards"] if c.get("card_number") == "114/193")
        listed["stats"] = {"n": 3, "min": 250000, "p25": 280000, "median": 300000, "p75": 320000, "max": 350000}
        dear = copy.deepcopy(listed)
        dear.update(name="Other Rare", card_number="118/193", rarity="SR", stats={"n": 3, "min": 1800000, "p25": 1900000, "median": 2000000, "p75": 2100000, "max": 2200000})
        for l in dear["listings"]:
            l["url"] += "-118"
        top = copy.deepcopy(listed)
        top.update(name="Chase Card", set_code="XY1", card_number="1/50", rarity="UR", catalog=True, stats={"n": 3, "min": 4800000, "p25": 4900000, "median": 5000000, "p75": 5100000, "max": 5200000})
        for l in top["listings"]:
            l["url"] += "-xy1"
        raw["cards"] += [dear, top]
        html = build_site.render(build_site.build(tiny_latest(), raw, catalog), True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True, args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://**/*", lambda route: route.abort())
            page.goto("file://" + path)
            page.wait_for_timeout(300)
            ev = page.evaluate
            chips = lambda: ev("() => [...document.querySelectorAll('#rawonly-sets .chip')].map(c => c.firstChild.textContent.split(' ')[0])")
            rows = lambda: ev("() => [...document.querySelectorAll('#rawonly-body tbody tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ').trim())")
            groups = lambda: ev("() => [...document.querySelectorAll('#rawonly-sets .lab')].map(l => l.textContent)")
            by_price = {"chips": chips(), "groups": groups(), "first_info": ev("() => document.querySelector('#rawonly-info').innerText"),
                        "label": ev("() => document.querySelector('#raw-sort').textContent")}
            page.click("#rawonly-sets .chip:has-text('MA3')")
            ma3_price = {"rows": rows(), "info": ev("() => document.querySelector('#rawonly-info').innerText")}
            page.click("#raw-sort")
            by_number = {"chips": chips(), "groups": groups(), "label": ev("() => document.querySelector('#raw-sort').textContent")}
            page.click("#rawonly-sets .chip:has-text('MA3')")
            ma3_number = rows()
            browser.close()
        self.assertEqual(errors, [])
        self.assertEqual(by_price["chips"][:4], ["Semua", "XY1", "MA3", "MA1"])                             # sets by the price of their dearest card
        self.assertEqual(by_price["groups"], ["Dari kartu termahal"])
        self.assertEqual(by_price["label"], "Urut: harga termahal dulu")
        self.assertIn("kartu termahal: Chase Card 1/50 UR Rp5.000.000", by_price["first_info"])               # the page opens on the dearest set
        order = [r.split(" ")[0] for r in ma3_price["rows"]]
        self.assertEqual(order, ["118/193", "114/193", "115/193", "117/193"])                                  # dearest first, the base-priced common, then the card with no price
        self.assertIn("kartu termahal: Other Rare 118/193 SR Rp2.000.000", ma3_price["info"])
        self.assertEqual(by_number["label"], "Urut: nomor kartu")
        self.assertEqual(by_number["chips"][:4], ["Semua", "MA3", "MA1", "XY1"])                               # the old order: MEGA family first, unknown sets last
        self.assertIn("Seri MEGA", by_number["groups"])
        self.assertEqual([r.split(" ")[0] for r in ma3_number], ["114/193", "115/193", "117/193", "118/193"])

    def test_board_shows_the_reprint_badge_and_finds_reprints_by_searching_the_word(self):
        from playwright.sync_api import sync_playwright
        raw, catalog = board_with_official_catalog()
        marks = {"MA3|117/193": {"rarity": "RRR", "reprint": "30th"}}
        html = build_site.render(build_site.build(tiny_latest(), raw, catalog, marks=marks), True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True, args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://**/*", lambda route: route.abort())
            page.goto("file://" + path)
            page.wait_for_timeout(300)
            ev = page.evaluate
            rows = lambda: ev("() => [...document.querySelectorAll('#rawonly-body tbody tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ').trim())")
            shown = rows()
            page.fill("#rawonly-tools input", "reprint")
            found = rows()
            browser.close()
        self.assertEqual(errors, [])
        self.assertTrue(any("117/193 Rare Thing SAR" in r and "reprint 30th" in r for r in shown))                 # the official rarity wins over a mark
        self.assertEqual(len(found), 1)
        self.assertIn("117/193", found[0])

    def test_board_shows_base_prices_missing_prices_official_images_and_can_hide_unpriced(self):
        from playwright.sync_api import sync_playwright
        raw, catalog = board_with_official_catalog()
        html = build_site.render(build_site.build(tiny_latest(), raw, catalog), True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        gif = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True,
                                        args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://**/*", lambda route: route.fulfill(status=200, content_type="image/gif", body=gif))
            page.goto("file://" + path)
            page.wait_for_timeout(300)
            ev = page.evaluate
            rows = lambda: ev("() => [...document.querySelectorAll('#rawonly-body tbody tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ').trim())")
            info = ev("() => document.querySelector('#rawonly-info').innerText")
            shown = rows()
            images = ev("() => [...document.querySelectorAll('#rawonly-body td.ph img')].map(i => i.getAttribute('src'))")
            page.check("#hide-unpriced")
            hidden = rows()
            browser.close()
        self.assertEqual(errors, [])
        self.assertNotIn("null", info)                                       # a set without a note shows no stray text
        self.assertIn("4 kartu resmi: 1 punya listing RAW", info)
        self.assertIn("1 harga dasar, 2 belum ada listing", info)
        self.assertEqual(len(shown), 4)
        self.assertTrue(any("115/193 Foo C" in r and "Rp500" in r and "harga dasar" in r for r in shown))
        self.assertTrue(any("117/193 Rare Thing SAR" in r and "belum ada listing" in r for r in shown))
        self.assertEqual(images[0], "https://official.example/114.png")          # the official image, not the seller's photo
        self.assertEqual(len(hidden), 2)
        self.assertFalse(any("belum ada listing" in r for r in hidden))

    def test_board_shows_the_yuyutei_column_with_rupiah_and_sold_out_and_says_so_for_unmapped_sets(self):
        from playwright.sync_api import sync_playwright
        raw, catalog = board_with_official_catalog()
        jp = {"generated_at": "2026-10-06T01:00:00+00:00", "card_url": "https://yuyu-tei.jp/sell/poc/card/{set}/{id}",
              "cards": [jp_card("m02a", "114/193", "フェザンディ", 2480, id=114), jp_card("m02a", "115/193", "フー", 30, id=115, in_stock=False)]}
        html = build_site.render(build_site.build(tiny_latest(), raw, catalog, jp, rates_file(105.5, 16250.4)), True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        gif = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True, args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://**/*", lambda route: route.fulfill(status=200, content_type="image/gif", body=gif))
            page.goto("file://" + path)
            page.wait_for_timeout(300)
            ev = page.evaluate
            head = ev("() => [...document.querySelectorAll('#rawonly-body thead th')].map(th => th.innerText)")
            rows = ev("() => [...document.querySelectorAll('#rawonly-body tbody tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ').trim())")
            link = ev("() => document.querySelector('#rawonly-body td.jp a').getAttribute('href')")
            info = ev("() => document.querySelector('#rawonly-info').innerText")
            kurs = ev("() => document.querySelector('#kurs').innerText")
            page.click("#rawonly-sets .chip:has-text('MA1')")
            other = ev("() => document.querySelector('#rawonly-info').innerText")
            browser.close()
        self.assertEqual(errors, [])
        self.assertIn("Acuan JP: belum ada untuk set ini", other)
        self.assertIn("ACUAN JP", head)                                                  # the header is upper-cased by the style sheet
        self.assertTrue(any("114/193" in r and "¥2.480" in r.replace(",", ".") and "Rp261.600" in r for r in rows), rows)
        self.assertTrue(any("115/193" in r and "¥30" in r and "habis" in r for r in rows), rows)
        self.assertFalse(any("117/193" in r and "¥" in r for r in rows))
        self.assertEqual(link, "https://yuyu-tei.jp/sell/poc/card/m02a/114")
        self.assertIn("Acuan JP: Yuyutei m02a (2 kartu ada harganya)", info)
        self.assertIn("¥1 = Rp105,5", kurs)                                                    # the rates are on the home page, not in each set's text
        self.assertIn("US$1 = Rp16.250", kurs)
        self.assertIn("diperbarui", kurs)

    def test_board_shows_the_egs_watch_summary_the_queue_and_marks_thin_data(self):
        from playwright.sync_api import sync_playwright
        export = make_egs_export(tempfile.mkdtemp())
        html = build_site.render(build_site.build(tiny_latest(), None, None, None, None, export), True)
        path = os.path.join(tempfile.mkdtemp(), "board.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        errors = []
        gif = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True, args=["--no-sandbox"] if os.geteuid() == 0 else [])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://**/*", lambda route: route.fulfill(status=200, content_type="image/gif", body=gif))
            page.goto("file://" + path)
            page.wait_for_timeout(300)
            ev = page.evaluate
            visible = ev("() => !document.querySelector('#egswatch').hidden")
            facts = ev("() => document.querySelector('#egswatch-facts').innerText")
            title = ev("() => document.querySelector('#egswatch-queue-title').innerText")
            page.click("#egswatch-queue summary")
            rows = ev("() => [...document.querySelectorAll('#egswatch-body tbody tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ').trim())")
            links = ev("() => [...document.querySelectorAll('#egswatch-body a')].map(a => a.href)")
            imgs = ev("() => document.querySelectorAll('#egswatch-body img').length")
            strips = ev("() => [...document.querySelectorAll('.grow small')].map(s => s.innerText)")
            browser.close()
        self.assertEqual(errors, [])
        self.assertTrue(visible)
        for want in ("12 kandidat ditemukan", "6 diterima", "3 ambigu", "3 ditolak"):
            self.assertIn(want, facts)
        self.assertIn("Antrean ambigu: 3 listing", title)
        self.assertEqual(len(rows), 3)
        self.assertTrue(any("salah rarity" in r and "Rarity di judul (SR)" in r and "Rp900.000" in r for r in rows), rows)
        self.assertTrue(all(l.startswith("https://toco.id/id/listing/") for l in links))
        self.assertEqual(imgs, 3)
        self.assertTrue(any("data terbatas · 1 listing" in s for s in strips), strips)

    def test_browser_probe_reports_listings_data_requests_and_serial(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_toco_browser.py")
        done = subprocess.run([sys.executable, script, self.site.base], capture_output=True, text=True,
                              timeout=180, env={**os.environ, "EGS_CHROME": chrome_path()})
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        text = done.stdout
        self.assertIn("listing terlihat=3", text)
        self.assertIn("/api/toco-api/search", text)
        self.assertIn("&page=2 -> listing=2 baru dibanding halaman 1=2", text)
        self.assertIn("EG12345", text)

    def test_data_probe_reports_request_results_images_and_text(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_toco_data.py")
        done = subprocess.run([sys.executable, script, self.site.base], capture_output=True, text=True,
                              timeout=180, env={**os.environ, "EGS_CHROME": chrome_path()})
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        text = done.stdout
        self.assertIn("2 kali ke /api/toco-api/search", text)
        self.assertIn("store-search", text)
        self.assertIn("total=2", text)
        self.assertIn("300x300", text)
        self.assertIn("Kondisi Bekas", text)
        self.assertIn("Deskripsi: kartu asli", text)

    def test_api_probe_finds_product_search_replays_it_and_reads_addetail(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_toco_api.py")
        done = subprocess.run([sys.executable, script, self.site.base, "2"], capture_output=True, text=True,
                              timeout=180, env={**os.environ, "EGS_CHROME": chrome_path()})
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        text = done.stdout
        self.assertIn("path=/store-search item=2", text)
        self.assertIn("path=/product-search item=3", text)
        self.assertIn("== 2. permintaan pencarian produk", text)
        self.assertIn("status=200 item=3 (di browser: 3)", text)
        self.assertIn("halaman berikutnya (offset+3): status=200 item=2 baru=2", text)
        self.assertIn("kunci adDetail", text)
        self.assertIn("/img/big.png", text)
        self.assertIn("CHINESE", text)
        self.assertIn("tidak ada alamat toco/... untuk diturunkan", text)


# ---- EGS slab watch -----------------------------------------------------------------------------

def egs_catalog_data():
    def card(set_, num, den, name, rarity, cid):
        return {"set": set_, "number": f"{num:03d}/{den}", "num": num, "den": den, "name": name, "rarity": rarity, "image": f"https://o/{cid}.png", "id": cid}
    cards = [card("MA3", 234, 193, "Pikachu ex", "SAR", 1), card("MA3", 223, 193, "Mega Charizard X ex", "SAR", 2),
             card("MA3", 211, 193, "Dreepy", "AR", 3), card("MA3", 212, 193, "Drakloak", "AR", 4),
             card("MA3", 54, 193, "Tapu Koko", None, 5), card("MA3", 54, 193, "Tapu Koko", None, 6), card("MA3", 54, 193, "Tapu Koko", None, 7),
             card("MA3", 100, 193, "Pikachu", None, 8), card("MA3", 150, 193, "Mimikyu", "RR", 9), card("MA3", 151, 193, "Mimikyu", "RR", 10),
             card("SV8A", 200, 187, "Leafeon ex", "SAR", 11), card("MA1", 128, 126, "Ivysaur", "AR", 12), card("MA1", 173, 126, "Mega Venusaur ex", "SAR", 13)]
    sets = [{"code": "MA3", "name": "Evolusi Mega Impian ex", "fetched": 10}, {"code": "SV8A", "name": "Festival Terastal ex", "fetched": 1},
            {"code": "MA1", "name": "Evolusi Mega", "fetched": 2}]
    return {"schema_version": 1, "sets": sets, "cards": cards}


EGS_CATALOG = egs_identity.Catalog(egs_catalog_data())


def verify_title(title, description="", price=500000, **kw):
    return egs_identity.verify({"title": title, "description": description, "ask": price, **kw.pop("listing", {})}, EGS_CATALOG, **kw)


def codes(v):
    return [r["code"] for r in v["reasons"]]


class EgsIdentity(unittest.TestCase):
    def test_everything_consistent_is_accepted_with_its_evidence(self):
        v = verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10")
        self.assertEqual((v["verdict"], v["reasons"]), ("accepted", []))
        i = v["identity"]
        self.assertEqual((i["name"], i["set_code"], i["card_number"], i["rarity"], i["grade"], i["company"], i["language"], i["identified_by"]),
                         ("Pikachu ex", "MA3", "234/193", "SAR", "10", "EGS", "ID", "nomor di judul"))
        self.assertEqual(egs_identity.card_key(i), "MA3|234/193|SAR|")

    def test_a_wrong_rarity_is_ambiguous_never_corrected_silently(self):
        v = verify_title("Pikachu ex SR 234/193 MA3 ID EGS 10")
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["rarity_mismatch"]))
        self.assertIn("SR", v["reasons"][0]["text"])
        self.assertIn("SAR", v["reasons"][0]["text"])
        self.assertEqual(codes(verify_title("Pikachu ex SR SAR 234/193 MA3 ID EGS 10")), ["rarity_multiple"])
        self.assertEqual(codes(verify_title("Pikachu ex 234/193 MA3 ID EGS 10")), [])           # no rarity written: the number settles it

    def test_language_needs_proof_and_foreign_text_is_rejected(self):
        self.assertEqual(verify_title("Rayquaza VMAX CSR EGS 10 CHINESE")["verdict"], "rejected")
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 EGS 10 Japanese")), ["foreign_language"])
        v = verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 bukan English")
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["language_conflict"]))
        v = verify_title("Leafeon ex SAR 200/187 SV8A EGS 10")                                  # no marker at all: not proof
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["language_unproven"]))
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 EGS 10")["identity"]["language_by"], "kode set MA (hanya dipakai kartu Indonesia)")
        v = verify_title("Leafeon ex SAR 200/187 SV8A EGS 10", photo={"language": "JP"})
        self.assertEqual((v["verdict"], codes(v)), ("rejected", ["foreign_language"]))
        # the first real photo reading: the title says Indonesia, the photo reader says EN and a number that no Indonesian set has
        v = verify_title("Charizard ex SAR Indonesia EGS10", photo={"is_slab": True, "company": "EGS", "grade": "10", "cert": "A0D960", "name": "Charizard EX",
                                                                    "number": "330/193", "rarity": None, "language": "EN"})
        self.assertEqual(v["verdict"], "ambiguous")
        self.assertIn("language_conflict", codes(v))
        self.assertEqual(verify_title("Pika egs 10", price=500000)["verdict"], "ambiguous")                                         # "pika" is Pikachu: a person or a photo decides, it is not thrown out
        self.assertEqual(codes(verify_title("Ijat EGS", price=500000)), ["no_pokemon_signal"])
        self.assertEqual(verify_title("Charizard ex SAR MA3 EGS10 Indo", photo={"language": "JP"})["verdict"], "ambiguous")        # an MA code is also a claim of Indonesian
        v = verify_title("Leafeon ex SAR 200/187 SV8A EGS 10", photo={"language": "ID"})
        self.assertEqual((v["verdict"], v["identity"]["language_by"]), ("accepted", "foto"))

    def test_the_grading_company_must_be_egs(self):
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 MA3 ID PSA 10")), ["other_grader"])
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID BGS 9.5")["verdict"], "rejected")
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 setara PSA 10")), ["company_conflict"])
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 clear slab not TAG Grading")["verdict"], "accepted")   # negated: not a TAG slab
        v = verify_title("Pikachu ex SAR 234/193 MA3 ID slab 10")
        self.assertEqual((v["verdict"], "no_egs" in codes(v)), ("ambiguous", True))
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 MA3 ID NM"))[0], "not_slab")
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID NM")["verdict"], "rejected")

    def test_titles_of_other_games_that_the_real_search_returned_are_rejected_not_queued(self):
        for title in ("Come On!! We\u2019ll Fight You !! - OPTCG - OP09 -020", "SUPER SAIYANS GOTENKS EGS 9", "Takato Matsuki EX2-056 P-R EGS 9"):
            v = verify_title(title)
            self.assertEqual((v["verdict"], codes(v)), ("rejected", ["not_pokemon"]), title)
        for title in ("Wanted Buggy GEM MINT 9.5 by EGS", "Gol D Roger PSEC* EGS 10 & Gold Don!! Roger"):
            v = verify_title(title)
            self.assertEqual((v["verdict"], codes(v)), ("rejected", ["no_pokemon_signal"]), title)
        self.assertEqual(verify_title("Dreepy EGS 10 ID")["verdict"], "ambiguous")                  # a catalog name is a signal
        self.assertEqual(verify_title("Pika egs 10")["verdict"], "ambiguous")                       # so is the nickname sellers use for Pikachu
        self.assertEqual(verify_title("Kartu EGS 10 pokemon langka")["verdict"], "ambiguous")        # so is the word Pokemon

    def test_a_listing_re_used_for_another_card_is_caught_by_its_address(self):
        v = verify_title("Mega Darkrai Ex ID EGS 10", listing={"slug": "mega-latias-ex-jp-egs-10-1785765419130-9475"})
        self.assertEqual((v["verdict"], "title_changed" in codes(v)), ("ambiguous", True))
        v = verify_title("Dreepy AR MA3 ID EGS 10", listing={"slug": "dreepy-ar-ma3-japan-egs-10-1785765419130-9475"})
        self.assertIn("title_changed", codes(v))                                                      # the address said Japan, the title now says nothing of it
        v = verify_title("Dreepy AR MA3 ID EGS 10 ready", listing={"slug": "dreepy-ar-ma3-id-egs-10-original-ready-1785765419130-9475"})
        self.assertEqual(v["verdict"], "accepted")                                                    # dropped filler words are not a change of card

    def test_egs_grading_ten_and_a_glued_grade_with_a_catalog_card_to_suggest(self):
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS Grading 10")["identity"]["grade"], "10")
        v = verify_title("Pikachu ex SAR Indonesia EGS10")
        self.assertEqual((v["verdict"], codes(v), v["identity"]["grade"]), ("ambiguous", ["number_missing"], "10"))
        self.assertEqual([s["card_number"] for s in v["suggestions"]], ["100/193", "234/193"])      # the Pikachu and the Pikachu ex that fit the words

    def test_sellers_who_glue_the_grade_to_the_company_are_read_too(self):
        for title, grade in (("Pikachu ex SAR 234/193 MA3 ID EGS10", "10"), ("Pikachu ex SAR 234/193 MA3 ID EGS9.5", "9.5"), ("Pikachu ex SAR 234/193 MA3 ID EGS-9", "9")):
            self.assertEqual((verify_title(title)["verdict"], verify_title(title)["identity"]["grade"]), ("accepted", grade), title)
        self.assertTrue(egs_watch.EGS_WORD.search("slab EGS10 pokemon"))
        self.assertFalse(egs_watch.EGS_WORD.search("legs 10"))
        self.assertFalse(egs_watch.EGS_WORD.search("EGSA 10"))

    def test_grades_are_separate_and_a_disagreement_is_ambiguous(self):
        self.assertEqual({verify_title(f"Pikachu ex SAR 234/193 MA3 ID EGS {g}")["identity"]["grade"] for g in ("10", "9.5", "9", "9,5")}, {"10", "9.5", "9"})
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID 9.5 EGS")["identity"]["grade"], "9.5")
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS Gem Mint 10")["identity"]["grade"], "10")
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10", "slab EGS 9.5 mint")), ["grade_conflict"])
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 EGS 9"))[0], "grade_conflict")
        self.assertEqual(codes(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS slab"))[0], "grade_missing")
        v = verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10", photo={"grade": "9"})
        self.assertEqual((v["verdict"], codes(v), v["identity"]["grade"]), ("ambiguous", ["photo_conflict"], None))
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS slab", photo={"grade": "10"})["identity"]["grade"], "10")

    def test_bundles_and_several_cards_are_never_one_card(self):
        v = verify_title("Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID")
        self.assertEqual((v["verdict"], "bundle" in codes(v)), ("ambiguous", True))
        self.assertIn("bundle", codes(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 bisa pilih opsi lain")))
        self.assertEqual(codes(verify_title("Pikachu ex 234/193 dan Dreepy 211/193 MA3 ID EGS 10"))[:1], ["number_multiple"])
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 stok 1 pcs")["verdict"], "accepted")   # "1 pcs" is not a bundle
        self.assertEqual(verify_title("Booster box MA3 ID EGS")["verdict"], "rejected")
        self.assertEqual(verify_title("One Piece OP05-119 EGS 10")["verdict"], "rejected")

    def test_a_missing_number_is_found_only_when_the_catalog_has_exactly_one_card(self):
        v = verify_title("Dreepy AR MA3 ID EGS 10")
        self.assertEqual((v["verdict"], v["identity"]["card_number"], v["identity"]["identified_by"]),
                         ("accepted", "211/193", "nama, set dan rarity (satu-satunya di katalog)"))
        v = verify_title("Mimikyu RR MA3 ID EGS 10")                                              # two Mimikyu RR in MA3
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["number_missing"]))
        self.assertEqual([s["card_number"] for s in v["suggestions"]], ["150/193", "151/193"])
        self.assertIn("2 kartu", v["reasons"][0]["text"])
        v = verify_title("Pikachu EGS 10 ID")                                                     # a name alone is never enough
        self.assertEqual((v["verdict"], codes(v)[-1]), ("ambiguous", "number_missing"))
        self.assertEqual(verify_title("Ivysaur AR #128 MA1 ID EGS 10")["identity"]["identified_by"], "nomor #128 dan set")
        self.assertEqual(verify_title("Ivysaur AR #129 MA1 ID EGS 10")["verdict"], "ambiguous")   # #129 is not in the catalog (and the name would not fit)
        self.assertEqual(verify_title("Leafeon SAR Festival Terastal ex ID EGS 10")["identity"]["card_number"], "200/187")   # set named, not coded
        v = verify_title("Dreepy EGS 10 ID MA3")                                                  # no rarity written: not enough
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["number_missing"]))

    def test_a_photo_can_supply_what_the_title_lacks_but_not_override_it(self):
        photo = {"is_slab": True, "company": "EGS", "grade": "10", "cert": "12345678", "number": "234/193", "rarity": "SAR", "language": "ID", "name": "Pikachu ex"}
        v = verify_title("Pikachu SAR MA3 EGS 10", photo=photo)
        self.assertEqual((v["verdict"], v["identity"]["card_number"], v["identity"]["identified_by"], v["identity"]["cert"]),
                         ("accepted", "234/193", "nomor di foto", "12345678"))
        v = verify_title("Pikachu SAR MA3 EGS 10 234/193", photo={**photo, "number": "223/193"})
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["photo_conflict"]))
        v = verify_title("Pikachu ex 234/193 MA3 ID EGS 10", photo={**photo, "rarity": "SR"})
        self.assertIn("photo_conflict", codes(v))
        v = verify_title("Pikachu ex 234/193 MA3 ID EGS 10 cert 99999999", photo={**photo, "cert": "12345678"})
        self.assertIn("photo_conflict", codes(v))
        self.assertEqual(verify_title("Pikachu SAR MA3 slab 10", photo={**photo, "company": "PSA"})["verdict"], "rejected")

    def test_the_catalog_must_agree_with_the_title(self):
        self.assertEqual(codes(verify_title("Charizard SAR 234/193 MA3 ID EGS 10")), ["name_mismatch"])      # 234/193 is Pikachu ex
        self.assertEqual(codes(verify_title("Arceus V SR AA S9 ID EGS 9.5 100/100"))[0], "set_unknown")
        self.assertIn("set S9 tidak ada di katalog", verify_title("Arceus V SR AA S9 ID EGS 9.5 100/100")["reasons"][0]["text"])
        self.assertEqual(codes(verify_title("Pikachu SAR 999/193 MA3 ID EGS 10"))[0], "card_not_in_catalog")
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 EGS 10 ID")["identity"]["set_code"], "MA3")      # set from the printed total
        v = verify_title("Pikachu ex SAR 234/777 EGS 10 ID")
        self.assertEqual((v["verdict"], codes(v)[0]), ("ambiguous", "set_unknown"))

    def test_foil_versions_a_bare_holo_and_a_named_pattern(self):
        v = verify_title("Tapu Koko MA3 054/193 EGS 9 holo ID")
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["variant_unclear"]))
        v = verify_title("Tapu Koko MA3 054/193 EGS 9 pokeball holo ID")
        self.assertEqual((v["verdict"], v["identity"]["variant"], egs_identity.card_key(v["identity"])), ("accepted", "poke_ball", "MA3|54/193|-|poke_ball"))
        self.assertEqual(verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10 holo")["verdict"], "accepted")   # this number has no foil versions

    def test_prices_a_reused_listing_and_the_card_the_bot_already_tracks(self):
        v = verify_title("Pikachu ex SAR 234/193 MA3 ID EGS 10", price=20000)
        self.assertEqual((v["verdict"], codes(v)), ("ambiguous", ["price_implausible"]))
        v = verify_title("Silvally AR MA5 EGS 10", listing={"slug": "pertarungan-penentuan-gladion-sar-ma5-egs-10-1790735079133-72e4"})
        self.assertIn("title_changed", codes(v))
        self.assertEqual(verify_title("Dreepy AR MA3 ID EGS 10", listing={"slug": "dreepy-ar-ma3-id-egs-10-1790675893834-b99c"})["verdict"], "accepted")
        tracked = {"name": "Mega Venusaur ex", "set_code": "MA1", "card_number": "173/126", "rarity": "SAR", "grade": "10"}
        v = verify_title("Mega Venusaur EGS 10 Evolusi Mega", tracked=tracked)                       # no number, no marker: trusted because the bot tracks it
        self.assertEqual((v["verdict"], v["identity"]["card_number"], v.get("tracked")), ("accepted", "173/126", True))
        self.assertTrue(v["disagreements"])                                                         # but what is missing is still shown

    def test_certificate_numbers_and_negations_are_read_from_the_text(self):
        ev = egs_identity.text_evidence("Pikachu EGS 10", "Sertifikat EGS no 12345678, grade 10")
        self.assertEqual(ev["cert"], "12345678")
        self.assertEqual(egs_identity.text_evidence("slab bukan PSA EGS 10")["companies"], ["EGS"])
        self.assertEqual(egs_identity.text_evidence("10/10 EGS 10 Pikachu 234/193")["numbers"], ["234/193"])   # "10/10" is a score


def egs_slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") + "-1790000000000-ab12"


def egs_item(title, price, store="s1", stock=1, description="", slug=None, photo=True, **extra):
    slug = slug or egs_slug(title)
    return {"slug": slug, "title": title, "description": description, "price": price, "price_special": None, "stock": stock, "city": "Kota Jambi",
            "primary_photo": f"https://img.example/{slug}.webp" if photo else None, "photos": [], "show": True, "archived": False, "deleted": False,
            "store_id": store, "store_name": "Toko " + store, **extra}


class FakeEgsToco:
    """toco's search as the watcher sees it: AND search over title and description, 'from' and 'size' paging, scripted failures."""

    def __init__(self, items):
        self.items, self.posts, self.script = list(items), [], []

    def post(self, body):
        b = body["body"]
        self.posts.append((b["query"], b["from"]))
        if self.script:
            step = self.script.pop(0)
            if step is not None:
                return step
        words = re.findall(r"[a-z0-9]+", b["query"].lower())
        hits = [i for i in self.items if all(w in re.findall(r"[a-z0-9]+", f"{i['title']} {i['description']}".lower()) for w in words)]
        return 200, json.dumps({"total": len(hits), "results": hits[b["from"]:b["from"] + b["size"]]}), None


EGS_HITS = {"grades": ["10", "9.5", "9"], "base_queries": ["EGS", "slab EGS"], "base_pages": 6, "hit_pages": 3, "tracked_pages": 2,
            "hits": ["Pikachu"], "deep_hits": [], "set_codes": []}


def make_egs_export(tmpdir):
    """A real export of the watcher over a small fake toco (the one used by the EgsWatch tests)."""
    case = EgsWatch("test_a_full_run_counts_dedupes_judges_and_explains")
    case.tmp = tempfile.TemporaryDirectory(dir=tmpdir)
    for name, data in (("catalog.json", egs_catalog_data()), ("hits.json", EGS_HITS)):
        with open(os.path.join(case.tmp.name, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    case.logs = []
    export, _ = case.go(FakeEgsToco(case.catalog_listings()))
    return export


def marks_catalog():
    def card(set_, n, den, name, rarity, cid, image=True):
        return {"set": set_, "number": f"{n:03d}/{den}", "num": n, "den": den, "name": name, "rarity": rarity, "image": f"https://o/{cid}.png" if image else None, "id": cid}
    return {"schema_version": 1, "sets": [{"code": "MA6", "product": "Booster Pack", "fetched": 6}, {"code": "AC3D", "product": "Starter Deck GX", "fetched": 2},
                                          {"code": "MA3", "product": "High Class Booster Pack", "fetched": 3}],
            "cards": [card("MA6", 2, 130, "Alolan Exeggutor", None, 1), card("MA6", 15, 130, "Fuecoco ex", "RR", 2),          # inside the set's own numbering: not a candidate
                      card("MA6", 189, 130, "Solgaleo GX", None, 3), card("MA6", 195, 130, "Arceus VSTAR", None, 4),            # special reprints above the printed total
                      card("MA6", 199, 130, "No Picture", None, 5, image=False),                                                  # nothing to look at
                      card("AC3D", 1, 172, "Celebi & Venusaur GX", None, 6), card("AC3D", 2, 172, "Pidgey", None, 7),           # a deck: no card has a rarity
                      card("MA3", 1, 193, "Pinsir", None, 8), card("MA3", 2, 193, "Rare", "SAR", 9)]}                            # a High Class set: unmarked is normal


class CardMarks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = os.path.join(self.tmp.name, "catalog.json")
        with open(self.catalog, "w", encoding="utf-8") as fh:
            json.dump(marks_catalog(), fh)
        self.marks = os.path.join(self.tmp.name, "marks.json")
        self.logs, self.seen = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **over):
        base = dict(catalog=self.catalog, marks=self.marks, sets=None, limit=None, again=False, dry_run=False)
        base.update(over)
        return argparse.Namespace(**base)

    def reader(self, answers):
        def read(url):
            self.seen.append(url)
            return answers.get(url)
        return read

    def test_candidates_are_unmarked_special_cards_and_unmarked_decks_only(self):
        got = [(c["set"], c["number"]) for c in read_card_marks.candidates(marks_catalog())]
        self.assertEqual(got, [("MA6", "189/130"), ("MA6", "195/130"), ("AC3D", "001/172"), ("AC3D", "002/172")])
        self.assertEqual([c["number"] for c in read_card_marks.candidates(marks_catalog(), "ma6")], ["189/130", "195/130"])

    def test_the_answers_of_a_model_are_read_strictly_and_two_readers_must_agree(self):
        self.assertEqual(read_card_marks.parse_mark('{"rarity": "rrr", "alternate_art": false}'), {"rarity": "RRR", "alt_art": False})
        self.assertEqual(read_card_marks.parse_mark('x {"rarity": null, "alternate_art": null} y'), {"rarity": None, "alt_art": None})
        self.assertEqual(read_card_marks.parse_mark('{"rarity": "Super Duper"}')["rarity"], "?")        # not a rarity code
        self.assertIsNone(read_card_marks.parse_mark("no json here"))
        sr = {"rarity": "SR", "alt_art": True}
        self.assertEqual(read_card_marks.agree([sr, dict(sr)]), {"rarity": "SR", "alt_art": True})
        self.assertEqual(read_card_marks.agree([sr, {"rarity": "SR", "alt_art": False}]), {"rarity": "SR"})   # the rarity agrees, the alternate-art flag does not: not claimed
        self.assertEqual(read_card_marks.agree([sr, {"rarity": "RRR", "alt_art": True}]), {})
        self.assertEqual(read_card_marks.agree([sr, {"rarity": None, "alt_art": None}]), {})
        self.assertEqual(read_card_marks.agree([{"rarity": "?", "alt_art": None}, {"rarity": "?", "alt_art": None}]), {})

    def test_what_both_readers_agree_on_is_written_and_nothing_else(self):
        answers = {"https://o/3.png": {"rarity": "RRR", "alt_art": False}, "https://o/4.png": {"rarity": "SR", "alt_art": True}}
        count = read_card_marks.run(self.args(sets="MA6"), self.reader(answers), log=self.logs.append)
        self.assertEqual(count, {"read": 2, "written": 2, "skipped": 0})
        with open(self.marks, encoding="utf-8") as fh:
            cards = json.load(fh)["cards"]
        self.assertEqual((cards["MA6|189/130"]["rarity"], cards["MA6|195/130"]["rarity"], cards["MA6|195/130"]["alt_art"]), ("RRR", "SR", True))
        self.assertEqual(read_card_marks.label(cards["MA6|195/130"]), "SR AA")
        self.assertEqual(read_card_marks.label(cards["MA6|189/130"]), "RRR")
        self.assertIn("dua model sepakat", cards["MA6|189/130"]["by"])
        self.seen.clear()                                                                      # a second run skips what is already known, and calls nothing
        self.assertEqual(read_card_marks.run(self.args(sets="MA6"), self.reader(answers), log=self.logs.append)["read"], 0)
        self.assertEqual(self.seen, [])

    def test_disagreement_failure_and_dry_run_write_nothing_and_manual_entries_are_never_overwritten(self):
        answers = {"https://o/3.png": {}, "https://o/4.png": None}                              # the readers disagreed on one picture; the other could not be read at all
        count = read_card_marks.run(self.args(sets="MA6"), self.reader(answers), log=self.logs.append)
        self.assertEqual(count, {"read": 2, "written": 0, "skipped": 2})
        self.assertFalse(os.path.exists(self.marks))
        self.assertEqual(read_card_marks.run(self.args(dry_run=True), self.reader({}), log=self.logs.append)["read"], 0)
        self.assertEqual(self.seen, ["https://o/3.png", "https://o/4.png"])                    # the dry run called no reader
        with open(self.marks, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "cards": {"MA6|189/130": {"rarity": "SR", "alt_art": True, "by": "manual"}}}, fh)
        answers = {"https://o/3.png": {"rarity": "RR", "alt_art": False}, "https://o/4.png": {"rarity": "SR", "alt_art": False}}
        read_card_marks.run(self.args(sets="MA6", again=True), self.reader(answers), log=self.logs.append)
        with open(self.marks, encoding="utf-8") as fh:
            cards = json.load(fh)["cards"]
        self.assertEqual((cards["MA6|189/130"]["rarity"], cards["MA6|189/130"]["by"]), ("SR", "manual"))   # even with --again
        self.assertEqual(cards["MA6|195/130"]["rarity"], "SR")

    def test_only_reads_exactly_the_named_cards_and_the_log_shows_what_each_reader_said(self):
        answers = {"https://o/3.png": read_card_marks.combine_marks([{"rarity": None, "alt_art": None}, {"rarity": "SR", "alt_art": True}]),   # they disagree
                   "https://o/4.png": read_card_marks.combine_marks([{"rarity": "SR", "alt_art": True}, {"rarity": "SR", "alt_art": True}])}
        count = read_card_marks.run(self.args(sets="MA6", only="189/130, 195/130"), self.reader(answers), log=self.logs.append)
        self.assertEqual(count, {"read": 2, "written": 1, "skipped": 1})
        text = "\n".join(self.logs)
        self.assertIn("bacaan: - | SR/AA", text)
        self.assertNotIn("readings", open(self.marks, encoding="utf-8").read())                  # the log line is not stored in the file
        self.seen.clear()
        read_card_marks.run(self.args(only="001/172"), self.reader({}), log=self.logs.append)    # a deck card named by number, with no set given
        self.assertEqual(self.seen, ["https://o/6.png"])
        self.assertEqual(read_card_marks.norm_number("001/130"), "1/130")

    def test_the_block_above_the_printed_total_is_flagged_as_reprints_without_a_model_and_nothing_else_is_touched(self):
        flagged = read_card_marks.mark_reprints(marks_catalog(), marks := {"cards": {"MA6|189/130": {"rarity": "RRR", "by": "manual"}, "MA6|195/130": {"reprint": False}}}, "MA6", "30th")
        self.assertEqual(flagged, 2)                                                          # 189 (named by the owner) gains the flag and so does 199; 195 already says it is not one
        self.assertEqual(marks["cards"]["MA6|199/130"]["reprint"], "30th")                    # a flag needs no picture
        self.assertEqual(marks["cards"]["MA6|189/130"], {"rarity": "RRR", "by": "manual", "reprint": "30th"})
        self.assertIs(marks["cards"]["MA6|195/130"]["reprint"], False)
        self.assertNotIn("MA6|002/130", marks["cards"])                                       # inside the set's own numbering
        self.assertNotIn("MA6|015/130", marks["cards"])
        self.assertFalse(any(k.startswith(("AC3D", "MA3")) for k in marks["cards"]))          # decks and other sets are not part of it
        self.assertEqual(read_card_marks.main(["--catalog", self.catalog, "--marks", self.marks, "--mark-reprints"]), 1)            # needs --sets
        self.assertEqual(read_card_marks.main(["--catalog", self.catalog, "--marks", self.marks, "--sets", "MA6", "--mark-reprints", "--reprint-label", "30th"]), 0)
        with open(self.marks, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(sorted(saved["cards"]), ["MA6|189/130", "MA6|195/130", "MA6|199/130"])
        self.assertTrue(saved["about"].startswith("Rarity yang tercetak"))                    # the explanation is written with the file
        self.assertTrue(all(c["reprint"] == "30th" for c in saved["cards"].values()))

    def test_the_board_labels_reprints_and_shows_the_printed_rarity_from_the_marks_given_to_the_build(self):
        raw, catalog = board_with_official_catalog()
        catalog["cards"] += [{"set": "MA3", "number": "150/193", "num": 150, "den": 193, "name": "Reprint Card", "rarity": None, "image": "https://official.example/150.png", "id": 150}]
        marks = {"MA3|150/193": {"rarity": "SR", "alt_art": True, "reprint": "30th"}}
        data = build_site.build(tiny_latest(), raw, catalog, marks=marks)
        row = next(c for c in data["raw_cards"] if c["number"] == "150/193")
        self.assertEqual((row["rarity"], row["reprint"], row["basis"]), ("SR AA", "30th", None))                    # no base price: it is above the unmarked numbering rule
        plain = next(c for c in data["raw_cards"] if c["number"] == "114/193")
        self.assertIsNone(plain["reprint"])
        self.assertIsNone(next(c for c in build_site.build(tiny_latest(), raw, catalog, marks={})["raw_cards"] if c["number"] == "150/193")["reprint"])

    def test_the_main_command_wants_two_models(self):
        self.assertEqual(read_card_marks.main(["--catalog", self.catalog, "--marks", self.marks, "--model", "gpt-4.1"]), 1)

    def test_the_board_shows_the_printed_rarity_instead_of_nothing(self):
        cards = marks_catalog()["cards"]
        marks = {"MA6|189/130": {"rarity": "RRR"}, "MA6|195/130": {"rarity": "SR", "alt_art": True}, "MA6|002/130": {"rarity": "C"}}
        index = build_site.official_index(cards, marks_catalog()["sets"], marks=marks)
        self.assertEqual(index[("ma6", "189/130", "")]["rarity"], "RRR")
        self.assertEqual(index[("ma6", "195/130", "")]["rarity"], "SR AA")
        self.assertEqual(index[("ma6", "15/130", "")]["rarity"], "RR")                      # the official rarity always wins over a mark
        self.assertEqual(index[("ma6", "2/130", "")]["rarity"], "C")
        self.assertIsNone(build_site.official_index(cards, marks_catalog()["sets"], marks={})[("ma6", "189/130", "")]["rarity"])


class EgsWatch(unittest.TestCase):
    T0 = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        for name, data in (("catalog.json", egs_catalog_data()), ("hits.json", EGS_HITS)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        self.latest = None
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **over):
        d = self.tmp.name
        base = dict(db=os.path.join(d, "w.sqlite"), out=os.path.join(d, "out.json"), latest=os.path.join(d, "latest.json"), catalog=os.path.join(d, "catalog.json"),
                    hits=os.path.join(d, "hits.json"), size=45, delay=0, budget=200, min_share=0.2, cache_dir=None, cache_ttl=0, recheck_limit=50,
                    no_seed=False, dry_run=False, photo_budget=0)
        base.update(over)
        return argparse.Namespace(**base)

    def write_latest(self, listings=(), candidates=()):
        row = lambda ask, at: {"observed_at": at, "asking_idr": ask, "in_stock": True}
        out = []
        for name, set_code, number, rarity, grade, url, ask in listings:
            days = [{"date_wib": f"2026-10-0{n}", **row(ask, f"2026-10-0{n}T01:00:00+00:00")} for n in (5, 4)]
            out.append({"name": name, "set_code": set_code, "card_number": number, "rarity": rarity, "grade": grade, "url": url,
                        "latest": row(ask, "2026-10-05T22:30:00+00:00"), "previous_day": days[0], "change_from_previous_day_idr": 0, "daily_latest": days})
        d = {"generated_at": "2026-10-06T00:10:00+00:00", "coverage": {}, "listings": out,
             "discovery": {"generated_at": "2026-10-06T00:00:00+00:00", "candidates": [{"url": u, "search_text": s, "grade_hint": "10", "tracked": False} for u, s in candidates]}}
        with open(os.path.join(self.tmp.name, "latest.json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def go(self, fake, now=None, status=lambda url: 404, photo_fn=None, **over):
        cfg = self.cfg(**over)
        return egs_watch.run(cfg, fake.post, status, photo_fn, now=now or self.T0, sleep=lambda s: None, log=self.logs.append)

    # -- discovery
    def test_pages_are_followed_until_the_results_run_out(self):
        fake = FakeEgsToco([egs_item(f"Pikachu ex SAR 234/193 MA3 ID EGS 10 copy{i}", 400000 + i) for i in range(100)])
        toco = egs_watch.Toco(fake.post, sleep=lambda s: None)
        items, diag, blocked = egs_watch.discover(toco, [("EGS", 15)], 45, 0, 100, 0.2, sleep=lambda s: None, log=self.logs.append)
        self.assertEqual((len(items), diag[0]["pages"], diag[0]["stopped"], blocked), (100, 3, "hasil habis", False))
        self.assertEqual([p[1] for p in fake.posts], [0, 45, 90])

    def test_a_repeated_page_stops_the_query(self):
        page = [egs_item(f"Pikachu EGS 10 ID {i}", 400000) for i in range(45)]
        calls = []

        def post(body):
            calls.append(body["body"]["from"])
            return 200, json.dumps({"total": 900, "results": page}), None
        items, diag, _ = egs_watch.discover(egs_watch.Toco(post, sleep=lambda s: None), [("EGS", 15)], 45, 0, 100, 0.2, sleep=lambda s: None, log=self.logs.append)
        self.assertEqual((len(items), diag[0]["pages"], diag[0]["stopped"]), (45, 2, "halaman berulang"))

    def test_a_query_stops_when_its_pages_stop_being_about_egs(self):
        fake = FakeEgsToco([egs_item(f"Pikachu EGS 10 ID a{i}", 400000) for i in range(45)] + [egs_item(f"Pikachu kartu raw ID b{i}", 5000, description="EGS") for i in range(0)]
                           + [egs_item(f"Pikachu raw b{i}", 5000) for i in range(100)])
        toco = egs_watch.Toco(fake.post, sleep=lambda s: None)
        items, diag, _ = egs_watch.discover(toco, [("Pikachu", 15)], 45, 0, 100, 0.2, sleep=lambda s: None, log=self.logs.append)
        self.assertEqual((diag[0]["pages"], diag[0]["stopped"]), (2, "halaman tidak lagi tentang EGS"))

    def test_the_request_budget_is_a_hard_limit(self):
        fake = FakeEgsToco([egs_item(f"Pikachu EGS 10 ID {i}", 400000) for i in range(500)])
        toco = egs_watch.Toco(fake.post, sleep=lambda s: None)
        items, diag, _ = egs_watch.discover(toco, [("EGS", 15), ("Pikachu", 15)], 45, 0, 4, 0.2, sleep=lambda s: None, log=self.logs.append)
        self.assertEqual((len(fake.posts), diag[-1]["stopped"]), (4, "anggaran permintaan habis"))

    def test_transient_errors_are_retried_with_growing_waits_and_a_block_is_not(self):
        waits = []
        fake = FakeEgsToco([egs_item("Pikachu EGS 10 ID", 400000)])
        fake.script = [(429, "slow down", "5"), (503, "", None)]
        toco = egs_watch.Toco(fake.post, sleep=waits.append, log=self.logs.append)
        self.assertEqual(len(toco.search("EGS", 0, 45)["results"]), 1)
        self.assertEqual((toco.requests, toco.ok, toco.retried), (3, 1, 2))
        self.assertEqual(waits, [5, 8])                                              # the site asked for 5 s (more than the graded 3 s), then the graded 8 s
        fake.script = [(403, "Forbidden", None)]
        toco = egs_watch.Toco(fake.post, sleep=waits.append, log=self.logs.append)
        with self.assertRaises(egs_watch.Blocked):
            toco.search("EGS", 0, 45)
        self.assertEqual((toco.requests, toco.blocked is not None), (1, True))        # no retry, no way around it
        fake.script = [(200, "<html>Just a moment... captcha</html>", None)]
        with self.assertRaises(egs_watch.Blocked):
            egs_watch.Toco(fake.post, sleep=waits.append).search("EGS", 0, 45)
        fake.script = [(500, "", None)] * 4
        toco = egs_watch.Toco(fake.post, sleep=waits.append)
        with self.assertRaises(egs_watch.SearchFailed):
            toco.search("EGS", 0, 45)
        self.assertEqual((toco.requests, toco.failed), (4, 1))

    def test_a_search_page_is_cached_for_a_while(self):
        fake = FakeEgsToco([egs_item("Pikachu EGS 10 ID", 400000)])
        clock = [1000.0]
        toco = egs_watch.Toco(fake.post, cache_dir=os.path.join(self.tmp.name, "cache"), ttl=600, sleep=lambda s: None, clock=lambda: clock[0])
        toco.search("EGS", 0, 45)
        os.utime(os.path.join(self.tmp.name, "cache", os.listdir(os.path.join(self.tmp.name, "cache"))[0]), (1000, 1000))
        clock[0] = 1300
        toco.search("EGS", 0, 45)
        self.assertEqual((len(fake.posts), toco.cached), (1, 1))
        clock[0] = 1700                                                               # older than the 10 minutes: fetched again
        toco.search("EGS", 0, 45)
        self.assertEqual(len(fake.posts), 2)

    def test_queries_cover_the_base_words_tracked_cards_hits_grades_and_the_old_candidates(self):
        cfg = dict(EGS_HITS, deep_hits=["Pikachu"], set_codes=["MA3"])
        tracked = [{"name": "Mega Venusaur ex", "set": "MA1", "number": "173/126", "rarity": "SAR"}]
        qs = [q for q, _ in egs_watch.build_queries(cfg, tracked, ["Dreepy"], ["Arceus V SR AA S9 ID EGS 9.5"])]
        for want in ("EGS", "slab EGS", "Mega Venusaur ex 173/126 egs", "Pikachu EGS", "Pikachu slab EGS", "Pikachu EGS 10", "Pikachu EGS 9.5", "MA3 EGS", "Dreepy EGS", "Arceus V SR AA S9 ID EGS 9.5"):
            self.assertIn(want, qs)
        self.assertEqual(len(qs), len({q.lower() for q in qs}))
        self.assertEqual(egs_watch.title_of("Sisa 1 Arceus V SR AA S9 ID EGS 9.5 Rp600.000 Kota Jambi"), "Arceus V SR AA S9 ID EGS 9.5")

    # -- a whole run
    def catalog_listings(self):
        return [
            egs_item("Pikachu ex SAR 234/193 MA3 ID EGS 10", 2500000, "s1"),
            egs_item("Pikachu ex SAR 234/193 MA3 ID EGS 10 ready", 2700000, "s2", description="Sertifikat EGS no 11112222"),
            egs_item("Pikachu ex SAR 234/193 MA3 ID EGS 10 repost", 2650000, "s2"),                      # the same seller again: one offer
            egs_item("Pikachu ex SAR 234/193 MA3 ID EGS 10 lagi", 2600000, "s3", description="Sertifikat EGS no 11112222"),   # the same certificate: one slab
            egs_item("Pikachu ex SAR 234/193 MA3 ID EGS 9.5", 1500000, "s1"),                           # another grade
            egs_item("Dreepy AR MA3 ID EGS 10", 200000, "s2"),
            egs_item("Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity", 900000, "s4", slug="pikachu-ex-sr-234-193-ma3-id-egs-10-salah-rarity-1790000000000-ab12"),
            egs_item("Pikachu ex SAR 234/193 MA3 ID PSA 10 bukan EGS", 2000000, "s5"),
            egs_item("Pikachu EGS 10 ID", 700000, "s6"),                                                # no number
            egs_item("Rayquaza VMAX CSR EGS 10 CHINESE", 1800000, "s7"),
            egs_item("Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID", 3000000, "s8"),
            egs_item("Booster box MA3 ID EGS promo", 900000, "s9"),
            egs_item("Gengar VMAX CSR", 150000, "s10")]                                                 # not EGS at all

    def test_a_full_run_counts_dedupes_judges_and_explains(self):
        fake = FakeEgsToco(self.catalog_listings())
        export, code = self.go(fake)
        self.assertEqual(code, 0)
        s = export["summary"]
        self.assertEqual((s["candidates_found"], s["accepted_total"], s["ambiguous"], s["rejected_this_run"]), (12, 6, 3, 3))
        self.assertEqual(s["rejected_reasons"], {"other_grader": 1, "foreign_language": 1, "product": 1})
        self.assertEqual(export["run"]["results_seen"], 12)                     # the search never returned the one listing that does not say EGS
        cards = {(c["name"], g["grade"]): g for c in export["cards"] for g in c["grades"]}
        pika10 = cards[("Pikachu ex", "10")]
        self.assertEqual((pika10["n"], pika10["sellers"], pika10["min"], pika10["max"], pika10["median"], pika10["mean"], pika10["limited"]),
                         (2, 2, 2500000, 2600000, 2550000, 2550000, True))     # s1; and one slab for s2's cert listing, its repost and s3's listing with the same cert
        self.assertEqual(cards[("Pikachu ex", "9.5")]["n"], 1)                  # a grade of its own
        self.assertEqual(cards[("Pikachu ex", "9.5")]["label"], "data terbatas")
        self.assertEqual(cards[("Dreepy", "10")]["limited"], True)
        excluded = {l["title"]: l["excluded"] for l in export["listings"] if l["excluded"]}
        self.assertEqual(len(excluded), 2)                                      # the cert twin of s2 and s2's repost
        self.assertTrue(all(v.startswith("duplicate:") for v in excluded.values()))
        queue = {q["title"]: q for q in export["queue"]}
        self.assertEqual(set(queue), {"Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity", "Pikachu EGS 10 ID", "Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID"})
        wrong = queue["Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity"]
        self.assertEqual(([r["code"] for r in wrong["reasons"]], wrong["photo"].startswith("https://img.example/")), (["rarity_mismatch"], True))
        self.assertTrue(queue["Pikachu EGS 10 ID"]["suggestions"])               # catalog cards that fit the name, for the reviewer
        self.assertEqual(egs_watch.validate(export), [])
        for l in export["listings"]:                                            # evidence on every accepted listing
            self.assertTrue(l["url"].startswith("https://toco.id/id/listing/") and l["photo"] and l["asking_idr"] and l["last_ok"] and l["latest"] and l["seller"])
        self.assertEqual(s["sellers_unique"], 3)                                 # s1, s3 and s2 (whose Dreepy is counted)
        with open(self.cfg().out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["summary"], s)

    def test_the_old_watchlist_and_its_history_are_kept_and_the_old_candidates_are_judged(self):
        tracked = [("Mega Venusaur ex", "MA1", "173/126", "SAR", "10", "https://toco.id/id/listing/mega-venusaur-ex-sar-173-ma1-egs-10-1790389583180-2c05", 650000)]
        self.write_latest(tracked, [("https://toco.id/id/listing/dreepy-ar-ma3-id-egs-10-1790000000000-ab12", "Sisa 1 Dreepy AR MA3 ID EGS 10 Rp200.000 Kota Jambi")])
        fake = FakeEgsToco([egs_item("Mega Venusaur ex SAR 173/126 MA1 ID EGS 10", 640000, "s1", slug="mega-venusaur-ex-sar-173-ma1-egs-10-1790389583180-2c05"),
                            egs_item("Dreepy AR MA3 ID EGS 10", 200000, "s2")])
        export, code = self.go(fake)
        by = {l["url"]: l for l in export["listings"]}
        old = by[tracked[0][5]]
        self.assertEqual((old["origin"], old["asking_idr"], [d["date_wib"] for d in old["daily_latest"]][-2:]), ("bot", 640000, ["2026-10-05", "2026-10-04"]))
        self.assertEqual(len(old["daily_latest"]), 3)                           # the two imported days and today's check
        self.assertEqual(export["summary"]["accepted_existing"], 1)
        self.assertEqual(export["summary"]["accepted_auto"], 1)                  # the old pending candidate, judged by the new rules
        self.assertTrue(any(q[0].startswith("Dreepy AR MA3 ID EGS 10") for q in fake.posts))     # it was searched for by its own title
        self.assertEqual(old["change_from_previous_day_idr"], 640000 - 650000)

    def test_a_listing_of_the_old_bot_is_looked_up_by_the_words_of_its_address(self):
        url = "https://toco.id/id/listing/egs-10-mega-venusaur-ex-sar-173-ma1-pokemon-evolusi-mega-id-1790389583180-2c05"
        self.write_latest([("Mega Venusaur ex", "MA1", "173/126", "SAR", "10", url, 650000)])
        fake = FakeEgsToco([egs_item("EGS 10 Mega Venusaur ex SAR 173 MA1 Pokemon Evolusi Mega ID", 640000, "s1", slug=egs_watch.slug_of(url))])
        cfg = self.cfg()
        db = egs_watch.open_db(cfg.db)
        egs_watch.seed(db, json.load(open(cfg.latest)), self.T0, log=self.logs.append)
        row = db.execute("SELECT * FROM listing").fetchone()
        self.assertEqual(egs_watch.recheck_query(row), "egs 10 mega venusaur ex sar 173 ma1 pokemon evolusi mega id")
        toco = egs_watch.Toco(fake.post, sleep=lambda s: None)
        count = egs_watch.recheck(db, toco, lambda u: 404, set(), EGS_CATALOG, egs_watch.tracked_by_url(db), None, [0], self.T0, 10, 0, lambda s: None, self.logs.append)
        self.assertEqual(dict(count)["ok"], 1)
        self.assertEqual(db.execute("SELECT ask, status FROM listing").fetchone()[:], (640000, "ok"))
        db.close()

    def test_statuses_unavailable_unreachable_gone_and_missing_are_different_things(self):
        titles = ["Pikachu ex SAR 234/193 MA3 ID EGS 10", "Dreepy AR MA3 ID EGS 10", "Drakloak AR MA3 ID EGS 10", "Ivysaur AR #128 MA1 ID EGS 10", "Tapu Koko MA3 054/193 ID EGS 9"]
        fake = FakeEgsToco([egs_item(t, 100000 + 1000 * i, f"s{i}") for i, t in enumerate(titles)])
        first, _ = self.go(fake)
        self.assertEqual(first["summary"]["accepted_total"], 5)
        # six hours later: #0 sold out (stock 0), #1 and #2 and #3 vanished from the search, #4 unchanged
        fake.items[0]["stock"] = 0
        gone = [fake.items[i] for i in (1, 2, 3)]
        fake.items = [fake.items[0], fake.items[4]]
        pages = {egs_slug(titles[1]): 404, egs_slug(titles[2]): 200, egs_slug(titles[3]): 0}
        second, code = self.go(fake, now=self.T0 + timedelta(hours=6), status=lambda url: pages[slug_of_url(url)])
        by = {l["title"]: l for l in second["listings"]}
        self.assertEqual({t: by[t]["status"] for t in titles}, {titles[0]: "unavailable", titles[1]: "gone", titles[2]: "missing_from_search",
                                                                titles[3]: "fetch_failed", titles[4]: "ok"})
        self.assertEqual(by[titles[0]]["excluded"], "unavailable")
        self.assertEqual(by[titles[1]]["excluded"], "status:gone")
        self.assertIsNone(by[titles[2]]["excluded"])                              # not in the results, but the page exists and the last check is fresh: still counted
        self.assertIsNone(by[titles[3]]["excluded"])
        self.assertEqual(second["summary"]["status"], {"unavailable": 1, "gone": 1, "missing_from_search": 1, "fetch_failed": 1, "ok": 1})
        # three days on, with no good check since: shown, no longer counted
        third, _ = self.go(fake, now=self.T0 + timedelta(days=3), status=lambda url: 200)
        by = {l["title"]: l for l in third["listings"]}
        self.assertEqual(by[titles[2]]["status"], "missing_from_search")
        self.assertEqual(by[titles[2]]["excluded"], "status:missing_from_search")
        self.assertFalse(by[titles[2]]["counted"])
        for l in gone:                                                              # a vanished listing is never called sold
            self.assertNotIn("terjual", json.dumps(by.get(l["title"], {}), ensure_ascii=False).lower())

    def test_a_listing_is_counted_only_while_its_last_good_check_is_recent(self):
        row = {"verdict": "accepted", "card_key": "MA3|211/193|AR|", "grade": "10", "active": 1, "ask": 200000, "status": "ok", "last_ok": self.T0.isoformat()}
        self.assertTrue(egs_watch.counts_now(row, self.T0 + timedelta(hours=47)))
        self.assertFalse(egs_watch.counts_now(row, self.T0 + timedelta(hours=49)))          # still "ok" on paper, but nobody has looked for two days
        self.assertFalse(egs_watch.counts_now({**row, "status": "unavailable"}, self.T0))
        self.assertFalse(egs_watch.counts_now({**row, "status": "gone"}, self.T0))
        self.assertFalse(egs_watch.counts_now({**row, "verdict": "ambiguous"}, self.T0))
        self.assertFalse(egs_watch.counts_now({**row, "last_ok": None}, self.T0))

    def test_price_history_changes_and_freshness(self):
        fake = FakeEgsToco([egs_item("Dreepy AR MA3 ID EGS 10", 200000, "s2")])
        self.go(fake)
        fake.items[0]["price"] = 180000
        export, _ = self.go(fake, now=self.T0 + timedelta(hours=6))
        l = export["listings"][0]
        self.assertEqual((l["asking_idr"], l["latest"]["asking_idr"]), (180000, 180000))
        self.assertEqual(export["summary"]["price_changes_24h"], 1)
        self.assertEqual(export["summary"]["price_changes"][0]["from"], 200000)
        self.assertEqual((export["summary"]["price_changes"][0]["to"], export["summary"]["freshness"]["fresh_8h"]), (180000, 1))
        self.assertEqual(len(l["daily_latest"]), 1)                                # both observations are on the same WIB day: one entry, the latest

    def test_extreme_prices_are_shown_but_not_counted(self):
        items = [egs_item(f"Dreepy AR MA3 ID EGS 10 v{i}", p, f"s{i}") for i, p in enumerate((200000, 210000, 190000, 205000))]
        items.append(egs_item("Dreepy AR MA3 ID EGS 10 mahal", 5000000, "s9"))
        export, _ = self.go(FakeEgsToco(items))
        g = export["cards"][0]["grades"][0]
        self.assertEqual((g["n"], g["max"], g["limited"]), (4, 210000, False))
        self.assertEqual([l["excluded"] for l in export["listings"] if l["asking_idr"] == 5000000], ["outlier"])
        self.assertEqual(egs_watch.validate(export), [])

    def test_a_source_that_fails_never_overwrites_the_last_good_file(self):
        fake = FakeEgsToco(self.catalog_listings())
        good, _ = self.go(fake)
        out = self.cfg().out
        with open(out, "rb") as fh:
            before = fh.read()
        dead = FakeEgsToco([])
        dead.script = [(403, "Forbidden", None)] * 400
        export, code = self.go(dead, now=self.T0 + timedelta(hours=6))
        self.assertEqual((export, code), (None, 2))
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertTrue(any("TIDAK diubah" in l for l in self.logs))
        flaky = FakeEgsToco(self.catalog_listings())
        flaky.script = [(500, "", None)] * 400
        export, code = self.go(flaky, now=self.T0 + timedelta(hours=6))
        self.assertEqual((export, code), (None, 2))
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), before)
        db = sqlite3.connect(self.cfg().db)
        try:
            self.assertEqual({r[0] for r in db.execute("SELECT status FROM listing")}, {"ok"})        # nothing was marked missing because of an error
        finally:
            db.close()

    def test_a_block_half_way_keeps_what_was_learned_and_says_so(self):
        fake = FakeEgsToco(self.catalog_listings())
        fake.script = [None, (403, "Forbidden", None)]
        export, code = self.go(fake)
        self.assertEqual(code, 2)
        self.assertFalse(export["run"]["source_ok"])
        self.assertIn("pembatasan akses", export["run"]["blocked"])
        self.assertGreater(export["summary"]["accepted_total"], 0)
        self.assertEqual(export["run"]["recheck"], {})                             # no one-by-one lookups after a block

    def test_an_invalid_result_is_not_written(self):
        fake = FakeEgsToco(self.catalog_listings())
        real = egs_watch.build_export

        def broken(db, now, run_info):
            d = real(db, now, run_info)
            d["listings"][0]["counted"] = True
            d["summary"]["listings_counted"] += 5
            return d
        egs_watch.build_export = broken
        try:
            export, code = self.go(fake)
        finally:
            egs_watch.build_export = real
        self.assertEqual((export, code), (None, 3))
        self.assertFalse(os.path.exists(self.cfg().out))

    def test_a_dry_run_changes_nothing_on_disk(self):
        fake = FakeEgsToco(self.catalog_listings())
        export, code = self.go(fake, dry_run=True)
        self.assertEqual(code, 0)
        self.assertGreater(export["summary"]["accepted_total"], 0)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["catalog.json", "hits.json"])
        self.assertFalse(os.path.exists(self.cfg().out))
        self.assertFalse(os.path.exists(self.cfg().db))

    def test_a_photo_reader_settles_a_missing_number_once_and_within_its_budget(self):
        fake = FakeEgsToco([egs_item("Pikachu SAR MA3 ID EGS 10", 2500000, "s1"), egs_item("Mimikyu RR MA3 ID EGS 10", 100000, "s2")])
        seen = []

        def reader(url):
            seen.append(url)
            return {"is_slab": True, "company": "EGS", "grade": "10", "cert": None, "number": "234/193" if "pikachu" in url else None,
                    "rarity": "SAR" if "pikachu" in url else None, "language": "ID", "name": None}
        export, _ = self.go(fake, photo_fn=reader, photo_budget=5)
        self.assertEqual(sorted(l["title"] for l in export["listings"]), ["Pikachu SAR MA3 ID EGS 10"])
        self.assertEqual(export["listings"][0]["identity"]["identified_by"], "nomor di foto")
        self.assertEqual([q["title"] for q in export["queue"]], ["Mimikyu RR MA3 ID EGS 10"])
        self.assertEqual(len(seen), 2)
        self.go(fake, photo_fn=reader, photo_budget=5, now=self.T0 + timedelta(hours=6))
        self.assertEqual(len(seen), 2)                                              # each photo is read once, then remembered
        fake2 = FakeEgsToco([egs_item(f"Mimikyu RR MA3 ID EGS 10 v{i}", 100000, f"s{i}") for i in range(5)])
        seen.clear()
        self.go(fake2, photo_fn=reader, photo_budget=2, db=os.path.join(self.tmp.name, "other.sqlite"), out=os.path.join(self.tmp.name, "other.json"))
        self.assertEqual(len(seen), 2)                                              # the budget is a hard limit

    PHOTO_READS = [  # the three models of the first real photo (a Charizard EX slab) and what each of them read from it
        {"is_slab": True, "company": "EGS", "grade": "10", "cert": "A0D960", "name": "Charizard EX", "number": "330/193", "rarity": None, "language": "EN"},
        {"is_slab": True, "company": "EGS", "grade": "10", "cert": "A018960", "name": "CHARIZARD EX", "number": "234/193", "rarity": "SAR", "language": "ID"},
        {"is_slab": True, "company": "EGS", "grade": "10", "cert": "A0019500", "name": "CHARIZARD EX", "number": "22/100", "rarity": "SAR", "language": "ID"}]

    def test_readers_must_agree_before_anything_from_the_photo_is_used(self):
        three = egs_watch.consensus(self.PHOTO_READS)
        self.assertEqual((three["number"], three["cert"], three["language"], three["rarity"], three["grade"], three["company"], three["name"]),
                         (None, None, None, None, "10", "EGS", "Charizard EX"))          # a field survives only if every reader gave it and they agree
        self.assertEqual(sorted(three["disagree"]), ["cert", "language", "number"])
        two = egs_watch.consensus(self.PHOTO_READS[1:])
        self.assertEqual((two["number"], two["cert"], two["language"], two["rarity"], two["readers"]), (None, None, "ID", "SAR", 2))
        self.assertEqual(sorted(two["disagree"]), ["cert", "number"])
        same = egs_watch.consensus([{**self.PHOTO_READS[1], "number": "234 / 193", "cert": "a018960"}, {**self.PHOTO_READS[1], "language": "UNCLEAR"}])
        self.assertEqual((same["number"], same["cert"].upper(), same["disagree"], same["language"]), ("234/193", "A018960", {}, None))   # spacing and case are not disagreement; UNCLEAR is no answer

    def test_the_same_number_written_two_ways_is_not_a_disagreement(self):
        base = {"is_slab": True, "company": "EGS", "grade": "9", "cert": None, "name": "Mew VMAX", "rarity": None, "language": "ID"}
        c = egs_watch.consensus([{**base, "number": "118/100"}, {**base, "number": "HR - 118/100"}])
        self.assertEqual((c["number"], c["disagree"]), ("118/100", {}))                 # from the first real run: the readers agreed, only the prefix differed
        c = egs_watch.consensus([{**base, "number": "001"}, {**base, "number": "#001"}])
        self.assertEqual(c["disagree"], {})
        c = egs_watch.consensus([{**base, "number": "234/193"}, {**base, "number": "230/182"}])
        self.assertEqual((c["number"], sorted(c["disagree"])), (None, ["number"]))

    def test_the_report_says_how_many_listings_the_photo_moved_and_how_many_photos_were_read(self):
        items = [egs_item("Pikachu SAR MA3 ID EGS 10", 2500000, "s1"), egs_item("Mimikyu RR MA3 ID EGS 10", 100000, "s2")]
        first, _ = self.go(FakeEgsToco(items))
        self.assertEqual((first["summary"]["accepted_total"], first["summary"]["ambiguous"], first["run"]["promoted"]), (0, 2, 0))

        def reader(url):
            both = {"is_slab": True, "company": "EGS", "grade": "10", "cert": None, "name": None, "rarity": "SAR", "language": "ID"}
            return egs_watch.consensus([{**both, "number": "234/193"}, {**both, "number": "234/193"}]) if "pikachu" in url else \
                egs_watch.consensus([{**both, "number": "150/193"}, {**both, "number": "151/193"}])
        second, _ = self.go(FakeEgsToco(items), now=self.T0 + timedelta(hours=6), photo_fn=reader, photo_budget=5)
        self.assertEqual((second["summary"]["accepted_total"], second["summary"]["ambiguous"], second["run"]["promoted"], second["run"]["photo_reads"]), (1, 1, 1, 2))
        text = egs_watch.format_report(second)
        self.assertIn("naik dari ambigu ke diterima putaran ini: 1", text)
        self.assertIn("foto dibaca putaran ini: 2 listing; 1 di antrean karena pembaca tidak sepakat soal nomor", text)

    def test_a_number_the_readers_disagree_on_keeps_the_listing_in_the_queue_with_the_reason(self):
        fake = FakeEgsToco([egs_item("Pikachu SAR MA3 ID EGS 10", 2500000, "s1")])
        disagree = {**self.PHOTO_READS[1], "name": None, "language": "ID"}
        export, _ = self.go(fake, photo_fn=lambda url: egs_watch.consensus([disagree, {**disagree, "number": "22/100"}]), photo_budget=3)
        self.assertEqual(export["listings"], [])
        reasons = [r["code"] for r in export["queue"][0]["reasons"]]
        self.assertIn("photo_unsure", reasons)
        self.assertIn("number_missing", reasons)
        fake2 = FakeEgsToco([egs_item("Pikachu SAR MA3 ID EGS 10", 2500000, "s1")])
        agree = {**disagree, "number": "234/193", "name": "Pikachu ex"}
        export, _ = self.go(fake2, photo_fn=lambda url: egs_watch.consensus([agree, dict(agree)]), photo_budget=3, db=os.path.join(self.tmp.name, "b.sqlite"), out=os.path.join(self.tmp.name, "b.json"))
        self.assertEqual(export["listings"][0]["identity"]["identified_by"], "nomor di foto")                      # two readers, the same number

    def test_the_photo_is_fetched_once_and_read_by_every_listed_model(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        fetched, asked = [], []

        def fake_ask(model, prompt=None):
            def ask(b64, media):
                asked.append((model, media))
                return json.dumps({**self.PHOTO_READS[1], "card_name": "Pikachu", "number": "234/193" if model == "m1" else None}) if model != "m3" else None
            return ask
        with mock.patch.object(egs_watch, "_ask_openai", side_effect=fake_ask):
            read = egs_watch.make_photo_reader("openai", "m1, m2", fetch=lambda url: (fetched.append(url) or (png, "image/png")))
            got = read("https://img.example/a.png")
            self.assertEqual((fetched, asked), (["https://img.example/a.png"], [("m1", "image/png"), ("m2", "image/png")]))
            self.assertEqual((got["readers"], got["number"], sorted(got["disagree"])), (2, None, []))                # one reader had no number: nothing agreed, nothing contradicted
            self.assertIsNone(egs_watch.make_photo_reader("openai", "m1,m3", fetch=lambda url: (png, "image/png"))("https://img.example/b.png"))   # a failed reader: try again later
            single = egs_watch.make_photo_reader("openai", "m1", fetch=lambda url: (png, "image/png"))("https://img.example/c.png")
            self.assertEqual((single["number"], "readers" in single), ("234/193", False))                             # one model: the old behaviour
        self.assertEqual(egs_watch.DEFAULT_PHOTO_MODELS["openai"], "gpt-4.1,gpt-4o")

    def test_the_board_takes_the_accepted_listings_and_shows_the_review_queue(self):
        export, _ = self.go(FakeEgsToco(self.catalog_listings()))
        latest = tiny_latest()
        data = build_site.build(latest, None, None, None, None, export)
        rows = {(t["name"], t["number"]): t["rows"] for t in data["types"]}
        self.assertEqual(len(latest["listings"]), 1)                                      # latest.json itself is never changed
        self.assertEqual(sorted(len(r) for r in rows.values()), [1, 1, 3])                # the old Mega Venusaur, Dreepy, and Pikachu ex with its two grades
        pika = rows[("Pikachu ex", "234/193")]
        self.assertEqual(sorted((r["grade"], r["ask"]) for r in pika), [("10", 2500000), ("10", 2600000), ("9.5", 1500000)])
        self.assertTrue(all(r["tracked"] and r["conf"] == "dipantau" for r in pika))
        w = data["egs_watch"]
        self.assertEqual((w["queue_total"], w["summary"]["accepted_total"], w["source_ok"]), (3, 6, True))
        q = next(x for x in w["queue"] if "salah rarity" in x["title"])
        self.assertTrue(q["photo"] and q["url"].startswith("https://toco.id/id/listing/") and "Rarity di judul (SR)" in q["reasons"][0])
        again = build_site.build(tiny_latest(), None, None, None, None, None)
        self.assertIsNone(again["egs_watch"])

    def test_the_photo_answer_is_parsed_defensively(self):
        ok = egs_watch.parse_photo_answer('Here you go: {"is_slab": true, "company": "EGS", "grade": "9.5", "cert": null, "card_name": "Pikachu ex", '
                                         '"number": "234/193", "rarity": "SAR", "language": "Indonesian"}')
        self.assertEqual((ok["company"], ok["grade"], ok["cert"], ok["number"], ok["language"], ok["is_slab"]), ("EGS", "9.5", None, "234/193", "ID", True))
        self.assertIsNone(egs_watch.parse_photo_answer("I cannot read this"))
        self.assertIsNone(egs_watch.parse_photo_answer("{not json}"))
        self.assertIsNone(egs_watch.parse_photo_answer('["a"]'))

    def test_the_photo_test_command_needs_a_provider(self):
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = egs_watch.main(["--read-photo", "https://img.example/x.webp"])
        self.assertEqual(code, 1)
        self.assertIn("--photo-mode", out[0])

    def test_report_and_validation_commands(self):
        fake = FakeEgsToco(self.catalog_listings())
        export, _ = self.go(fake)
        out = self.cfg().out
        self.assertEqual(egs_watch.main(["--validate", out]), 0)
        text = egs_watch.format_report(export)
        for want in ("kandidat ditemukan", "ambigu", "Pikachu ex 234/193 MA3 SAR EGS 10", "data terbatas", "alasan:", "foto: https://img.example/"):
            self.assertIn(want, text)
        with open(out, encoding="utf-8") as fh:
            broken = json.load(fh)
        broken["listings"][0]["asking_idr"] = 0
        broken["listings"][0]["last_ok"] = None
        errors = egs_watch.validate(broken)
        self.assertTrue(any("tanpa bukti" in e for e in errors))
        broken["queue"].append(dict(broken["queue"][0]))
        self.assertTrue(any("antrean ganda" in e for e in egs_watch.validate(broken)))

    # -- listings collected by hand (Shopee, Tokopedia): same judgement, never fetched
    def write_import(self, rows, name="impor.csv", header=("url", "judul", "harga", "toko", "foto", "stok")):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        return path

    def test_hand_collected_rows_are_judged_like_toco_listings_and_nothing_is_fetched(self):
        path = self.write_import([
            ("https://shopee.co.id/Pikachu-ex-SAR-i.111.222?sp_atk=abc&xptdk=1", "Pikachu ex SAR 234/193 MA3 ID EGS 10", "Rp2.450.000", "Toko A", "https://down-id.img.example/a.jpg", "1"),
            ("https://www.tokopedia.com/tokob/pikachu-ex-egs-10?extParam=ivf%3Dtrue", "Pikachu ex SAR 234/193 MA3 ID EGS 10", "2400000", "Toko B", "", ""),
            ("https://www.tokopedia.com/tokoc/pikachu-egs", "Pikachu EGS 10 ID", "700000", "Toko C", "", "2"),     # no number: ambiguous
            ("https://shopee.co.id/rusak-i.1.2", "Dreepy AR MA3 ID EGS 10", "abc", "Toko D", "", "1"),            # price is not a number
            ("ftp://bukan-web/x", "Dreepy AR MA3 ID EGS 10", "100000", "Toko D", "", "1")])                        # not a web address
        fake = FakeEgsToco([])
        export, code = self.go(fake, no_search=True, import_file=path)
        self.assertEqual((code, fake.posts), (0, []))                                  # toco was not even asked
        self.assertEqual(egs_watch.validate(export), [])
        by_url = {l["url"]: l for l in export["listings"]}
        self.assertEqual(sorted(by_url), ["https://shopee.co.id/Pikachu-ex-SAR-i.111.222", "https://www.tokopedia.com/tokob/pikachu-ex-egs-10"])   # tracking parameters gone
        self.assertEqual({l["source"] for l in export["listings"]}, {"shopee", "tokopedia"})
        self.assertTrue(all(l["origin"] == "import" and l["counted"] and l["last_ok"] for l in export["listings"]))
        self.assertEqual([q["title"] for q in export["queue"]], ["Pikachu EGS 10 ID"])
        g = export["cards"][0]["grades"][0]
        self.assertEqual((g["n"], g["sellers"], g["min"], g["max"]), (2, 2, 2400000, 2450000))
        imp = export["run"]["imported"]
        self.assertEqual((imp["rows"], imp["accepted"], imp["ambiguous"], imp["rejected"], len(imp["problems"])), (3, 2, 1, 0, 2))
        self.assertEqual(export["summary"]["by_source"], {"shopee": {"accepted": 1, "counted": 1}, "tokopedia": {"accepted": 1, "counted": 1}})
        self.assertIn("impor manual: 3 baris", egs_watch.format_report(export))

    def test_hand_collected_rows_are_not_looked_up_on_toco_and_stop_counting_when_nobody_refreshes_them(self):
        path = self.write_import([("https://shopee.co.id/Pikachu-i.1.2", "Pikachu ex SAR 234/193 MA3 ID EGS 10", "2450000", "Toko A", "", "1")])
        self.go(FakeEgsToco([]), no_search=True, import_file=path)
        fake = FakeEgsToco([])
        later, code = self.go(fake, now=self.T0 + timedelta(hours=6), status=lambda url: 404)        # a normal toco run: it must not turn a Shopee row into "gone"
        row = later["listings"][0]
        self.assertEqual((code, row["status"], row["counted"], row["excluded"]), (0, "ok", True, None))
        old, _ = self.go(fake, now=self.T0 + timedelta(days=3), status=lambda url: 404)
        row = old["listings"][0]
        self.assertEqual((row["status"], row["counted"], row["excluded"]), ("ok", False, "stale"))   # shown, honestly marked, not in any price

    def test_importing_again_records_the_price_history_and_the_same_page_is_one_listing(self):
        row = lambda price: ("https://www.tokopedia.com/tokoa/pikachu-egs?utm_source=x", "Pikachu ex SAR 234/193 MA3 ID EGS 10", price, "Toko A", "", "1")
        self.go(FakeEgsToco([]), no_search=True, import_file=self.write_import([row("2500000"), row("2450000")]))     # twice in one file: the later line wins
        second, _ = self.go(FakeEgsToco([]), no_search=True, now=self.T0 + timedelta(days=1), import_file=self.write_import([row("2300000")], name="dua.csv"))
        self.assertEqual(len(second["listings"]), 1)
        l = second["listings"][0]
        self.assertEqual((l["asking_idr"], l["previous_day"]["asking_idr"], l["change_from_previous_day_idr"]), (2300000, 2450000, -150000))

    def test_a_hand_collected_row_without_a_number_can_be_settled_by_its_photo(self):
        path = self.write_import([("https://shopee.co.id/Pikachu-i.3.4", "Pikachu SAR MA3 ID EGS 10", "2450000", "Toko A", "https://down-id.img.example/p.jpg", "1")])
        seen = []

        def reader(url):
            seen.append(url)
            return {"is_slab": True, "company": "EGS", "grade": "10", "cert": None, "number": "234/193", "rarity": "SAR", "language": "ID", "name": None}
        export, _ = self.go(FakeEgsToco([]), no_search=True, import_file=path, photo_fn=reader, photo_budget=3)
        self.assertEqual((seen, export["listings"][0]["identity"]["identified_by"]), (["https://down-id.img.example/p.jpg"], "nomor di foto"))

    def test_import_files_in_other_shapes_and_prices_in_rupiah_notation(self):
        for text, want in (("Rp1.250.000", 1250000), ("1.250.000", 1250000), (" 1250000 ", 1250000), (1250000, 1250000), ("Rp 99.500", 99500),
                           ("1,250,000", None), ("12,5", None), ("abc", None), ("", None), (0, None), (-5, None), (True, None)):
            self.assertEqual(egs_watch.parse_rupiah(text), want, text)
        d = self.tmp.name
        with open(os.path.join(d, "a.json"), "w", encoding="utf-8") as fh:
            json.dump({"listings": [{"link": "https://shopee.co.id/x-i.5.6#top", "Judul": "Dreepy AR MA3 ID EGS 10", "harga": 200000, "toko": "Toko Z"}]}, fh)
        with open(os.path.join(d, "b.csv"), "w", encoding="utf-8-sig") as fh:
            fh.write("url;judul;harga\nhttps://shopee.co.id/y-i.7.8;Dreepy AR MA3 ID EGS 10;200.000\n")
        for name in ("a.json", "b.csv"):
            records, problems = egs_watch.read_import(os.path.join(d, name))
            self.assertEqual((problems, [(r["ask"], r["stock"]) for r in records]), ([], [(200000, 1)]), name)
        self.assertEqual(egs_watch.read_import(os.path.join(d, "a.json"))[0][0]["url"], "https://shopee.co.id/x-i.5.6")
        self.assertTrue(egs_watch.read_import(os.path.join(d, "tidak-ada.csv"))[1])
        self.assertEqual([egs_watch.source_of(u) for u in ("https://toco.id/id/listing/x", "https://www.shopee.co.id/a", "https://shp.ee/abc", "https://www.tokopedia.com/a/b", "https://m.facebook.com/x")],
                         ["toco", "shopee", "shopee", "tokopedia", "m.facebook.com"])

    # -- the worksheet: photos with empty columns, filled by hand, read back
    def make_sheet(self, fake, **over):
        export, _ = self.go(fake, **over)
        path = os.path.join(self.tmp.name, "lembar.csv")
        counts = egs_worksheet.make(export, egs_catalog_data(), path)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return export, path, counts, rows

    def filled(self, rows, path, edits):
        """Write the sheet back the way Google Sheets exports it after the owner typed edits = {kunci-or-kartu-text: {column: value}}."""
        for r in rows:
            for key, values in edits.items():
                if key in (r["kunci"], r["kartu"]):
                    r.update(values)
        out = os.path.join(self.tmp.name, "terisi.csv")
        with open(out, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=egs_worksheet.COLUMNS)
            w.writeheader()
            w.writerows(rows)
        return out

    def test_the_worksheet_has_the_photos_the_prices_and_the_empty_columns_to_fill(self):
        export, path, counts, rows = self.make_sheet(FakeEgsToco(self.catalog_listings()))
        self.assertEqual(counts, {"pasar": 3, "antrean": 3})                          # Pikachu ex 10, Pikachu ex 9.5 and Dreepy 10; and the review queue
        market = [r for r in rows if r["jenis"] == "pasar"]
        queue = {r["kartu"]: r for r in rows if r["jenis"] == "antrean"}
        pika = next(r for r in market if r["kartu"].startswith("Pikachu ex 234/193 MA3") and r["grade"] == "10")
        self.assertEqual((pika["foto"], pika["harga_toco"], pika["kunci"]), ('=IMAGE("https://o/1.png")', "2550000", "Pikachu ex|MA3|234/193|SAR|10"))   # the official picture
        q = queue["Pikachu EGS 10 ID"]
        self.assertTrue(q["foto"].startswith('=IMAGE("https://img.example/') and q["kunci"].startswith("https://toco.id/id/listing/") and "nomor" in q["alasan_ragu"].lower())
        for r in rows:                                                              # nothing is pre-filled where the owner has to type
            self.assertEqual([r[c] for c in ("nomor", "rarity", "bahasa", "keputusan", "harga_tokopedia", "link_tokopedia", "harga_shopee", "link_shopee", "catatan")], [""] * 9)

    def test_a_tab_separated_worksheet_pastes_into_columns_and_reads_back(self):
        export, _ = self.go(FakeEgsToco(self.catalog_listings()))
        path = os.path.join(self.tmp.name, "lembar.tsv")
        egs_worksheet.make(export, egs_catalog_data(), path)
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().split("\n")
        self.assertEqual(lines[0].split("\t"), egs_worksheet.COLUMNS)
        index = next(n for n, l in enumerate(lines) if l.startswith("pasar") and "Pikachu ex 234/193" in l and "\t10\t" in l)
        pika = lines[index].split("\t")
        self.assertEqual(pika[1], '=IMAGE("https://o/1.png")')                       # one plain formula, no quote doubling, so Sheets turns it into the picture
        self.assertTrue(all(len(l.split("\t")) == len(egs_worksheet.COLUMNS) for l in lines[1:] if l))
        col = egs_worksheet.COLUMNS.index
        pika[col("bahasa")], pika[col("harga_shopee")], pika[col("link_shopee")] = "ID", "2.450.000", "https://shopee.co.id/Pikachu-i.9.9"
        lines[index] = "\t".join(pika)
        reads, imports, problems = egs_worksheet.parse("\n".join(lines))
        self.assertEqual((reads, problems, [(x["url"], x["price"]) for x in imports]), ({}, [], [("https://shopee.co.id/Pikachu-i.9.9", "2.450.000")]))

    def test_what_is_typed_in_the_worksheet_is_read_strictly(self):
        export, path, counts, rows = self.make_sheet(FakeEgsToco(self.catalog_listings()))
        queue = {r["kartu"]: r["kunci"] for r in rows if r["jenis"] == "antrean"}
        pika = "Pikachu ex|MA3|234/193|SAR|10"
        dreepy = next(r["kunci"] for r in rows if r["kartu"].startswith("Dreepy"))
        sheet = self.filled(rows, path, {
            queue["Pikachu EGS 10 ID"]: {"nomor": "234 / 193", "rarity": "sar", "bahasa": "Indo", "grade": "10"},
            queue["Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID"]: {"keputusan": "Tolak"},
            queue["Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity"]: {"nomor": "abc"},                         # not a number: the whole row is refused
            pika: {"bahasa": "ID", "harga_tokopedia": "Rp 2.600.000", "link_tokopedia": "https://www.tokopedia.com/t/pika?x=1", "harga_shopee": "2.550.000"},   # no shopee link
            dreepy: {"bahasa": "EN", "harga_shopee": "100.000", "link_shopee": "https://shopee.co.id/d-i.1.2"}})   # an English card is not counted here
        reads, imports, problems = egs_worksheet.read(sheet)
        self.assertEqual(reads[queue["Pikachu EGS 10 ID"]], {"manual": True, "is_slab": None, "company": None, "cert": None, "name": None, "number": "234/193", "rarity": "SAR", "language": "ID", "grade": "10"})
        self.assertEqual(reads[queue["Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID"]], {"manual": True, "reject": True})
        self.assertNotIn(queue["Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity"], reads)
        self.assertEqual([(i["url"], i["title"], i["price"]) for i in imports], [("https://www.tokopedia.com/t/pika?x=1", "Pikachu ex 234/193 MA3 SAR EGS 10 ID", "Rp 2.600.000")])
        text = "\n".join(problems)
        self.assertIn("nomor 'abc' harus lengkap", text)
        self.assertIn("harga dan link harus diisi dua-duanya", text)
        self.assertIn("bahasa EN, bukan kartu Indonesia", text)
        self.assertEqual(egs_worksheet.read(os.path.join(self.tmp.name, "tidak-ada.csv"))[2][0][:25], "lembar kerja tidak terbac")
        self.assertIn("tidak dikenali", egs_worksheet.parse("a,b\n1,2\n")[2][0])
        invisible = '"\ufeffjenis",kartu,kunci,bahasa,harga_shopee,link_shopee\npasar,Pikachu,Pikachu ex|MA3|234/193|SAR|10,ID,2.450.000,https://shopee.co.id/p-i.1.2\n'
        self.assertEqual(len(egs_worksheet.parse(invisible)[1]), 1)             # a byte-order mark pasted into the first cell must not hide every row

    def test_a_filled_worksheet_settles_the_queue_and_adds_market_prices_and_it_sticks(self):
        items = self.catalog_listings()
        export, path, counts, rows = self.make_sheet(FakeEgsToco(items))
        queue = {r["kartu"]: r["kunci"] for r in rows if r["jenis"] == "antrean"}
        sheet = self.filled(rows, path, {
            queue["Pikachu EGS 10 ID"]: {"nomor": "234/193", "rarity": "SAR", "bahasa": "ID", "grade": "10"},
            queue["Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID"]: {"keputusan": "tolak"},
            "Pikachu ex|MA3|234/193|SAR|10": {"bahasa": "ID", "harga_tokopedia": "2.600.000", "link_tokopedia": "https://www.tokopedia.com/t/pika",
                                              "harga_shopee": "2.450.000", "link_shopee": "https://shopee.co.id/Pikachu-i.9.9?sp=1"}})
        fake = FakeEgsToco(items)
        later, code = self.go(fake, now=self.T0 + timedelta(hours=1), no_search=True, worksheet=sheet)
        self.assertEqual((code, fake.posts, egs_watch.validate(later)), (0, [], []))
        ws = later["run"]["worksheet"]
        self.assertEqual((ws["filled_rows"], ws["accepted"], ws["rejected"], ws["market_rows"], ws["problems"]), (2, 1, 1, 2, []))
        self.assertEqual([q["title"] for q in later["queue"]], ["Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity"])      # two of the three left the queue
        settled = next(l for l in later["listings"] if l["title"] == "Pikachu EGS 10 ID")
        self.assertEqual(settled["identity"]["identified_by"], "nomor diisi manual dari foto")
        self.assertEqual(settled["identity"]["language_by"], "teks")                    # the title already says ID; only the number came from the owner
        sources = {l["source"]: l for l in later["listings"] if l["origin"] == "import"}
        self.assertEqual(sorted(sources), ["shopee", "tokopedia"])
        self.assertEqual(sources["shopee"]["url"], "https://shopee.co.id/Pikachu-i.9.9")
        self.assertEqual(later["summary"]["by_source"]["toco"]["counted"] + later["summary"]["by_source"]["shopee"]["counted"] + later["summary"]["by_source"]["tokopedia"]["counted"],
                         later["summary"]["listings_counted"])
        self.assertIn("lembar kerja: 2 baris antrean diisi", egs_watch.format_report(later))
        # an ordinary run afterwards sees the same listings again in the search: the typed answers hold, the thrown-out listing stays out
        again, _ = self.go(FakeEgsToco(items), now=self.T0 + timedelta(hours=7))
        self.assertEqual([q["title"] for q in again["queue"]], ["Pikachu ex SR 234/193 MA3 ID EGS 10 salah rarity"])
        self.assertIn("Pikachu EGS 10 ID", [l["title"] for l in again["listings"]])
        self.assertNotIn("Paket 3 slab EGS 10 Pikachu Mew Charizard MA3 ID", [x["title"] for x in again["listings"] + again["queue"]])


def slug_of_url(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


class MarketplaceProbe(unittest.TestCase):
    def serve(self, routes):
        """A throw-away HTTP server: routes maps a path prefix to (status, headers, body)."""
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                for prefix, (status, headers, body) in routes.items():
                    if self.path.startswith(prefix):
                        self.send_response(status)
                        for k, v in headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        self.wfile.write(body.encode())
                        return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass
        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}"

    def run_probe(self, base, **kw):
        lines = []
        v = probe_marketplaces.probe_site("Situs", base, "/search?q={q}", lines.append, sleep=lambda s: None, **kw)
        return v, "\n".join(lines)

    def test_a_page_with_listings_is_ok_and_robots_rules_are_shown(self):
        page = "<html>" + "".join(f"<div>Pokemon EGS 10 Rp{n},000</div>" for n in (450, 500, 650)).replace(",000", ".000") + "</html>"
        base = self.serve({"/robots.txt": (200, {}, "User-agent: *\nDisallow: /admin\nCrawl-delay: 5\n"), "/search": (200, {}, page)})
        v, text = self.run_probe(base)
        self.assertEqual(v, {"page": "ok"})
        self.assertIn("Crawl-delay: 5", text)
        self.assertIn("OK: 3 harga terbaca", text)

    def test_a_candidate_site_is_not_read_when_robots_txt_forbids_it_and_its_terms_are_pointed_to(self):
        page = ("<html><a href='/syarat-ketentuan'>Syarat &amp; Ketentuan</a><a href=\"/about\">Tentang</a><a href=\"/privacy\">x</a>"
                + "".join(f"<div>Pokemon Rp{n}.000</div>" for n in (450, 500, 650)) + "</html>")
        allowed = self.serve({"/robots.txt": (200, {}, "User-agent: *\nDisallow: /admin\n"), "/search": (200, {}, page)})
        lines = []
        v = probe_marketplaces.probe_site("Situs", allowed, "/search?q={q}", lines.append, sleep=lambda s: None, respect_robots=True)
        text = "\n".join(lines)
        self.assertEqual(v, {"page": "ok"})
        self.assertIn(allowed + "/syarat-ketentuan", text)                      # the terms page is shown to be read ...
        self.assertIn(allowed + "/privacy", text)
        self.assertNotIn("/about", text.split("syarat dan ketentuan:")[1])
        self.assertIn("baca sebelum membangun pengambil", text)                 # ... before anything is built
        closed = self.serve({"/robots.txt": (200, {}, "User-agent: *\nDisallow: /\n"), "/search": (200, {}, page)})
        lines = []
        v = probe_marketplaces.probe_site("Situs", closed, "/search?q={q}", lines.append, sleep=lambda s: None, respect_robots=True)
        self.assertEqual(v, {"page": "robots"})
        self.assertIn("melarang bot", "\n".join(lines))
        self.assertNotIn("halaman pencarian", "\n".join(lines))                  # the page itself was never requested
        lines = []
        v = probe_marketplaces.probe_site("Situs", closed, "/search?q={q}", lines.append, sleep=lambda s: None, api_path="/api?q={q}")   # every site obeys it, Tokopedia and Shopee too
        self.assertEqual(v, {"page": "robots"})

    def test_robots_patterns_with_stars_and_dollars_are_understood_the_way_the_sites_write_them(self):
        tokopedia = "User-agent: *\nDisallow: /*src=search*\nDisallow: /amp/api/*\nDisallow: /search?*\nDisallow: /image-search/\n"   # as the real file reads
        allows = lambda text, path: probe_marketplaces.robots_allows(text, "https://x.example" + path)
        self.assertFalse(allows(tokopedia, "/search?q=EGS%20pokemon&st=product"))            # the search page of the first VPS probe: forbidden
        self.assertFalse(allows(tokopedia, "/p/pokemon?src=search"))
        self.assertTrue(allows(tokopedia, "/some-shop"))
        self.assertTrue(allows(tokopedia, "/search"))                                         # only the form with a query string is named
        shopee = "User-agent: *\nCrawl-delay:0.1\nDisallow: /search*searchPrefill\nDisallow: /search?shop=\nDisallow: /search/?\n"
        self.assertTrue(allows(shopee, "/search?keyword=EGS%20pokemon"))
        self.assertFalse(allows(shopee, "/search?x=1&searchPrefill=2"))
        mixed = "User-agent: *\nDisallow: /shop/\nAllow: /shop/public/\nDisallow: /*.json$\n"
        self.assertTrue(allows(mixed, "/shop/public/a"))                                       # the longer Allow beats the shorter Disallow
        self.assertFalse(allows(mixed, "/shop/private"))
        self.assertFalse(allows(mixed, "/data.json"))
        self.assertTrue(allows(mixed, "/data.json.html"))                                      # $ anchors the end
        self.assertTrue(allows("User-agent: *\nDisallow:\n", "/anything"))                   # an empty Disallow allows everything
        self.assertTrue(allows("User-agent: googlebot\nDisallow: /\n", "/anything"))         # a group for another bot is not ours
        self.assertTrue(allows("", "/anything"))

    def test_site_option_and_candidate_list(self):
        self.assertEqual([c[0] for c in probe_marketplaces.CANDIDATES], ["Cardtell", "Pokepedia", "Carousell Indonesia"])
        base = self.serve({"/robots.txt": (404, {}, ""), "/cari": (200, {}, "".join(f"<div>Rp{n}.000</div>" for n in (450, 500, 650)))})
        lines = []
        with mock.patch("builtins.print", lambda *a, **k: lines.append(" ".join(str(x) for x in a))), mock.patch.object(probe_marketplaces.time, "sleep", lambda s: None):
            probe_marketplaces.main(["--skip-marketplaces", "--site", f"Toko X={base}|/cari?q={{q}}", "--site", "rusak"])
        text = "\n".join(lines)
        self.assertIn("== Toko X", text)
        self.assertIn("Toko X: page=ok", text)
        self.assertIn("--site 'rusak' dilewati", text)
        self.assertNotIn("Tokopedia", text)

    def test_a_challenge_a_login_a_block_and_an_empty_shell_are_told_apart_and_none_is_worked_around(self):
        cases = [("captcha", (403, {}, "<html>Please verify you are human. captcha</html>")),
                 ("captcha", (200, {}, "<html>Just a moment... Cloudflare cf-chl</html>")),
                 ("blocked", (429, {}, "slow down")),
                 ("empty", (200, {}, "<html><div id='root'></div><script>loadProducts()</script></html>")),
                 ("error", (503, {}, ""))]
        for want, route in cases:
            base = self.serve({"/search": route})
            v, text = self.run_probe(base)
            self.assertEqual(v["page"], want, text)
        base = self.serve({"/search": (302, {"Location": "/buyer/login?next=%2Fsearch"}, ""), "/buyer/login": (200, {}, "<html>Masuk atau daftar</html>")})
        v, text = self.run_probe(base)
        self.assertEqual(v["page"], "login")
        self.assertIn("/buyer/login", text)

    def test_shopee_style_refusal_of_its_own_search_call_is_reported_as_blocked(self):
        base = self.serve({"/api/search": (200, {"Content-Type": "application/json"}, json.dumps({"error": 90309999, "data": None})),
                           "/search": (200, {}, "<html></html>")})
        lines = []
        v = probe_marketplaces.probe_site("Shopee", base, "/search?keyword={q}", lines.append, sleep=lambda s: None, api_path="/api/search?keyword={q}")
        text = "\n".join(lines)
        self.assertEqual(v, {"page": "empty", "api": "blocked"})
        self.assertIn("anti-bot Shopee (90309999)", text)
        self.assertIn("tidak dilewati", text)
        ok = self.serve({"/api/search": (200, {"Content-Type": "application/json"}, json.dumps({"error": None, "items": [{"item_basic": {"name": "EGS"}}]}))})
        lines.clear()
        self.assertEqual(probe_marketplaces.probe_site("Shopee", ok, "/x?{q}", lines.append, sleep=lambda s: None, api_path="/api/search?keyword={q}")["api"], "ok")

    def test_the_summary_says_when_no_route_is_usable(self):
        blocked = self.serve({"/search": (403, {}, "no")})
        out = []
        real = probe_marketplaces.time.sleep
        probe_marketplaces.time.sleep = lambda s: None
        try:
            with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
                probe_marketplaces.main(["--tokopedia", blocked, "--shopee", blocked])
        finally:
            probe_marketplaces.time.sleep = real
        text = "\n".join(out)
        self.assertIn("tokopedia: page=blocked  -> belum ada jalur yang bisa dipakai tanpa melewati pembatasan", text)
        self.assertIn("shopee: page=blocked, api=blocked", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
