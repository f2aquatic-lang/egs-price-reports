#!/usr/bin/env python3
"""Unit tests for the read-only tools. Run: python3 tools/test_tools.py

Real titles below were copied from published latest.json candidates; the grade the
bot recorded (grade_hint) is the expected answer. RAW titles are made up.
"""
import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
import collect_raw  # noqa: E402
import db_report  # noqa: E402
import fetch_catalog  # noqa: E402
import probe_pokemon_card  # noqa: E402
import probe_toco  # noqa: E402
import probe_toco_fields  # noqa: E402
import probe_toco_next  # noqa: E402
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

    def test_trend_compares_with_about_a_week_earlier(self):
        h = lambda d, m: {"date_wib": d, "n": 5, "median": m, "min": m}
        self.assertIsNone(build_site.trend_of([h("2026-10-10", 100)]))
        self.assertEqual(build_site.trend_of([h("2026-10-03", 100), h("2026-10-06", 110), h("2026-10-10", 120)]),
                         {"days": 7, "pct": 20.0})   # baseline is the 3 Oct entry, not the 6 Oct one
        self.assertEqual(build_site.trend_of([h("2026-10-08", 200), h("2026-10-10", 150)]), {"days": 2, "pct": -25.0})

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
            sets=None, include=fetch_catalog.DEFAULT_INCLUDE, refresh_days=30, list_sets=False)

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
        data = self.go()
        sets = {s["code"]: s for s in data["sets"]}
        self.assertEqual(sorted(sets), ["MA6", "SV8S"])                       # SM1 does not match the default --include
        self.assertEqual((sets["MA6"]["name"], sets["MA6"]["released"], sets["MA6"]["total"], sets["MA6"]["complete"]),
                         ("30th CELEBRATION", "2026-09-16", 25, True))        # product of MA6, not the other product on card 1003
        ma6 = [c for c in data["cards"] if c["set"] == "MA6"]
        self.assertEqual(len(ma6), 24)                                        # the card without a number is left out
        self.assertEqual((ma6[0]["number"], ma6[0]["name"], ma6[0]["num"], ma6[0]["den"], ma6[0]["evolve"]), ("001/130", "Kartu 1", 1, 130, "basic"))
        self.assertEqual(ma6[0]["image"], "/id/card-img/id00001000.png")
        with open(self.args.out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cards"], data["cards"])

    def test_a_stopped_run_continues_without_refetching(self):
        first = self.go(sets="MA6", max_requests=8)                            # home 2 + list 2 + 4 cards
        self.assertFalse(first["sets"][0]["complete"])
        self.assertEqual(first["sets"][0]["fetched"], 4)
        used_before = len(self.calls)
        second = self.go(sets="MA6", max_requests=None)
        self.assertTrue(second["sets"][0]["complete"])
        detail_calls = [c for c in self.calls[used_before:] if "/detail/" in c]
        self.assertEqual(len(detail_calls), 21)                                # only the 21 cards still missing
        self.assertFalse(any("list/?pageNo" in c for c in self.calls[used_before:]))  # lists are fresh for 30 days

    def test_detail_page_without_a_number_or_with_odd_markup_does_not_crash(self):
        d = fetch_catalog.parse_detail("<html><h1 class='x'>nothing</h1></html>")
        self.assertEqual((d["name"], d["number"], d["image"], d["products"]), (None, None, None, {}))

    def test_a_missing_page_is_skipped_and_reported(self):
        del self.pages["/id/card-search/detail/1005/"]
        data = self.go(sets="MA6")
        self.assertEqual(len([c for c in data["cards"] if c["set"] == "MA6"]), 23)
        self.assertFalse(data["sets"][0]["complete"])
        self.assertTrue(any("status 404" in l for l in self.logs))

    def test_product_name_takes_the_quoted_part(self):
        self.assertEqual(fetch_catalog.product_name('Booster Pack "Evolusi Mega"'), "Evolusi Mega")
        self.assertEqual(fetch_catalog.product_name("Starter Deck"), "Starter Deck")


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
        self.assertTrue(any(c.startswith("MA3 · Evolusi Mega Impian ex") and c.endswith("130") for c in chips))
        self.assertTrue(any(c.startswith("XY1") and "·" not in c for c in chips))
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
