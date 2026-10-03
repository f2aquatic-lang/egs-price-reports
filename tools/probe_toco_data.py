#!/usr/bin/env python3
"""Fourth probe: what toco's search request looks like, what each result contains, and where the
big listing photos are. Opens toco.id in a headless browser (Playwright). Read-only, writes no files,
about 3 page loads.   Run: python3 probe_toco_data.py     (EGS_CHROME=/path/to/chrome to force a browser)
"""
import json
import os
import re
import sys
from urllib.parse import quote_plus

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://toco.id"
API = "/api/toco-api/search"
SELECTOR = 'a[href*="/listing/"]'
IMGS = """() => [...document.images].map(i => ({src: i.currentSrc || i.src, nw: i.naturalWidth, nh: i.naturalHeight,
  w: Math.round(i.getBoundingClientRect().width)}))"""


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


def shape(obj, depth=0):
    """Compact description of a JSON value: types, lengths and keys."""
    if isinstance(obj, dict):
        if depth >= 2:
            return f"dict({len(obj)})"
        return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(obj.items())[:14]) + "}"
    if isinstance(obj, list):
        return f"list({len(obj)})" + (f"<{shape(obj[0], depth + 1)}>" if obj and depth < 2 else "")
    return type(obj).__name__


def run(out=print, base=BASE):
    from playwright.sync_api import sync_playwright
    url1 = f"{base}/search?q={quote_plus('egs')}"
    with sync_playwright() as p:
        browser = launch(p, out)
        page = browser.new_page()
        hits = []
        page.on("response", lambda r: hits.append(r) if API in r.url else None)
        page.goto(url1, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector(SELECTOR, timeout=25000)
        except Exception:
            out("(tidak ada tautan listing setelah 25 detik)")
        page.wait_for_timeout(2000)
        first_url = page.evaluate(f"() => (document.querySelector('{SELECTOR}') || {{}}).href || null")

        out(f"== 1. permintaan pencarian ({len(hits)} kali ke {API})")
        for r in hits[:1]:
            req = r.request
            out(f"{req.method} {req.url[:160]}")
            out("isi permintaan: " + (req.post_data or "-")[:700])
            try:
                data = r.json()
            except Exception as e:
                out("JSON tidak terbaca:", str(e)[:100])
                continue
            out("bentuk jawaban: " + shape(data)[:900])
            out(f"total={data.get('total') if isinstance(data, dict) else '-'}")
            out("awal jawaban: " + json.dumps(data, ensure_ascii=False)[:1800])

        if first_url:
            out(f"== 2. foto dan teks di halaman listing: {first_url}")
            lp = browser.new_page()
            lp.goto(first_url, wait_until="domcontentloaded", timeout=45000)
            lp.wait_for_timeout(4000)
            imgs = lp.evaluate(IMGS)
            big = sorted((i for i in imgs if i["nw"] >= 200), key=lambda i: -i["nw"])
            out(f"gambar di halaman: {len(imgs)}; berukuran 200 piksel atau lebih: {len(big)}")
            for i in big[:8]:
                out(f"  {i['nw']}x{i['nh']} (tampil {i['w']}px) {i['src'][:240]}")
            nd = lp.evaluate("() => { const e = document.getElementById('__NEXT_DATA__'); return e ? e.textContent : null }")
            if nd:
                try:
                    pp = (json.loads(nd).get("props") or {}).get("pageProps") or {}
                    out("kunci pageProps halaman listing: " + str(list(pp)[:15]))
                except ValueError:
                    pass
                urls = sorted(set(re.findall(r'https?://pix\.toco\.id[^"\\\s]+', nd)))[:8]
                out(f"alamat foto di dalam __NEXT_DATA__ ({len(urls)}):")
                for u in urls:
                    out("  " + u[:240])
            text = " ".join(lp.evaluate("() => document.body.innerText").split())
            for label in ("Kondisi", "Deskripsi"):
                m = re.search(rf".{{0,40}}{label}.{{0,500}}", text)
                out(f"teks sekitar '{label}': " + (m.group(0) if m else "-"))
        browser.close()
    out("== selesai. Kirim seluruh output ini.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
