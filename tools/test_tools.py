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
