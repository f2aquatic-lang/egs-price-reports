#!/usr/bin/env python3
"""Unit tests for the read-only tools. Run: python3 tools/test_tools.py

Real titles below were copied from published latest.json candidates; the grade the
bot recorded (grade_hint) is the expected answer. RAW titles are made up.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
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

    def test_ids_extracted(self):
        r = classify_title("Pikachu ex SAR 234/193 MA3 ID NM")
        self.assertEqual((r["number"], r["set"], r["condition"]), ("234/193", "MA3", "nm"))


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


def good_raw():
    s = {"n": 3, "min": 80000, "p25": 90000, "median": 100000, "p75": 120000, "max": 150000}
    ls = [{"url": f"https://toco.id/id/listing/a-{i}-1790000000000-ab12", "ask_idr": p, "city": "Kota Jambi",
           "observed_at": "2026-10-02T22:30:00+00:00", "conf": "nomor", "title": "t"}
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

    def test_listing_outside_stats(self):
        d = good_raw()
        d["cards"][0]["listings"][2]["ask_idr"] = 999999
        self.assertTrue(any("outside" in e for e in validate_raw.validate(d)[0]))


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


class Board(unittest.TestCase):
    def test_raw_joins_on_set_and_number(self):
        raw = good_raw()
        raw["cards"].append({**raw["cards"][0], "name": "Gengar ex", "set_code": "MA1", "card_number": "150/126"})
        data = build_site.build(tiny_latest(), raw)
        self.assertEqual(data["types"][0]["raw"]["median"], 100000)
        self.assertEqual([c["name"] for c in data["raw_only"]], ["Gengar ex"])

    def test_without_raw(self):
        data = build_site.build(tiny_latest())
        self.assertNotIn("raw", data["types"][0])
        self.assertIsNone(data["raw_meta"])

    def test_queries_per_type(self):
        q = watch_queries.queries({"name": "Zekrom <N>", "set": "MA3", "number": "210/193"})
        self.assertEqual(q["egs"][0], "Zekrom N 210/193 egs")
        self.assertIn("210/193 MA3", q["raw"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
