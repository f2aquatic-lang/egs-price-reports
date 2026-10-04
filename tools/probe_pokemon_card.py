#!/usr/bin/env python3
"""Probe the official Indonesian card database (https://asia.pokemon-card.com/id/card-search/).

Run it where the site is reachable (the VPS); it only reads, one request per second. It prints what the pages
offer so the real fetcher can be written against facts: links, images, filter controls (the expansion list),
hints of a JSON/API behind the page, and the text of one card page.

Usage: python3 probe_pokemon_card.py [--base https://asia.pokemon-card.com] [--delay 1] [--snippets]
       python3 probe_pokemon_card.py --url URL [--url URL ...] [--find REGEX] [--text 800]
  --url      look at any page instead (for example a price list): robots.txt of its host, links, images, and the text;
             with --find, pieces of the page source around every match of REGEX (for example '\\d[\\d,]*\\s*円')
  --snippets  instead of the overview, print short pieces of the page source (an expansion tile, a card in the
              list, the number / rarity / expansion parts of a card page) to see exactly how the data is marked up.
Needs only the Python standard library.
"""
import argparse
import re
import sys
import time
from collections import Counter, defaultdict
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BASE = "https://asia.pokemon-card.com"


def get(url, timeout=30, limit=3_000_000):
    """(status, content-type, text). Never raises: a failure is a status of 0 and the reason as text."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                                "Accept-Language": "id,en;q=0.8"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(limit).decode("utf-8", "replace")
    except HTTPError as e:
        try:
            body = e.read(2000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", body
    except (URLError, OSError) as e:
        return 0, "", f"{type(e).__name__}: {e}"


class Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.links, self.images, self.controls = "", [], [], defaultdict(list)
        self.script_src, self.inline, self.text = [], [], []
        self._in = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("title", "script", "style"):
            self._in = tag
            if tag == "script" and a.get("src"):
                self.script_src.append(a["src"])
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            for key in ("src", "data-src", "data-original"):
                if a.get(key):
                    self.images.append(a[key])
                    break
        elif tag == "input" and a.get("name"):
            self.controls[a["name"]].append(a.get("value", ""))
        elif tag == "select":
            self._select = a.get("name", "?")
        elif tag == "option" and self._select is not None:
            self.controls[self._select].append(a.get("value", ""))

    def handle_endtag(self, tag):
        if tag in ("title", "script", "style"):
            self._in = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "script":
            if data.strip():
                self.inline.append(data)
        elif self._in is None and data.strip():
            self.text.append(data.strip())


def pattern(url):
    """Shape of a URL: numbers become N, query keys kept without values."""
    p = urlsplit(url)
    path = re.sub(r"\d+", "N", p.path)
    keys = ",".join(sorted(k for k, _ in parse_qsl(p.query)))
    return path + (f"?{keys}" if keys else "")


def top(urls, n):
    counts, example = Counter(), {}
    for u in urls:
        k = pattern(u)
        counts[k] += 1
        example.setdefault(k, u)
    return [(k, c, example[k]) for k, c in counts.most_common(n)]


def describe(url, status, ctype, body, out=print):
    out(f"\n=== {url}\nstatus {status} | {ctype} | {len(body)} karakter")
    if status != 200:
        out("isi (awal): " + re.sub(r"\s+", " ", body)[:300])
        return None
    scan = Scan()
    try:
        scan.feed(body)
    except Exception as e:  # malformed pages must not stop the probe
        out(f"(pembaca HTML berhenti: {e})")
    out(f"judul: {scan.title.strip()[:120]!r}")
    out(f"tautan: {len(scan.links)}")
    for k, c, ex in top([urljoin(url, h) for h in scan.links if not h.startswith(("#", "javascript:", "mailto:"))], 14):
        out(f"  {c:4d}x {k}   contoh: {ex[:110]}")
    out(f"gambar: {len(scan.images)}")
    for k, c, ex in top([urljoin(url, s) for s in scan.images], 6):
        out(f"  {c:4d}x {k}   contoh: {ex[:110]}")
    if scan.controls:
        out("kontrol formulir (nama: jumlah nilai, contoh):")
        for name, values in list(scan.controls.items())[:25]:
            uniq = list(dict.fromkeys(values))
            out(f"  {name}: {len(uniq)} nilai, mis. {uniq[:12]}")
    if scan.script_src:
        out("skrip: " + ", ".join(scan.script_src[:8]))
    hints = []
    for block in scan.inline:
        for line in re.split(r"[;\n]", block):
            if re.search(r"expansion|series|__NEXT_DATA__|window\.__|fetch\(|axios|/api/|\.json", line, re.I):
                hints.append(re.sub(r"\s+", " ", line.strip())[:220])
    for h in list(dict.fromkeys(hints))[:10]:
        out(f"  petunjuk skrip: {h}")
    out("teks (awal): " + re.sub(r"\s+", " ", " ".join(scan.text))[:500])
    return scan


def windows(html, pattern, before, after, limit):
    """Up to `limit` pieces of source around matches of `pattern`, whitespace collapsed."""
    out = []
    for m in re.finditer(pattern, html):
        piece = html[max(0, m.start() - before): m.end() + after]
        out.append(re.sub(r"\s+", " ", piece)[:1100])
        if len(out) >= limit:
            break
    return out


def show(title, pieces, out=print):
    out(f"\n--- {title} ({len(pieces)})")
    for piece in pieces:
        out("  > " + piece)
    if not pieces:
        out("  (tidak ditemukan)")


def snippets(base, wait, out=print):
    home, listing = base + "/id/card-search/", base + "/id/card-search/list/?pageNo=1&expansionCodes=MA1"
    status, _, body = get(home)
    out(f"=== {home}: status {status}")
    if status == 200:
        show("kotak set (tautan expansionCodes)", windows(body, r'expansionCodes=[A-Za-z0-9]+', 350, 450, 2), out)
        show("pagination set (pageNo=2)", windows(body, r'\?pageNo=2', 200, 250, 1), out)
    wait()
    status, _, body = get(listing)
    out(f"\n=== {listing}: status {status}")
    detail = None
    if status == 200:
        show("satu kartu di daftar", windows(body, r'/detail/\d+/', 350, 550, 2), out)
        show("jumlah hasil", windows(body, r'Hasil Pencarian\s*\d', 150, 300, 1), out)
        m = re.search(r'/id/card-search/detail/\d+/', body)
        detail = urljoin(listing, m.group(0)) if m else None
    wait()
    if detail:
        status, _, body = get(detail)
        out(f"\n=== {detail}: status {status}")
        if status == 200:
            show("judul dan h1", windows(body, r'<title>|<h1', 0, 300, 2), out)
            show("nomor kartu", windows(body, r'\b\d{1,3}\s*/\s*\d{2,3}\b', 500, 450, 1), out)
            show("kode set (expansionCodes)", windows(body, r'expansionCodes=', 350, 350, 1), out)
            show("kata rarity", windows(body, r'(?i)rarity|kelangkaan|rare', 200, 300, 3), out)
            show("gambar kartu", windows(body, r'card-img/', 250, 250, 1), out)
    return 0


def probe_urls(urls, find, text_chars, wait, out=print, brief=False):
    """Any pages: the host's robots.txt (once), then the overview of each page and, with `find`, pieces of its source."""
    robots_done = set()
    for url in urls:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in robots_done:
            robots_done.add(host)
            status, _, body = get(host + "/robots.txt")
            out(f"=== {host}/robots.txt: status {status}")
            out(body[:900] if status == 200 else re.sub(r"\s+", " ", body)[:200])
            wait()
        status, ctype, body = get(url)
        if brief:  # only the pieces around the matches, not the overview of the page
            out(f"\n=== {url}: status {status} | {len(body)} karakter")
            scan = None
        else:
            scan = describe(url, status, ctype, body, out)
        if status == 200 and find:
            show(f"sumber di sekitar {find!r}", windows(body, find, 250, 350, 4), out)
        if scan and text_chars > 500:
            out("teks (lebih panjang): " + re.sub(r"\s+", " ", " ".join(scan.text))[:text_chars])
        wait()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--snippets", action="store_true", help="print pieces of the page source instead of the overview")
    ap.add_argument("--url", action="append", help="probe this page instead of the card database (repeatable)")
    ap.add_argument("--find", help="with --url: print pieces of the source around matches of this regular expression")
    ap.add_argument("--text", type=int, default=500, help="with --url: how many characters of page text to print")
    ap.add_argument("--brief", action="store_true", help="with --url: skip the overview, print only the pieces around --find")
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    wait = lambda: time.sleep(a.delay)
    if a.url:
        return probe_urls(a.url, a.find, a.text, wait, brief=a.brief)
    if a.snippets:
        return snippets(base, wait)

    status, ctype, body = get(base + "/robots.txt")
    print(f"=== robots.txt: status {status}")
    print(body[:700] if status == 200 else re.sub(r"\s+", " ", body)[:200])
    wait()

    home = base + "/id/card-search/"
    status, ctype, body = get(home)
    scan = describe(home, status, ctype, body)
    wait()

    detail = None
    for url in (base + "/id/card-search/list/?pageNo=1", base + "/id/card-search/list/?pageNo=2&expansionCodes=MA1"):
        status, ctype, body = get(url)
        got = describe(url, status, ctype, body)
        if got and not detail:
            detail = next((urljoin(url, h) for h in got.links if "detail" in h), None)
        wait()
    if not detail and scan:
        detail = next((urljoin(home, h) for h in scan.links if "detail" in h), None)
    if detail:
        status, ctype, body = get(detail)
        describe(detail, status, ctype, body)
    else:
        print("\n(tidak ada tautan kartu yang bisa dibuka; lihat bagian tautan di atas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
