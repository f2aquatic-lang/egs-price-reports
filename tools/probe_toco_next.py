#!/usr/bin/env python3
"""Second probe: toco.id is a Next.js app and its search results are loaded by JavaScript.

Finds where the data comes from: the __NEXT_DATA__ block, the /_next/data route, and the
API addresses written inside the page's JavaScript files. Standalone (standard library only),
read-only, about 15 GET requests for public files, 1 second apart.   Run: python3 probe_toco_next.py
"""
import json
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE = "https://toco.id"
UA = "Mozilla/5.0 (compatible; egs-price-reports/1.0)"
INTEREST = re.compile(r"api|graphql|search|listing|product|typesense|algolia|elastic|meili|catalog", re.I)
NOISE = re.compile(r"w3\.org|reactjs\.org|nextjs\.org|github\.com|schema\.org|mozilla\.org|googleapis\.com/css|"
                   r"example\.com|localhost")
URL_RX = re.compile(r"https?://[A-Za-z0-9_.~:/?#@!$&*+,;=%\-]{6,200}")
PATH_RX = re.compile(r"[\"'`](/(?:api|v\d)/[A-Za-z0-9_\-./{}$]+)[\"'`]")


def http_get(url, limit=3_000_000):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "id,en;q=0.5"})
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, r.read(limit).decode("utf-8", "replace"), ""
    except HTTPError as e:
        return e.code, e.read(200_000).decode("utf-8", "replace"), f"HTTP {e.code}"
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {e}"


def run(get=http_get, sleep=time.sleep, out=print, base=BASE):
    status, html, err = get(f"{base}/search?q=egs")
    out(f"== 1. halaman pencarian: status={status} ukuran={len(html)} {err}".rstrip())
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    data = None
    if m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            out("__NEXT_DATA__ bukan JSON yang sah")
    out("memakai app router (self.__next_f):", "self.__next_f" in html)
    if data:
        pp = (data.get("props") or {}).get("pageProps") or {}
        out(f"page={data.get('page')} buildId={data.get('buildId')} locale={data.get('locale')} query={data.get('query')}")
        out(f"kunci pageProps: {list(pp)[:15]}")
        out("potongan: " + json.dumps(data, ensure_ascii=False)[:700])

    chunks = [u for u in (urljoin(base, x) for x in re.findall(r'<script[^>]+src=["\']([^"\']+)', html)) if "/_next/" in u]
    out(f"== 2. berkas JavaScript: {len(chunks)}")
    found = {}
    for u in chunks[:14]:
        sleep(1)
        _, js, _ = get(u, 2_000_000)
        for x in URL_RX.findall(js) + PATH_RX.findall(js):
            if INTEREST.search(x) and not NOISE.search(x):
                found.setdefault(x, u.rsplit("/", 1)[-1][:30])
    out(f"== 3. alamat menarik di dalam JavaScript: {len(found)}")
    for x, src in list(found.items())[:40]:
        out(f"  {x}   <- {src}")

    if data and data.get("buildId"):
        out("== 4. rute data Next.js")
        for path in (f"/_next/data/{data['buildId']}/search.json?q=egs",
                     f"/_next/data/{data['buildId']}/id/search.json?q=egs"):
            sleep(1)
            st, body, e = get(base + path)
            out(f"{path} -> status={st} ukuran={len(body)} awal: {body[:250]!r}")
    out("== selesai. Kirim seluruh output ini.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
