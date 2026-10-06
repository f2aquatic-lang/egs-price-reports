#!/usr/bin/env python3
"""Fetch Japanese single-card asking prices from Yuyu-tei (yuyu-tei.jp, Pokemon section) into jp_prices.json.

One page per set (yuyu-tei.jp/sell/poc/s/<set code>) lists every card of the set with its shop price in yen
(condition "no scratches"), grouped by rarity, with the stock. robots.txt does not ask crawlers to stay away from
these pages; the default delay is 2 seconds. Sets: --list-sets shows them (code and name), --sets picks some.

Usage:
  python3 tools/fetch_jp_prices.py --list-sets
  python3 tools/fetch_jp_prices.py --sets m02a,m01l --out /tmp/jp_test.json
  python3 tools/fetch_jp_prices.py --out /opt/egs-raw/jp_prices.json
Needs only the Python standard library.
"""
import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://yuyu-tei.jp"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Mega series (m01l, m02a, m06, ...) and Scarlet & Violet (sv8a, sv11b, ...): the sets Indonesian cards are made from
DEFAULT_INCLUDE = r"^(m\d{2}[a-z]?|sv\d{1,2}[a-z]?)$"


def http_get(url, timeout=40):
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "ja,en;q=0.7"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read(6_000_000).decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, OSError):
        return 0, ""


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_set_list(page):
    """[(code, label)] from the set checkboxes of the search form, e.g. ('m02a', '[M2a] ハイクラスパック MEGAドリームex')."""
    out, seen = [], set()
    for code, label in re.findall(r'<input[^>]*name="vers\[\]"[^>]*value="([^"]+)"[^>]*>\s*<label[^>]*>(.*?)</label>', page, re.S):
        if code not in seen:
            seen.add(code)
            out.append((code, text_of(label)))
    for value, label in re.findall(r'<input[^>]*value="([^"]+)"[^>]*name="vers\[\]"[^>]*>\s*<label[^>]*>(.*?)</label>', page, re.S):
        if value not in seen:
            seen.add(value)
            out.append((value, text_of(label)))
    return out


# the start tag of one card: any whitespace between the parts, class among other attributes
CARD_START = re.compile(r'<div\b[^>]*?\bclass\s*=\s*"[^"]*\bcard-product\b[^"]*"[^>]*>')


def why_empty(page):
    """One line on what a page without readable cards does contain, to find out what changed in its markup."""
    codes = Counter(re.findall(r'href="https://yuyu-tei\.jp/sell/poc/card/([^/"]+)/\d+"', page))
    blocks = len(CARD_START.findall(page))
    has_list = "ada" if 'id="card-list3"' in page else "tidak ada"
    return f"{len(page)} karakter, card-list3 {has_list}, {blocks} blok card-product, kode di tautan kartu: {dict(codes.most_common(5))}"


def parse_set_page(page, code):
    """Cards of one set page: [{number, rarity, name, yen, stock, url, image, id}]. Other sets shown on the page are ignored."""
    start = page.find('id="card-list3"')
    body = page[start:] if start >= 0 else page
    marks = [(m.start(), "rarity", m.group(1).strip()) for m in re.finditer(
        r'<h3[^>]*>\s*<span[^>]*text-white[^>]*>([^<]+)</span>\s*Card List', body)]
    marks += [(m.start(), "card", None) for m in CARD_START.finditer(body)]
    marks.sort()
    cards, rarity = [], None
    for i, (pos, kind, value) in enumerate(marks):
        if kind == "rarity":
            rarity = value
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        block = body[pos:end]
        link = re.search(r'href="https://yuyu-tei\.jp/sell/poc/card/([^/"]+)/(\d+)"', block)
        if not link or link.group(1) != code:
            continue
        number = re.search(r'<span\b[^>]*\bclass\s*=\s*"d-block border[^"]*"[^>]*>\s*([^<]+?)\s*</span>', block)
        name = re.search(r"<h4[^>]*>(.*?)</h4>", block, re.S)
        yen = re.search(r"<strong[^>]*>\s*([\d,]+)\s*円", block)
        stock = re.search(r"在庫\s*:\s*(?:(×)|(\d+)\s*点)", block)
        image = re.search(r'<img[^>]*src="(https://card\.yuyu-tei\.jp/[^"]+)"', block)
        if not (number and yen):
            continue
        cards.append({"set": code, "number": text_of(number.group(1)), "rarity": rarity,
                      "name": text_of(name.group(1)) if name else None, "yen": int(yen.group(1).replace(",", "")),
                      "stock": 0 if (stock and stock.group(1)) or "sold-out" in CARD_START.match(block).group(0) else int(stock.group(2)) if stock and stock.group(2) else None,
                      "url": f"{BASE}/sell/poc/card/{code}/{link.group(2)}", "image": image.group(1) if image else None,
                      "id": int(link.group(2))})
    return cards


def parse_saved(path, code, log=print):
    """Read a page saved earlier (yuyutei_<code>.html) and show what the parser makes of it; no request is made."""
    with open(path, encoding="utf-8") as fh:
        cards = parse_set_page(fh.read(), code)
    log(f"{code}: {len(cards)} kartu, {sum(1 for c in cards if c['stock'])} ada stok; per rarity: {dict(Counter(c['rarity'] for c in cards))}")
    for c in cards[:4]:
        log(f"  {c['number']:>8} {str(c['rarity']):4} {c['yen']:>7,} yen stok {c['stock']} {c['name']}")
    return cards


def run(args, get=http_get, now=None, sleep=time.sleep, log=print):
    now = now or datetime.now(timezone.utc)
    status, page = get(f"{BASE}/top/poc")
    if status != 200:
        log(f"! daftar set tidak terbaca (status {status})")
        return None
    all_sets = parse_set_list(page)
    if args.list_sets:
        for code, label in all_sets:
            log(f"{code:10} | {label}")
        return None
    names = dict(all_sets)
    wanted = [c.strip() for c in args.sets.split(",")] if args.sets else [c for c, _ in all_sets if re.search(args.include, c)]
    log(f"{len(all_sets)} set di situs, {len(wanted)} dipilih: {', '.join(wanted)}")
    sets, cards, used = [], [], 0
    for code in wanted:
        if args.max_requests is not None and used >= args.max_requests:
            log("anggaran permintaan habis; sisanya tidak diambil")
            break
        sleep(args.delay)
        used += 1
        status, text = get(f"{BASE}/sell/poc/s/{code}")
        if status != 200:
            log(f"  ! {code}: status {status}")
            continue
        got = parse_set_page(text, code)
        if not got:
            dump = os.path.join(os.path.dirname(os.path.abspath(args.out)), f"yuyutei_{code}.html")
            with open(dump, "w", encoding="utf-8") as fh:
                fh.write(text)
            log(f"  ! {code}: tidak ada kartu terbaca ({why_empty(text)}); halaman disimpan di {dump}")
        cards += got
        sets.append({"code": code, "label": names.get(code), "cards": len(got)})
        log(f"  {code}: {len(got)} kartu ({names.get(code)})")
    data = {"schema_version": 1, "source": "yuyu-tei.jp/sell/poc", "currency": "JPY", "condition": "傷無し",
            "generated_at": now.isoformat(), "sets": sets, "cards": cards}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(args.out + ".tmp", args.out)
    log(f"ditulis: {args.out} ({len(sets)} set, {len(cards)} kartu, {used + 1} permintaan)")
    return data


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="jp_prices.json")
    ap.add_argument("--delay", type=float, default=2.0, help="detik antar permintaan")
    ap.add_argument("--sets", help="kode set Yuyutei dipisah koma, mis. m02a,m01l (bawaan: semua yang cocok --include)")
    ap.add_argument("--include", default=DEFAULT_INCLUDE, help="ekspresi reguler kode set yang diambil")
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--list-sets", action="store_true", help="hanya tampilkan set di situs, lalu selesai")
    ap.add_argument("--parse-file", help="baca halaman yang sudah disimpan (dengan --sets <satu kode>), tanpa permintaan ke situs")
    args = ap.parse_args(argv)
    try:
        if args.parse_file:
            parse_saved(args.parse_file, (args.sets or "").strip())
            return 0
        run(args)
    except BrokenPipeError:  # "| head" closed the pipe: not an error
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
