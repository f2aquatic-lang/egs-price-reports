#!/usr/bin/env python3
"""Third probe: open toco.id in a headless browser (Playwright) and report what a browser sees.

toco renders its search results with JavaScript, so this checks: do listing links appear after
rendering, which data requests the page makes, whether page=2 or scrolling loads more, and what
a listing page contains (photo, text, any serial number). Read-only, writes no files, about 4 page
loads. Standalone apart from the Playwright package the VPS already has.

  python3 probe_toco_browser.py            # toco.id
  EGS_CHROME=/path/to/chrome python3 probe_toco_browser.py   # force a browser binary
"""
import json
import os
import re
import sys
from urllib.parse import quote_plus

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://toco.id"
SELECTOR = 'a[href*="/listing/"]'
SERIAL = re.compile(r".{0,50}(?:serial|no\.? ?seri|nomor seri|sertifikat|cert\b|cert no).{0,70}", re.I)
GRAB = """() => [...document.querySelectorAll('a[href*="/listing/"]')].map(a => ({
  href: a.href, text: a.innerText.replace(/\\s+/g, ' ').trim(),
  img: (a.querySelector('img') || {}).src || null, html: a.outerHTML.slice(0, 500)}))"""


def launch(p, out=print):
    args = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    paths = [os.environ.get("EGS_CHROME"), "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    try:
        return p.chromium.launch(headless=True, args=args)
    except Exception as e:
        out("peluncuran bawaan gagal:", str(e).strip().splitlines()[0][:140])
    for path in paths:
        if path and os.path.exists(path):
            try:
                out("memakai", path)
                return p.chromium.launch(headless=True, executable_path=path, args=args)
            except Exception as e:
                out("gagal:", str(e).strip().splitlines()[0][:140])
    raise SystemExit("Tidak ada browser yang bisa dijalankan.")


def first_list(obj, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, list) and len(obj) >= 3 and all(isinstance(x, dict) for x in obj):
        return obj
    items = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for v in items:
        found = first_list(v, depth + 1)
        if found:
            return found
    return None


def listings(page, out=print):
    try:
        page.wait_for_selector(SELECTOR, timeout=25000)
    except Exception:
        out("  (tidak ada tautan listing setelah 25 detik)")
    page.wait_for_timeout(1500)
    return page.evaluate(GRAB)


def run(out=print, base=BASE):
    from playwright.sync_api import sync_playwright
    url1 = f"{base}/search?q={quote_plus('egs')}"
    with sync_playwright() as p:
        browser = launch(p, out)
        page = browser.new_page()
        calls = []

        def on_response(r):
            if r.request.resource_type in ("xhr", "fetch"):
                calls.append(r)
        page.on("response", on_response)

        out(f"== 1. halaman pencarian dirender: {url1}")
        page.goto(url1, wait_until="domcontentloaded", timeout=45000)
        cards = listings(page, out)
        urls1 = {c["href"] for c in cards}
        out(f"listing terlihat={len(urls1)}  dengan harga={sum('Rp' in c['text'] for c in cards)}  "
            f"dengan foto={sum(bool(c['img']) for c in cards)}")
        for c in cards[:3]:
            out(f" - ...{c['href'][-55:]} | {c['text'][:150]} | foto: {c['img'] or '-'}")
        if cards:
            out("HTML tautan pertama:")
            out(cards[0]["html"].replace("\n", " "))

        out("== 2. permintaan data yang dibuat halaman (xhr/fetch)")
        seen = set()
        biggest = None
        for r in calls:
            key = (r.request.method, r.url.split("?")[0])
            if key in seen:
                continue
            seen.add(key)
            ctype = (r.headers.get("content-type") or "")[:25]
            out(f"  {r.request.method} {r.status} {r.url[:170]} [{ctype}]")
            if "json" in ctype and r.status == 200 and ("search" in r.url or "product" in r.url or "listing" in r.url):
                biggest = biggest or r
            if len(seen) >= 18:
                break
        if biggest is not None:
            try:
                data = biggest.json()
                out(f"contoh JSON dari {biggest.url[:120]}")
                out(f"  kunci atas: {list(data)[:12] if isinstance(data, dict) else type(data).__name__}")
                rows = first_list(data)
                if rows:
                    out(f"  daftar {len(rows)} item; kunci item pertama: {list(rows[0])[:30]}")
                    out("  item pertama: " + json.dumps(rows[0], ensure_ascii=False)[:700])
            except Exception as e:
                out("  JSON tidak terbaca:", str(e)[:100])

        out("== 3. halaman berikutnya dan scroll")
        before = len(urls1)
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(3500)
        after = len({c["href"] for c in page.evaluate(GRAB)})
        out(f"setelah scroll: listing {before} -> {after}")
        p2 = browser.new_page()
        p2.goto(f"{url1}&page=2", wait_until="domcontentloaded", timeout=45000)
        urls2 = {c["href"] for c in listings(p2, out)}
        out(f"&page=2 -> listing={len(urls2)} baru dibanding halaman 1={len(urls2 - urls1)}")

        if cards:
            out(f"== 4. halaman listing: {cards[0]['href']}")
            lp = browser.new_page()
            lp.goto(cards[0]["href"], wait_until="domcontentloaded", timeout=45000)
            lp.wait_for_timeout(3500)
            og = None
            try:
                og = lp.eval_on_selector('meta[property="og:image"]', "e => e.content")
            except Exception:
                pass
            text = " ".join(lp.evaluate("() => document.body.innerText").split())
            out(f"judul: {lp.title()[:100]}")
            out(f"og:image: {og or '-'}  jumlah <img>: {lp.evaluate('() => document.images.length')}")
            out("teks halaman (700 huruf pertama): " + text[:700])
            hits = [" ".join(m.group(0).split()) for m in SERIAL.finditer(text)][:6]
            out("kata 'serial/sertifikat':" if hits else "tidak ada kata 'serial/sertifikat' di teks halaman.")
            for h in hits:
                out(f"   ... {h}")
        browser.close()
    out("== selesai. Kirim seluruh output ini.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
