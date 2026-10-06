#!/usr/bin/env python3
"""Fetch card data and market prices from TCGdex (api.tcgdex.net, a free open API made for developers) into tcgdex.json.

Why: the Indonesian official catalog has names, numbers and rarities, the Japanese shop (Yuyutei) has Japanese prices, and neither knows the other's
numbering (Mega Charizard X ex SAR is 110/080 in Japan and 121/103 in Indonesia). TCGdex has the English and the Japanese edition of a card with
its illustrator, Pokedex number, rarity, and the Cardmarket / TCGplayer market prices of the English card, so it can be the bridge and the English
reference. Only reading: one request per --delay seconds, a budget per run, a SQLite cache so that a stopped run continues and a card is not
fetched twice. The Indonesian edition of TCGdex is not used: it lacks the newest sets and carries no rarity, Pokedex number or price.

Usage:
  python3 tools/fetch_tcgdex.py --list                                   # the sets that are fetched by default
  python3 tools/fetch_tcgdex.py --sets ja:M2 --max-requests 30 --out /tmp/t.json --db /tmp/t.sqlite   # a small trial
  python3 tools/fetch_tcgdex.py --db /opt/egs-raw/tcgdex.sqlite --out /opt/egs-raw/tcgdex.json        # everything, resumable
Needs only the Python standard library.
"""
import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://api.tcgdex.net/v2"
UA = "RadarKartu-fetch (price board; reads only, one request per second or slower)"
# the English and Japanese sets that matter for the Indonesian board: the MEGA series, the 30th Celebration and the Scarlet & Violet series
DEFAULT_SETS = {
    "en": ["me01", "me02", "me02.5", "me03", "me04", "me05", "30th", "sv01", "sv02", "sv03", "sv03.5", "sv04", "sv04.5", "sv05", "sv06", "sv06.5",
           "sv07", "sv08", "sv08.5", "sv09", "sv10", "sv10.5b", "sv10.5w"],
    "ja": ["M1L", "M1S", "M2", "M2a", "M3", "M4", "M5", "M6", "SV1S", "SV1V", "SV1a", "SV2D", "SV2P", "SV2a", "SV3", "SV3a", "SV4K", "SV4M", "SV4a",
           "SV5K", "SV5M", "SV5a", "SV6", "SV6a", "SV7", "SV7a", "SV8", "SV8a", "SV9", "SV9a", "SV10", "SV11B", "SV11W"]}
SCHEMA = """
CREATE TABLE IF NOT EXISTS setinfo(lang TEXT, id TEXT, name TEXT, total INTEGER, official INTEGER, cards TEXT, fetched_at TEXT, PRIMARY KEY(lang, id));
CREATE TABLE IF NOT EXISTS card(lang TEXT, id TEXT, set_id TEXT, local_id TEXT, name TEXT, rarity TEXT, illustrator TEXT, dex TEXT, hp INTEGER,
  third TEXT, pricing TEXT, fetched_at TEXT, status TEXT, PRIMARY KEY(lang, id));
"""


class Budget(Exception):
    """The request budget of this run is spent; what was fetched is saved."""


class Stop(Exception):
    """The source answers with something that is not data (a block or repeated failures): stop, keep what was learned."""


def http_get(url, timeout=30):
    """(status, parsed JSON or None); never raises. A network failure is status 0."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(3_000_000).decode("utf-8", "replace"))
    except HTTPError as e:
        return e.code, None
    except (URLError, OSError, ValueError):
        return 0, None


class Client:
    def __init__(self, get, delay, max_requests, sleep, log):
        self.get, self.delay, self.max, self.sleep, self.log, self.used, self.failures = get, delay, max_requests, sleep, log, 0, 0

    def fetch(self, path):
        """(status, data) with at most 3 tries on a refusal or a failure; 404 is an answer, 403 is a stop."""
        for wait in (0, 5, 20):
            if self.max is not None and self.used >= self.max:
                raise Budget()
            if wait:
                self.sleep(wait)
            self.used += 1
            status, data = self.get(f"{BASE}/{path}")
            self.sleep(self.delay)
            if status == 200 and data is not None:
                self.failures = 0
                return status, data
            if status == 404:
                return status, None
            if status in (401, 403, 451):
                raise Stop(f"{path}: status {status}: ditolak, berhenti (tidak dilewati)")
            self.log(f"  ! {path} -> status {status}")
        self.failures += 1
        if self.failures >= 3:
            raise Stop("tiga kartu berturut-turut gagal: berhenti, dilanjutkan di putaran berikutnya")
        return 0, None


def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.executescript(SCHEMA)
    return db


def parse_sets(spec, langs):
    """{lang: [set ids]} from 'ja:M2,en:me02' (or the defaults of the languages given)."""
    if not spec:
        return {lang: DEFAULT_SETS[lang] for lang in langs if lang in DEFAULT_SETS}
    out = {}
    for part in spec.split(","):
        lang, _, sid = part.strip().partition(":")
        if lang and sid:
            out.setdefault(lang, []).append(sid)
    return out


def crawl_set(client, db, lang, sid, now, price_days, log):
    status, data = client.fetch(f"{lang}/sets/{sid}")
    if status == 404:
        log(f"  {lang}:{sid}: tidak ada di TCGdex")
        return
    if not data:
        return
    cards = data.get("cards") or []
    count = data.get("cardCount") or {}
    db.execute("INSERT OR REPLACE INTO setinfo VALUES(?,?,?,?,?,?,?)",
               (lang, sid, data.get("name"), count.get("total"), count.get("official"), json.dumps([c["id"] for c in cards]), now.isoformat()))
    stale = (now - timedelta(days=price_days)).isoformat()
    todo = []
    for c in cards:
        row = db.execute("SELECT fetched_at, status FROM card WHERE lang=? AND id=?", (lang, c["id"])).fetchone()
        if row is None or row[1] == "failed" or (row[1] == "ok" and row[0] < stale):    # a card the source said does not exist ("missing") is not asked for again
            todo.append(c["id"])
    log(f"  {lang}:{sid} {data.get('name')}: {len(cards)} kartu, {len(todo)} perlu diambil")
    for cid in todo:
        status, card = client.fetch(f"{lang}/cards/{cid}")
        if status == 404 or card is None:
            db.execute("INSERT OR REPLACE INTO card(lang, id, set_id, status, fetched_at) VALUES(?,?,?,?,?)", (lang, cid, sid, "missing" if status == 404 else "failed", now.isoformat()))
        else:
            pricing = card.get("pricing") or {}
            db.execute("INSERT OR REPLACE INTO card VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (lang, cid, sid, card.get("localId"), card.get("name"), card.get("rarity"), card.get("illustrator"), json.dumps(card.get("dexId")),
                        card.get("hp"), json.dumps(card.get("thirdParty")), json.dumps(pricing), now.isoformat(), "ok"))
        db.commit()


def export(db, now):
    sets, cards = {}, []
    for lang, sid, name, total, official in db.execute("SELECT lang, id, name, total, official FROM setinfo ORDER BY lang, id"):
        sets.setdefault(lang, []).append({"id": sid, "name": name, "total": total, "official": official})
    for lang, cid, set_id, local, name, rarity, ill, dex, hp, third, pricing in db.execute(
            "SELECT lang, id, set_id, local_id, name, rarity, illustrator, dex, hp, third, pricing FROM card WHERE status='ok' ORDER BY lang, set_id, id"):
        p, t = json.loads(pricing or "{}") or {}, json.loads(third or "null") or {}
        cm, tp = p.get("cardmarket") or {}, p.get("tcgplayer") or {}
        row = {"lang": lang, "id": cid, "set": set_id, "local": local, "name": name, "rarity": rarity, "illustrator": ill, "dex": json.loads(dex or "null"), "hp": hp,
               "cardmarket_id": t.get("cardmarket"), "tcgplayer_id": t.get("tcgplayer")}
        if cm:
            row["cardmarket"] = {k: cm.get(k) for k in ("unit", "updated", "low", "avg", "trend", "avg1", "avg7", "avg30") if cm.get(k) is not None}
        if tp:
            row["tcgplayer"] = {k: v for k, v in tp.items() if v is not None}
        cards.append(row)
    return {"schema_version": 1, "source": "api.tcgdex.net", "generated_at": now.isoformat(), "sets": sets, "cards": cards}


def run(args, get=http_get, now=None, sleep=time.sleep, log=print):
    now = now or datetime.now(timezone.utc)
    wanted = parse_sets(args.sets, [x.strip() for x in args.langs.split(",") if x.strip()])
    lock = open(args.db + ".lock", "a") if os.path.isdir(os.path.dirname(os.path.abspath(args.db))) else None
    if lock is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"Sudah ada pengambilan TCGdex yang berjalan untuk {args.db}. Tunggu selesai.")
            lock.close()
            return None
    db = open_db(args.db)
    client = Client(get, args.delay, args.max_requests, sleep, log)
    try:
        try:
            for lang, ids in wanted.items():
                for sid in ids:
                    crawl_set(client, db, lang, sid, now, args.price_days, log)
        except Budget:
            log(f"anggaran {args.max_requests} permintaan habis; jalankan lagi untuk melanjutkan")
        except Stop as e:
            log(f"BERHENTI: {e}")
        data = export(db, now)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(args.out + ".tmp", args.out)
        log(f"ditulis: {args.out} ({sum(len(v) for v in data['sets'].values())} set, {len(data['cards'])} kartu, {client.used} permintaan)")
        return data
    finally:
        db.commit()
        db.close()
        if lock is not None:
            lock.close()


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.path.expanduser("~/.egs-raw/tcgdex.sqlite"))
    ap.add_argument("--out", default="tcgdex.json")
    ap.add_argument("--langs", default="en,ja", help="bahasa yang diambil bila --sets tidak diberikan")
    ap.add_argument("--sets", help="set tertentu: bahasa:kode dipisah koma, mis. ja:M2,en:me02")
    ap.add_argument("--delay", type=float, default=0.5, help="detik antar permintaan")
    ap.add_argument("--max-requests", type=int, help="batas permintaan per putaran; lanjut di putaran berikutnya")
    ap.add_argument("--price-days", type=float, default=7, help="harga sebuah kartu diambil ulang setelah sekian hari")
    ap.add_argument("--list", action="store_true", help="tampilkan set bawaan lalu selesai")
    args = ap.parse_args(argv)
    if args.list:
        for lang, ids in DEFAULT_SETS.items():
            print(f"{lang}: {' '.join(ids)}")
        return 0
    return 0 if run(args) is not None else 1


if __name__ == "__main__":
    sys.exit(main())
