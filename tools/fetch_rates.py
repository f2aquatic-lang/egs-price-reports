#!/usr/bin/env python3
"""Fetch the day's exchange rates yen -> rupiah, US dollar -> rupiah and euro -> rupiah (the Cardmarket prices) into rates.json.

The rate is read from Google Finance (google.com/finance/quote/JPY-IDR), the one the owner looks at. As a check, and as the fallback when
Google's page cannot be read, the daily reference rate of the European Central Bank (frankfurter.dev, free, no key) is fetched too:
Google's number is used unless it is outside a sane range or differs from the ECB rate by more than 3 percent. rates.json says which
source each rate came from. Six requests per run, so a daily refresh is a few requests a day.

Usage:
  python3 tools/fetch_rates.py --out /opt/egs-raw/rates.json
  python3 tools/fetch_rates.py --out /opt/egs-raw/rates.json --max-age-hours 20   # do nothing while the file is fresher than that
When Google's page is not understood it is saved as google_<currency>.html next to the output, and the log says what it did contain.
Needs only the Python standard library.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
GOOGLE = "https://www.google.com/finance/quote/{cur}-IDR?hl=en"
ECB = "https://api.frankfurter.dev/v1/latest?base={cur}&symbols=IDR"
REQUIRED = ("JPY", "USD")     # without these two the file is not written; the euro (English reference prices) is carried when it can be read
CURRENCIES = REQUIRED + ("EUR",)
SANE = {"JPY": (30.0, 400.0), "USD": (5000.0, 40000.0), "EUR": (6000.0, 50000.0)}  # rupiah per unit: outside this the number is not a rate
MAX_GAP = 0.03  # Google and the ECB may differ by this much (the ECB rate is fixed once a day)


def http_get(url, timeout=30):
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read(3_000_000).decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, OSError):
        return 0, ""


def number(text):
    try:
        return float(text.replace(",", "").strip())
    except ValueError:
        return None


def parse_google(page):
    """The rate on a Google Finance quote page: the data-last-price attribute, else the big price element."""
    for pattern in (r'data-last-price="([\d.,]+)"', r'class="YMlKec fxKbKc">\s*([\d.,]+)\s*<'):
        m = re.search(pattern, page)
        if m and number(m.group(1)):
            return number(m.group(1))
    return None


def parse_ecb(body):
    try:
        rate = float(json.loads(body)["rates"]["IDR"])
        return rate if rate > 0 else None
    except (ValueError, KeyError, TypeError):
        return None


def choose(cur, google, ecb):
    """(rate, source, note): Google's rate when it is sane and agrees with the ECB's; else the ECB's; else nothing."""
    lo, hi = SANE[cur]
    g = google if google and lo <= google <= hi else None
    e = ecb if ecb and lo <= ecb <= hi else None
    if g and (e is None or abs(g / e - 1) <= MAX_GAP):
        return g, "google", None if e else "tanpa pembanding ECB"
    if e:
        return e, "ecb", ("Google berbeda lebih dari 3% dari ECB" if g else "Google di luar kisaran wajar" if google else "Google tidak terbaca")
    return None, None, "tidak ada kurs yang masuk akal"


def describe(page):
    return f"{len(page)} karakter, data-last-price {'ada' if 'data-last-price' in page else 'tidak ada'}, 'consent' {'ada' if 'consent' in page.lower() else 'tidak ada'}"


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def fresh(old, now, hours):
    try:
        return all(now - datetime.fromisoformat(old["rates"][c]["fetched_at"]) < timedelta(hours=hours) for c in CURRENCIES)
    except (KeyError, TypeError, ValueError):
        return False


def run(args, get=http_get, now=None, sleep=time.sleep, log=print):
    now = now or datetime.now(timezone.utc)
    old = load(args.out)
    if args.max_age_hours and fresh(old, now, args.max_age_hours):
        log(f"kurs masih baru (kurang dari {args.max_age_hours} jam); tidak diambil lagi")
        return old
    rates, updated = dict((old or {}).get("rates") or {}), False
    folder = os.path.dirname(os.path.abspath(args.out))
    for cur in CURRENCIES:
        sleep(args.delay)
        status, page = get(GOOGLE.format(cur=cur))
        google = parse_google(page) if status == 200 else None
        if google is None:
            dump = os.path.join(folder, f"google_{cur}.html")
            if status == 200:
                with open(dump, "w", encoding="utf-8") as fh:
                    fh.write(page)
            log(f"  ! Google {cur}: " + (f"kurs tidak terbaca ({describe(page)}); halaman disimpan di {dump}" if status == 200 else f"status {status}"))
        sleep(args.delay)
        status, body = get(ECB.format(cur=cur))
        ecb = parse_ecb(body) if status == 200 else None
        rate, source, note = choose(cur, google, ecb)
        if rate is None:
            log(f"  ! {cur}: {note}; kurs lama dipertahankan" if cur in rates else f"  ! {cur}: {note}")
            continue
        rates[cur], updated = {"idr": rate, "source": source, "fetched_at": now.isoformat(), "google": google, "ecb": ecb}, True
        log(f"  {cur}: Rp {rate:g} ({source}; Google {google if google else '-'}, ECB {ecb if ecb else '-'})" + (f" [{note}]" if note else ""))
    if not updated or not all(c in rates for c in REQUIRED):
        log("Tidak ada kurs baru yang bisa dipakai. rates.json tidak diubah.")
        return old
    data = {"schema_version": 1, "generated_at": now.isoformat(), "rates": {c: rates[c] for c in CURRENCIES if c in rates}}
    os.makedirs(folder, exist_ok=True)
    with open(args.out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(args.out + ".tmp", args.out)
    log(f"ditulis: {args.out}")
    return data


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="rates.json")
    ap.add_argument("--delay", type=float, default=2.0, help="detik antar permintaan")
    ap.add_argument("--max-age-hours", type=float, help="jangan mengambil lagi kalau berkas lebih baru dari ini")
    data = run(ap.parse_args(argv))
    return 0 if data else 1


if __name__ == "__main__":
    sys.exit(main())
