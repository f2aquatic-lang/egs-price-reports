#!/usr/bin/env python3
"""Probe toco.id and print a short report. Standalone: standard library only, no key, no repo needed.

Sends about 6 GET requests, 2 seconds apart, and writes no files. Run it on the VPS and send the
whole output back:   python3 probe_toco.py

It checks what the RAW collector has to guess: how the search page is built, which URL parameter
pages through the results, where listing photos are, and whether a serial number appears in text.
"""
import html as htmllib
import re
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

BASE = "https://toco.id"
UA = "Mozilla/5.0 (compatible; egs-price-reports/1.0)"
SERIAL = re.compile(r".{0,50}(?:serial|no\.? ?seri|nomor seri|sertifikat|cert\b|cert no).{0,70}", re.I)


def http_get(url):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "id,en;q=0.5"})
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, r.read(3_000_000).decode("utf-8", "replace"), ""
    except HTTPError as e:
        return e.code, e.read(200_000).decode("utf-8", "replace"), f"HTTP {e.code}"
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {e}"


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.cards, self._cur, self._text = base, {}, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "/listing/" in (a.get("href") or ""):
            u = urlsplit(urljoin(self.base, a["href"]))
            self._cur = urlunsplit((u.scheme, u.netloc, u.path, "", ""))
            self._text = []
            self.cards.setdefault(self._cur, {"url": self._cur, "text": "", "image": None})
        elif tag == "img" and self._cur:
            src = a.get("src") or a.get("data-src") or (a.get("srcset") or "").split(" ")[0]
            if src and not src.startswith("data:") and not self.cards[self._cur]["image"]:
                self.cards[self._cur]["image"] = urljoin(self.base, src)

    def handle_data(self, data):
        if self._cur:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._cur:
            text = " ".join(" ".join(self._text).split())
            if len(text) > len(self.cards[self._cur]["text"]):
                self.cards[self._cur]["text"] = text
            self._cur = None


def parse(html, base):
    p = Links(base)
    p.feed(html)
    return [c for c in p.cards.values() if c["text"]]


def visible_text(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def title_of(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return " ".join(m.group(1).split())[:100] if m else "-"


def probe(get=http_get, sleep=time.sleep, out=print, base=BASE, query="egs"):
    url1 = f"{base}/search?q={quote_plus(query)}"
    out(f"== 1. halaman pencarian: {url1}")
    status, html, err = get(url1)
    out(f"status={status} ukuran={len(html)} {err}".rstrip())
    if not html:
        out("Tidak ada isi: diblokir atau tidak ada jaringan dari VPS ini.")
        return 1
    block = re.search(r"just a moment|cf-chl|captcha|access denied|attention required", html, re.I)
    if block:
        out(f"PERHATIAN: tampak halaman penghalang ({block.group(0)}).")
    out(f"judul: {title_of(html)}")
    out("penanda:", {k: (k in html) for k in ("__NEXT_DATA__", "/_next/", "application/ld+json", "og:image")})
    low = html.lower()
    out(f"tag <a>={low.count('<a ')}  tag <img>={low.count('<img')}")
    cards = parse(html, base)
    out(f"listing terbaca={len(cards)}  dengan harga={sum('Rp' in c['text'] for c in cards)}  "
        f"dengan foto={sum(bool(c['image']) for c in cards)}")
    for c in cards[:3]:
        out(f" - ...{c['url'][-55:]} | {c['text'][:150]} | foto: {c['image'] or '-'}")
    m = re.search(r"<a\b[^>]*/listing/", html)
    if m:
        out("HTML tautan pertama:")
        out(html[m.start():m.start() + 600].replace("\n", " "))

    out("== 2. parameter halaman")
    first = {c["url"] for c in cards}
    for name in ("page", "p", "halaman"):
        sleep(2)
        st, h2, e2 = get(f"{url1}&{name}=2")
        got = {c["url"] for c in parse(h2, base)} if h2 else set()
        out(f"&{name}=2 -> status={st} listing={len(got)} baru dibanding halaman 1={len(got - first)} {e2}".rstrip())

    if cards:
        sleep(2)
        lurl = cards[0]["url"]
        out(f"== 3. halaman listing: {lurl}")
        st, h3, e3 = get(lurl)
        out(f"status={st} ukuran={len(h3)} {e3}".rstrip())
        if h3:
            og = re.search(r'og:image["\'][^>]*content=["\']([^"\']+)', h3, re.I) or \
                re.search(r'content=["\']([^"\']+)["\'][^>]*og:image', h3, re.I)
            out(f"judul: {title_of(h3)}")
            out(f"og:image: {og.group(1) if og else '-'}  tag <img>={h3.lower().count('<img')}")
            hits = [" ".join(m.group(0).split()) for m in SERIAL.finditer(visible_text(h3))][:6]
            out("kata 'serial/sertifikat' di teks halaman:" if hits else "tidak ada kata 'serial/sertifikat' di teks halaman.")
            for h in hits:
                out(f"   ... {h}")
    out("== selesai. Kirim seluruh output ini.")
    return 0


if __name__ == "__main__":
    sys.exit(probe())
