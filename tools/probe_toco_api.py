#!/usr/bin/env python3
"""Fifth probe: find toco's product search request, check it can be replayed without a browser,
read a listing's server-side data (adDetail: description, photos), and test the original photo address.

Opens toco.id in a headless browser (Playwright) once, then uses plain HTTP. Read-only, writes no files.
Run: python3 probe_toco_api.py        (EGS_CHROME=/path/to/chrome to force a browser)
"""
import copy
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://toco.id"
MIN_ITEMS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
API = "/api/toco-api/search"
SELECTOR = 'a[href*="/listing/"]'
UA = "Mozilla/5.0 (compatible; egs-price-reports/1.0)"
ORIGINAL_HOSTS = ("https://d13eru2cpf9i9v.cloudfront.net/", "https://pix.toco.id/")


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
    if isinstance(obj, dict):
        if depth >= 2:
            return f"dict({len(obj)})"
        return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(obj.items())[:14]) + "}"
    if isinstance(obj, list):
        return f"list({len(obj)})" + (f"<{shape(obj[0], depth + 1)}>" if obj and depth < 2 else "")
    return type(obj).__name__


def items_of(obj, depth=0):
    """The longest list of dicts anywhere inside obj."""
    best = []
    if depth > 6:
        return best
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        best = obj
    for v in (obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []):
        found = items_of(v, depth + 1)
        if len(found) > len(best):
            best = found
    return best


def http(url, body=None, referer=None, binary=False):
    headers = {"User-Agent": UA, "Accept-Language": "id,en;q=0.5"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Accept": "application/json",
                        "Origin": BASE, "Referer": referer or BASE})
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=30) as r:
            raw = r.read(6_000_000)
            return r.status, (len(raw) if binary else raw.decode("utf-8", "replace")), r.headers.get("Content-Type", "")
    except HTTPError as e:
        return e.code, 0 if binary else e.read(1500).decode("utf-8", "replace"), ""
    except Exception as e:
        return 0, 0 if binary else f"{type(e).__name__}: {e}", ""


def paged(body, step):
    """Copy of a search body moved to the next page, if it has an offset or page field."""
    b = copy.deepcopy(body)
    inner = b.get("body") if isinstance(b.get("body"), dict) else b
    for key in ("offset", "from", "skip"):
        if key in inner:
            inner[key] = int(inner[key]) + step
            return b, f"{key}+{step}"
    for key in ("page", "pageQuery"):
        if key in inner:
            inner[key] = int(inner[key]) + 1
            return b, f"{key}+1"
    return None, "tidak ada kolom offset/page"


def run(out=print, base=BASE, min_items=MIN_ITEMS):
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
        page.wait_for_timeout(2500)
        first_href = page.evaluate(f"() => (document.querySelector('{SELECTOR}') || {{}}).href || null")
        thumb = page.evaluate(f"() => {{ const i = document.querySelector('{SELECTOR} img'); return i ? i.src : null }}")

        out(f"== 1. semua permintaan ke {API} ({len(hits)})")
        best = None
        for r in hits:
            raw = r.request.post_data or ""
            try:
                body = json.loads(raw or "{}")
                data = r.json()
            except Exception:
                body, data = {}, None
            items = items_of(data) if data is not None else []
            out(f"- path={body.get('path')} item={len(items)} permintaan: {raw[:240]}")
            if len(items) >= min_items and (best is None or len(items) > len(best[2])):
                best = (raw, body, items, data)

        if best:
            raw, body, items, data = best
            out("== 2. permintaan pencarian produk (yang terbesar)")
            out("isi lengkap: " + raw[:1500])
            out("bentuk jawaban: " + shape(data)[:700])
            out("item pertama: " + json.dumps(items[0], ensure_ascii=False)[:2400])
            out("== 3. diulang tanpa browser (HTTP biasa)")
            st, txt, _ = http(base + API, body=body, referer=url1)
            try:
                again = items_of(json.loads(txt))
            except ValueError:
                again = []
            out(f"status={st} item={len(again)} (di browser: {len(items)})" + ("" if again else f" awal: {txt[:200]!r}"))
            nxt, how = paged(body, len(items))
            if nxt:
                st2, txt2, _ = http(base + API, body=nxt, referer=url1)
                try:
                    more = items_of(json.loads(txt2))
                except ValueError:
                    more = []
                seen = {json.dumps(x, sort_keys=True) for x in items}
                out(f"halaman berikutnya ({how}): status={st2} item={len(more)} baru={sum(json.dumps(x, sort_keys=True) not in seen for x in more)}")
            else:
                out(f"halaman berikutnya: {how}")
        else:
            out(f"(tidak ada permintaan dengan {min_items} item atau lebih)")

        if first_href:
            out(f"== 4. data halaman listing lewat HTTP biasa: {first_href}")
            st, html, _ = http(first_href)
            m = re.search(r'<script id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
            out(f"status={st} ukuran={len(html)} __NEXT_DATA__={'ada' if m else 'tidak ada'}")
            if m:
                try:
                    ad = ((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}).get("adDetail")
                except ValueError:
                    ad = None
                if ad is not None:
                    flat = json.dumps(ad, ensure_ascii=False)
                    out("kunci adDetail: " + str(list(ad)[:40] if isinstance(ad, dict) else type(ad).__name__))
                    out("adDetail (awal): " + flat[:2200])
                    urls = list(dict.fromkeys(re.findall(r'https?://[^"\\\s]+?\.(?:webp|jpe?g|png)[^"\\\s]*', flat)))[:10]
                    out(f"alamat gambar di adDetail ({len(urls)}):")
                    for u in urls:
                        out("  " + u[:240])

        out("== 5. foto asli dari foto kecil di hasil pencarian")
        out("foto kecil: " + (thumb or "-")[:240])
        m = re.search(r"/(toco/[^?/]+\.(?:webp|jpe?g|png))", thumb or "")
        if not m:
            out("tidak ada alamat toco/... untuk diturunkan")
        else:
            for host in ORIGINAL_HOSTS:
                url = host + m.group(1)
                st, size, ctype = http(url, binary=True)
                dims = ""
                if st == 200:
                    ip = browser.new_page()
                    try:
                        ip.goto(url, timeout=30000)
                        dims = " ukuran " + "x".join(str(v) for v in ip.evaluate(
                            "() => [document.images[0].naturalWidth, document.images[0].naturalHeight]"))
                    except Exception:
                        pass
                out(f"  {url[:130]} -> status={st} {ctype[:20]} {size} byte{dims}")
        browser.close()
    out("== selesai. Kirim seluruh output ini.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
