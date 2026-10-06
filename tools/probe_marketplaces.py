#!/usr/bin/env python3
"""Probe Tokopedia, Shopee and other candidate sources: can listings be read at all, without logging in and without getting around a block?

Run it where the bot runs (the VPS): the answer depends on that address. It only reads: robots.txt, a public search page and,
for Shopee, the search call the website itself makes, a few requests per site, 2 seconds apart. Each site gets one verdict:

  ok          the page or call returned listings with titles and prices
  login       the site asks for a login: not used
  captcha     a challenge or verification page: not used, not solved, not worked around
  blocked     refused (403/429, an anti-bot error code): not worked around
  notfound    404/410: the address guessed for the site does not exist; a wrong path, not a block
  empty       answered normally but the listings are not in what was sent (they load later with JavaScript)
  error       network or server error

Only an "ok" is a source we can build on. For "empty" the optional --browser reads the rendered page with a headless browser exactly as
a visitor's browser would (Playwright needed); it stops at the first challenge or login.

Every site is probed under its robots.txt (patterns with * and $ understood): an address that robots.txt disallows for every bot is not requested at all
(verdict "robots"). For the other candidate sources (--candidates: Cardtell, Pokepedia, Carousell Indonesia; or --site NAME=BASE|SEARCH_PATH) the terms-of-use
links found on the page are printed as well, so that they are read before a collector is built. Reading a page is not permission to copy a site's data: that is a decision for the owner.

Usage:  python3 tools/probe_marketplaces.py [--query "EGS pokemon"] [--browser]
        python3 tools/probe_marketplaces.py --tokopedia https://www.tokopedia.com --shopee https://shopee.co.id
        python3 tools/probe_marketplaces.py --skip-marketplaces --candidates
        python3 tools/probe_marketplaces.py --skip-marketplaces --site "Toko X=https://example.id|/cari?q={q}"
"""
import argparse
import json
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CHALLENGE = re.compile(r"captcha|are you a robot|verify you are human|verifikasi|cf-chl|just a moment|geetest|slide to verify|_Incapsula_|akamai", re.I)
LOGIN = re.compile(r"/buyer/login|/login\b|masuk atau daftar|silakan masuk|log in to continue", re.I)
PRICE = re.compile(r"(?:Rp|US\s?\$|\$|\u00a5|JPY)\s?[\d.,]{3,}|[\d.,]{3,}\s?(?:\u5186|yen)", re.I)   # a price in rupiah, dollars, yen: SNKRDUNK shows "US $759~", Yuyutei "480 yen"
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


TERMS = re.compile(r"""<a\s[^>]*href=["']([^"'#]+)["'][^>]*>(.{0,80}?)</a>""", re.I | re.S)
TERMS_WORDS = re.compile(r"syarat|ketentuan|terms|privacy|privasi|kebijakan|robots", re.I)

# candidate sources besides Tokopedia and Shopee: (name, base address, search path with {q}); paths are best guesses: override with --site
CANDIDATES = (("Cardtell", "https://cardtell.id", "/"),
              ("Pokepedia", "https://pokepedia.id", "/marketplace"),
              ("Carousell Indonesia", "https://www.carousell.co.id", "/search/{q}"))


def robots_rules(text):
    """[(allow, pattern)] of the rules that apply to every bot ('User-agent: *'), in the order of the file."""
    groups, current, has_rules = [], None, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or has_rules:
                current, has_rules = {"agents": [], "rules": []}, False
                groups.append(current)
            current["agents"].append(value.lower())
        elif key in ("allow", "disallow") and current is not None:
            has_rules = True
            if value:                                   # an empty Disallow allows everything
                current["rules"].append((key == "allow", value))
    return [r for g in groups if "*" in g["agents"] for r in g["rules"]]


def pattern_regex(pattern):
    """A robots.txt path pattern as a regular expression: * is any run of characters, a trailing $ anchors the end (the widely used extensions)."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in body) + ("$" if anchored else ""))


def robots_allows(robots_text, url):
    """Whether robots.txt lets every bot fetch this address: the longest matching rule wins and Allow wins a tie; no matching rule means allowed.
    The standard library's parser does not know the * of a pattern (Tokopedia's 'Disallow: /search?*'), so this one does."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    best = None
    for allow, pattern in robots_rules(robots_text):
        if pattern_regex(pattern).match(path) and (best is None or (len(pattern), allow) > (best[0], best[1])):
            best = (len(pattern), allow)
    return True if best is None else best[1]


def terms_links(page, base, limit=4):
    """Addresses on a page that look like terms of use, privacy or robots pages, for a person to read."""
    out = []
    for href, label in TERMS.findall(page or ""):
        if TERMS_WORDS.search(href) or TERMS_WORDS.search(re.sub(r"<[^>]+>", " ", label)):
            link = urljoin(base + "/", href)
            if link not in out:
                out.append(link)
    return out[:limit]


def robots_lines(text, words=("search", "/api", "Crawl-delay", "User-agent: *"), limit=8):
    keep = [l.strip() for l in text.splitlines() if any(w.lower() in l.lower() for w in words)]
    return keep[:limit]


def judge_page(status, final_url, text, error):
    """(verdict, explanation) for a search page."""
    if status == 0:
        return "error", error
    if status in (404, 410):
        return "notfound", f"status {status}: alamat ini tidak ada di situsnya. Itu salah jalur, bukan pemblokiran; berikan alamat halaman yang benar (--site)"
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
    return "empty", f"{len(text)} karakter, {len(prices)} harga di isi halaman: dimuat belakangan oleh JavaScript, atau tertanam dalam bentuk lain (lihat isinya dengan tools/probe_pokemon_card.py --url ... --find)"


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


def probe_site(name, base, search_path, out, sleep=time.sleep, api_path=None, query="EGS pokemon", respect_robots=True):
    out(f"== {name}: {base}")
    status, _, text, err = get(base + "/robots.txt")
    out(f"robots.txt: status {status}" + (f" ({err})" if err else ""))
    for line in (robots_lines(text, ("allow", "disallow", "crawl-delay", "user-agent"), 25) if respect_robots else robots_lines(text)):
        out("   " + line)
    sleep(2)
    url = base + search_path.format(q=quote(query))
    robots_text, robots_ok = text, status == 200
    if respect_robots and robots_ok and not robots_allows(robots_text, url):
        out(f"robots.txt melarang bot membaca {url}: tidak dibaca. Izin harus datang dari pemilik situs.")
        return {"page": "robots"}
    status, final, text, err = get(url)
    verdict, why = judge_page(status, final, text, err)
    out(f"halaman pencarian {url}\n   status {status} | {len(text)} karakter | {verdict.upper()}: {why}")
    if respect_robots and name not in ("Tokopedia", "Shopee"):
        links = terms_links(text, base)
        out("syarat dan ketentuan: " + (", ".join(links) if links else "tautan tidak ditemukan di halaman ini; cari sendiri di situsnya") + "  (baca sebelum membangun pengambil)")
    verdicts = {"page": verdict}
    if api_path:
        sleep(2)
        aurl = base + api_path.format(q=quote(query))
        if respect_robots and robots_ok and not robots_allows(robots_text, aurl):
            out(f"robots.txt melarang bot memanggil {aurl[:100]}: tidak dipanggil")
            verdicts["api"] = "robots"
            return verdicts
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
    ap.add_argument("--candidates", action="store_true", help="juga uji Cardtell, Pokepedia dan Carousell Indonesia (menurut robots.txt mereka)")
    ap.add_argument("--site", action="append", default=[], metavar="NAMA=ALAMAT|JALUR", help="situs lain, mis. \"Toko=https://contoh.id|/cari?q={q}\" (jalur bawaan: /)")
    ap.add_argument("--skip-marketplaces", action="store_true", help="lewati Tokopedia dan Shopee")
    a = ap.parse_args(argv)
    out = print
    sleep = lambda s: time.sleep(s)
    results = {}
    if not a.skip_marketplaces:
        results = {
            "tokopedia": probe_site("Tokopedia", a.tokopedia.rstrip("/"), "/search?q={q}&st=product", out, sleep, query=a.query),
            "shopee": probe_site("Shopee", a.shopee.rstrip("/"), "/search?keyword={q}", out, sleep,
                                 api_path="/api/v4/search/search_items?by=relevancy&keyword={q}&limit=20&newest=0&order=desc&page_type=search&scenario=PAGE_GLOBAL_SEARCH&version=2",
                                 query=a.query)}
    others = list(CANDIDATES) if a.candidates else []
    for spec in a.site:
        name, _, rest = spec.partition("=")
        base, _, path = rest.partition("|")
        if not name or not base.startswith("http"):
            out(f"--site '{spec}' dilewati: bentuknya NAMA=https://alamat|/jalur")
            continue
        others.append((name.strip(), base.strip(), path.strip() or "/"))
    for name, base, path in others:
        results[name] = probe_site(name, base.rstrip("/"), path, out, sleep, query="psa pokemon" if "carousell" in base else a.query)
    if a.browser and not a.skip_marketplaces:
        for key, base, path in (("tokopedia", a.tokopedia, f"/search?q={quote(a.query)}&st=product"), ("shopee", a.shopee, f"/search?keyword={quote(a.query)}")):
            if results[key].get("page") == "robots":
                out(f"== {key} (browser): dilewati, robots.txt melarang halaman pencarian itu")
                results[key]["browser"] = "robots"
            else:
                results[key]["browser"] = probe_browser(key.capitalize(), base.rstrip("/") + path, out)
    out("\n== ringkasan")
    for site, v in results.items():
        usable = [k for k, x in v.items() if x == "ok"]
        verdict_text = (f"  -> bisa dipakai lewat: {', '.join(usable)}" if usable
                        else "  -> jalur halaman salah (404), BUKAN pemblokiran: kirim alamat halaman yang benar dan uji lagi (--site)" if v.get("page") == "notfound"
                        else "  -> belum ada jalur yang bisa dipakai tanpa melewati pembatasan")
        out(f"{site}: " + ", ".join(f"{k}={x}" for k, x in v.items()) + verdict_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
