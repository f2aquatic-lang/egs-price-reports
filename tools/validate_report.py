#!/usr/bin/env python3
"""Validate latest.json (schema_version 1). Read-only: never writes any file.

Usage: python3 tools/validate_report.py [path/to/latest.json]
Exit code 1 if any ERROR is found; warnings and info lines do not fail the run.
"""
import json
import sys
from datetime import datetime, timedelta

WIB = timedelta(hours=7)
TOP_KEYS = ("schema_version", "generated_at", "price_type", "source",
            "coverage", "discovery", "listings")
LISTING_KEYS = ("name", "set_code", "card_number", "rarity", "grade", "url",
                "latest", "previous_day", "change_from_previous_day_idr",
                "daily_latest")
DISCOVERY_KEYS = ("generated_at", "pages", "links_examined", "candidates_latest",
                  "tracked_latest", "pending_latest", "candidates")


def ts(value):
    return datetime.fromisoformat(value)


def validate(d):
    errors, warnings, info = [], [], []
    err, warn = errors.append, warnings.append

    missing = [k for k in TOP_KEYS if k not in d]
    if missing:
        err("missing top-level keys: " + ", ".join(missing))
        return errors, warnings, info
    if d["schema_version"] != 1:
        err(f"schema_version is {d['schema_version']!r}, expected 1")
    if d["price_type"] != "OBSERVED_ASKING_ONLY":
        err(f"price_type is {d['price_type']!r}, expected OBSERVED_ASKING_ONLY")

    gen = ts(d["generated_at"])
    listings = d["listings"]
    urls = [l.get("url") for l in listings]
    if len(urls) != len(set(urls)):
        err("duplicate listing urls")

    observed = []
    for l in listings:
        label = l.get("name", "?")
        gone = [k for k in LISTING_KEYS if k not in l]
        if gone:
            err(f"{label}: missing keys {', '.join(gone)}")
            continue
        if any(c in l["name"] for c in "<>"):
            warn(f"{label}: name contains angle brackets (parse artefact?)")
        latest = l["latest"]
        if not latest:
            warn(f"{label}: no observation")
            continue
        obs = ts(latest["observed_at"])
        observed.append(obs)
        if obs > gen:
            err(f"{label}: observed_at is after generated_at")
        if not isinstance(latest["asking_idr"], int) or latest["asking_idr"] <= 0:
            err(f"{label}: asking_idr must be a positive integer")
        daily = l["daily_latest"]
        if daily:
            dates = [x["date_wib"] for x in daily]
            if dates != sorted(set(dates), reverse=True):
                err(f"{label}: daily_latest not unique/newest-first")
            if daily[0]["observed_at"] != latest["observed_at"]:
                err(f"{label}: daily_latest[0] differs from latest")
            for x in daily:
                if (ts(x["observed_at"]) + WIB).date().isoformat() != x["date_wib"]:
                    err(f"{label}: date_wib {x['date_wib']} does not match observed_at in WIB")
        prev = l["previous_day"]
        if prev:
            expected = latest["asking_idr"] - prev["asking_idr"]
            if l["change_from_previous_day_idr"] != expected:
                err(f"{label}: change_from_previous_day_idr is "
                    f"{l['change_from_previous_day_idr']}, expected {expected}")
            gap = (obs - ts(prev["observed_at"])).total_seconds() / 3600
            info.append(f"{label}: previous_day is {gap:.0f}h before latest")
        if gen - obs > timedelta(hours=8):
            warn(f"{label}: latest observation is older than 8h at generation time")

    cov = d["coverage"]
    if cov.get("watched") != len(listings):
        err(f"coverage.watched={cov.get('watched')} but {len(listings)} listings")
    if observed:
        if cov.get("last_observed_at") != max(observed).isoformat():
            err("coverage.last_observed_at is not the newest latest.observed_at")
        n14 = sum(o >= gen - timedelta(days=14) for o in observed)
        n8 = sum(o >= gen - timedelta(hours=8) for o in observed)
        if cov.get("observed_within_14_days") != n14:
            warn(f"coverage.observed_within_14_days={cov.get('observed_within_14_days')}, recomputed {n14}")
        if cov.get("fresh_within_8_hours") != n8:
            warn(f"coverage.fresh_within_8_hours={cov.get('fresh_within_8_hours')}, recomputed {n8}")
        info.append(f"report lag: generated_at is {(gen - max(observed))} after the newest observation")

    disc = d["discovery"]
    gone = [k for k in DISCOVERY_KEYS if k not in disc]
    if gone:
        err("discovery missing keys: " + ", ".join(gone))
        return errors, warnings, info
    cands = disc["candidates"]
    curls = [c["url"] for c in cands]
    if len(curls) != len(set(curls)):
        err("duplicate candidate urls")
    tracked = [c for c in cands if c["tracked"]]
    if disc["candidates_latest"] != len(cands):
        err(f"discovery.candidates_latest={disc['candidates_latest']} but {len(cands)} candidates")
    if disc["tracked_latest"] != len(tracked):
        err(f"discovery.tracked_latest={disc['tracked_latest']} but {len(tracked)} tracked")
    if disc["pending_latest"] != len(cands) - len(tracked):
        err(f"discovery.pending_latest={disc['pending_latest']} but {len(cands) - len(tracked)} pending")
    if {c["url"] for c in tracked} != set(urls):
        warn("tracked candidates and listings are not the same set of urls")
    for c in cands:
        if "ReferensiGeografis" in c["search_text"]:
            warn("candidate search_text contains scraper text 'ReferensiGeografis': " + c["url"][-40:])
    info.append(f"watchlist {len(tracked)} tracked, {len(cands) - len(tracked)} pending review")
    return errors, warnings, info


def main(argv):
    path = argv[1] if len(argv) > 1 else "latest.json"
    with open(path, encoding="utf-8") as fh:
        errors, warnings, info = validate(json.load(fh))
    for kind, items in (("ERROR", errors), ("WARN", warnings), ("info", info)):
        for line in items:
            print(f"{kind}: {line}")
    print(f"{path}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
