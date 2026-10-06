#!/usr/bin/env python3
"""Probe Tokopedia and Shopee: can listings be read from them at all, without logging in and without getting around a block?

Run it where the bot runs (the VPS): the answer depends on that address. It only reads: robots.txt, a public search page and,
for Shopee, the search call the website itself makes, a few requests per site, 2 seconds apart. Each site gets one verdict:

  ok          the page or call returned listings with titles and prices
  login       the site asks for a login: not used
  captcha     a challenge or verification page: not used, not solved, not worked around
  blocked     refused (403/429, an anti-bot error code): not worked around
  empty       answered normally but the listings are not in what was sent (they load later with JavaScript)
  error       network or server error

Only an "ok" is a source we can build on. For "empty" the optional --browser reads the rendered page with a headless browser exactly as
a visitor's browser would (Playwright needed); it stops at the first challenge or login.

Usage:  python3 tools/probe_marketplaces.py [--query "EGS pokemon"] [--browser]
        python3 tools/probe_marketplaces.py --tokopedia https://www.tokopedia.com --shopee https://shopee.co.id
"""
import argparse
import json
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CHALLENGE = re.compile(r"captcha|are you a robot|verify you are human|verifikasi|cf-chl|just a moment|geetest|slide to verify|_Incapsula_|akamai", re.I)
LOGIN = re.compile(r"/buyer/login|/login\b|masuk atau daftar|silakan masuk|log in to continue", re.I)
PRICE = re.compile(r"Rp\s?[\d.]{4,}")
# Shopee's own search call returns one of these error codes when it does not accept the caller (anti-fraud headers missing)
SHOPEE_BLOCK_CODES = {90309999: "anti-bot Shopee (90309999)", 90301999: "anti-bot Shopee (90301999)"}


def get(url, timeout=25, accept="text/html,application/json;q=0.9,*/*;q=0.8"):
    """(status, final url, text, error). Never raises; redirects are followed so a login redirect is visible in the final address."""
    req = Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "id-ID,id;q=0.9,en;q=0.7"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(2_000_000).decode("utf-8", "replace"), ""
    except HTTPError as e:
        return e.code, url, e.read(200_000).decode("utf-8", "replace"), f"HTTP {e.code}"
    except Exception as e:
        return 0, url, "", f"{type(e).__name__}: {e}"


def robots_lines(text, words=("search", "/api", "Crawl-delay", "User-agent: *")):
    keep = [l.strip() for l in text.splitlines() if any(w.lower() in l.lower() for w in words)]
    return keep[:8]


def judge_page(status, final_url, text, error):
    """(verdict, explanation) for a search page."""
    if status == 0:
        return "error", error
    if status in (401, 403, 429, 451) or status >= 500:
        if CHALLENGE.search(text[:5000]):
            return "captcha", f"status {status} dengan halaman verifikasi"
        return ("blocked" if status < 500 else "error"), f"status {status}"
    if LOGIN.search(final_url):
        return "login", "dialihkan ke halaman masuk: " + final_url[:100]
    if CHALLENGE.search(text[:20000]) and not PRICE.search(text):
        return "captcha", "halaman berisi tantangan verifikasi"
    prices = PRICE.findall(text)
    if len(prices) >= 3:
        return "ok", f"{len(prices)} harga terbaca dari halaman"
    return "empty", f"{len(text)} karakter, {len(prices)} harga: daftar produk dimuat belakangan oleh JavaScript"


def judge_shopee_api(status, text):
    if status == 0:
        return "error", "tidak terhubung"
    try:
        data = json.loads(text)
    except ValueError:
        return ("captcha" if CHALLENGE.search(text[:5000]) else "blocked"), f"status {status}, jawaban bukan JSON"
    err = data.get("error") if isinstance(data, dict) else None
    if err in SHOPEE_BLOCK_CODES:
        return "blocked", f"{SHOPEE_BLOCK_CODES[err]}: panggilan ditolak tanpa header anti-penipuan dari browser; tidak dilewati"
    if err:
        return "blocked", f"kode kesalahan {err}"
    items = data.get("items") or []
    if items:
        return "ok", f"{len(items)} produk dalam jawaban"
    return "empty", "jawaban normal tanpa produk"


def probe_site(name, base, search_path, out, sleep=time.sleep, api_path=None, query="EGS pokemon"):
    out(f"== {name}: {base}")
    status, _, text, err = get(base + "/robots.txt")
    out(f"robots.txt: status {status}" + (f" ({err})" if err else ""))
    for line in robots_lines(text):
        out("   " + line)
    sleep(2)
    url = base + search_path.format(q=quote(query))
    status, final, text, err = get(url)
    verdict, why = judge_page(status, final, text, err)
    out(f"halaman pencarian {url}\n   status {status} | {len(text)} karakter | {verdict.upper()}: {why}")
    verdicts = {"page": verdict}
    if api_path:
        sleep(2)
        aurl = base + api_path.format(q=quote(query))
        astatus, _, atext, aerr = get(aurl, accept="application/json")
        averdict, awhy = judge_shopee_api(astatus, atext)
        out(f"panggilan pencarian milik situs\n   {aurl[:110]}\n   status {astatus} | {averdict.upper()}: {awhy}")
        verdicts["api"] = averdict
    return verdicts


def probe_browser(name, url, out, wait_ms=9000):
    """Open the search page like a visitor would, read what renders, stop at a challenge or a login."""
    try:
        from playwright.sync_api import sync_playwright
        from probe_toco_data import launch
    except Exception as e:
        out(f"== {name} (browser): Playwright tidak tersedia ({type(e).__name__}); lewati")
        return "error"
    out(f"== {name} (browser): {url}")
    with sync_playwright() as p:
        browser = launch(p, out)
        page = browser.new_page(user_agent=UA, locale="id-ID")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(wait_ms)
            text, final = page.inner_text("body"), page.url
        except Exception as e:
            browser.close()
            out(f"   gagal membuka: {type(e).__name__}: {str(e)[:100]}")
            return "error"
        browser.close()
    verdict, why = judge_page(200, final, text, "")
    out(f"   {len(text)} karakter | {verdict.upper()}: {why}")
    if verdict in ("captcha", "login"):
        out("   berhenti di sini: tantangan atau halaman masuk tidak dilewati")
    return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--query", default="EGS pokemon")
    ap.add_argument("--tokopedia", default="https://www.tokopedia.com")
    ap.add_argument("--shopee", default="https://shopee.co.id")
    ap.add_argument("--browser", action="store_true", help="juga baca halaman dengan browser tanpa kepala (butuh Playwright)")
    a = ap.parse_args(argv)
    out = print
    sleep = lambda s: time.sleep(s)
    results = {
        "tokopedia": probe_site("Tokopedia", a.tokopedia.rstrip("/"), "/search?q={q}&st=product", out, sleep, query=a.query),
        "shopee": probe_site("Shopee", a.shopee.rstrip("/"), "/search?keyword={q}", out, sleep,
                             api_path="/api/v4/search/search_items?by=relevancy&keyword={q}&limit=20&newest=0&order=desc&page_type=search&scenario=PAGE_GLOBAL_SEARCH&version=2",
                             query=a.query)}
    if a.browser:
        results["tokopedia"]["browser"] = probe_browser("Tokopedia", a.tokopedia.rstrip("/") + f"/search?q={quote(a.query)}&st=product", out)
        results["shopee"]["browser"] = probe_browser("Shopee", a.shopee.rstrip("/") + f"/search?keyword={quote(a.query)}", out)
    out("\n== ringkasan")
    for site, v in results.items():
        usable = [k for k, x in v.items() if x == "ok"]
        out(f"{site}: " + ", ".join(f"{k}={x}" for k, x in v.items()) + (f"  -> bisa dipakai lewat: {', '.join(usable)}" if usable else "  -> belum ada jalur yang bisa dipakai tanpa melewati pembatasan"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
