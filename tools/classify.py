#!/usr/bin/env python3
"""Classify a toco listing title: slab, raw card, sealed product or non-Pokemon.

Reference implementation for the RAW dataset (see docs/raw-dataset.md). Keyword
rules on the title only; used by the page builder and meant to be reused by the
collector. Run `python3 tools/classify.py "title"` to try one title.

kind     slab     graded card (EGS, PSA, BGS, CGC ...), grade is read when possible
         raw      single card with no grader mentioned (or explicitly "raw")
         product  booster, box, deck, binder, sleeves ... not a single card
         other    not a Pokemon card
lang     ID | foreign | unknown   (ID needs an explicit marker such as "ID", "Indo")
"""
import json
import re
import sys

NON_POKEMON = re.compile(
    r"one ?piece|\bop\d{2}-\d{3}|\bp-\d{3}\b|digimon|\bex\d-\d{3}|yu-?gi-?oh|"
    r"dragon ?ball|\bmtg\b|magic the gathering|union arena", re.I)
RAW_MARK = re.compile(
    r"\braw\b|ungraded|belum (?:di)?grad\w*|\b(?:non|bukan|no|tanpa)[ -]?(?:slab|grad\w*|psa|egs)\b", re.I)
GRADER = re.compile(r"\b(egs|psa|bgs|cgc|ars|pcg|beckett|slab|graded|grading|gem ?mint)\b", re.I)
PRODUCT = re.compile(
    r"booster|\bbox\b|\bdeck\b|\btin\b|\betb\b|elite trainer|blister|bundle|display|sealed|segel|"
    r"collection box|binder|playmat|sleeves?\b|\bcase\b|protector|toploader|top loader", re.I)
FOREIGN = re.compile(r"japan\w*|jepang|\bjpn?\b|chinese|china|korea\w*|thai\w*|english|\beng\b", re.I)
ID_MARK = re.compile(r"\[id\]|\bid\b|\bindo\b|indonesia\w*", re.I)
NUMBER = re.compile(r"\b(\d{1,3})\s*/\s*(\d{2,3})\b")
HASH_NO = re.compile(r"#\s?(\d{1,3})\b")
SET_CODE = re.compile(r"\b(ma\d{1,2}|sv\d{1,2}[a-z]?|s\d{1,2}[a-z]{0,2}|m\d[a-z]?|ac\d[a-z]*)\b", re.I)
RARITY = re.compile(r"\b(SAR|SR|AR|UR|HR|CSR|SSR|RRR|RR)\b")
CONDITION = re.compile(r"\b(near mint|nm|mint|lp|mp|hp|dmg|damaged)\b", re.I)
GRADE = re.compile(
    r"\b(?:egs|psa|bgs|cgc)\W+(?:(?:grading|gem ?mint|gm|slab|grade|mint)\W+)*(10|9[.,]5|9|8[.,]5|8)\b", re.I)


def classify_title(text):
    t = re.sub(r"^Sisa \d+ ", "", text.strip())
    if NON_POKEMON.search(t):
        kind = "other"
    elif PRODUCT.search(t) and not NUMBER.search(t):
        kind = "product"
    elif RAW_MARK.search(t):
        kind = "raw"
    elif GRADER.search(t):
        kind = "slab"
    else:
        kind = "raw"
    if FOREIGN.search(t):
        lang = "foreign"
    elif ID_MARK.search(t):
        lang = "ID"
    else:
        lang = "unknown"
    grade = None
    if kind == "slab":
        m = GRADE.search(t)
        grade = m.group(1).replace(",", ".") if m else None
    num = NUMBER.search(t)
    hno = HASH_NO.search(t)
    sc = SET_CODE.search(t)
    cond = CONDITION.search(t) if kind == "raw" else None
    return {
        "kind": kind, "lang": lang, "grade": grade,
        "number": f"{int(num.group(1))}/{num.group(2)}" if num else None,
        "card_no": hno.group(1) if hno else None,
        "set": sc.group(1).upper() if sc else None,
        "rarity": sorted(set(RARITY.findall(t.upper()))),
        "condition": cond.group(1).lower() if cond else None,
    }


if __name__ == "__main__":
    for title in sys.argv[1:]:
        print(json.dumps(classify_title(title), ensure_ascii=False))
