#!/usr/bin/env python3
"""Identity check of one marketplace listing as an EGS-graded Pokemon slab. Pure functions, no network.

verify(listing, catalog, photo=None) answers one question: is this listing, with the evidence we have,
one specific Indonesian card in one specific EGS grade? The answer is a verdict:

  accepted   every piece of evidence is present and consistent -> may enter the watchlist and the prices
  ambiguous  something is missing or in doubt -> kept in a review queue with the reasons and the photo,
             never counted in any price
  rejected   clearly out of scope (not Pokemon, not a slab, another grading company, foreign language)

Nothing is guessed: a missing card number, an unproven language or a rarity that disagrees with the
official catalog keeps a listing ambiguous. The evidence is the title, the description and, when a
vision reader ran, the photo (company, grade, certificate number, card number, rarity and language read
from the label and the card). A photo may confirm or contradict the text; it never overrides a conflict.

Try one title:  python3 tools/egs_identity.py "Pikachu ex SAR 234/193 MA3 ID EGS 10" --catalog catalog.json
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_site  # noqa: E402
from classify import NON_POKEMON as BASE_NON_POKEMON, NUMBER, PRODUCT, RARITY, SET_CODE, variant_of  # noqa: E402

NON_POKEMON = re.compile(BASE_NON_POKEMON.pattern + r"|optcg|\bop\d{2}\s?-\s?\d{3}|super saiyan|saiyans|dbscg|\bbt\d{1,2}-\d{3}|gundam|lorcana|flesh and blood|"
                         r"\bst\d{2}-\d{3}|yugioh|weiss|vanguard|one piece", re.I)
POKEMON_WORD = re.compile(r"pok[e\u00e9]mon|\bptcg\b|\btcg\b", re.I)

MIN_PRICE = 50_000  # a slab for less than this is a typo, a bundle fee or a different item
GRADES = ("10", "9.5", "9", "8.5", "8", "7.5", "7")
COMPANIES = {
    "EGS": re.compile(r"\begs(?![a-z])|\be\.g\.s\b", re.I),   # "EGS10" and "EGS9.5" too
    "PSA": re.compile(r"\bpsa\b", re.I),
    "BGS": re.compile(r"\bbgs\b|\bbeckett\b", re.I),
    "CGC": re.compile(r"\bcgc\b", re.I),
    "ARS": re.compile(r"\bars\b", re.I),
    "PCG": re.compile(r"\bpcg\b", re.I),
    "TAG": re.compile(r"\btag grading\b", re.I),
}
SLAB_WORD = re.compile(r"\b(slab|graded|grading|gem ?mint)\b", re.I)
GRADE_AFTER = re.compile(
    r"\begs(?![a-z])[\s:#_\-]*(?:grading|grade|gem\s*mint|gm|mint|pristine|slab)?[\s:#_\-]*(10|9[.,]5|9|8[.,]5|8|7[.,]5|7)(?![\d/.,]?\d)", re.I)
GRADE_BEFORE = re.compile(r"\b(10|9[.,]5|9|8[.,]5|8)\s*(?:gem\s*mint\s*)?egs\b", re.I)
CERT = re.compile(
    r"(?:cert(?:ificate)?|sertifikat|serial(?:\s*number)?|\bs/?n\b|no\.?\s*seri|no\.?\s*cert)\W{0,4}(?:egs\W{0,3})?(?:no\.?|nomor|number|id)?\W{0,4}([A-Z]{0,3}\d{6,10})", re.I)
FOREIGN = re.compile(r"japan\w*|jepang|\bjpn?\b|chinese|china|korea\w*|thai\w*|english|\beng\b|\bsimplified\b", re.I)
ID_MARK = re.compile(r"\[id\]|\(id\)|\bid\b|\bind\b|\bindo\b|indonesia\w*|bahasa indonesia", re.I)
ID_ONLY_SET = re.compile(r"\bma\d{1,2}\b", re.I)  # Indonesian Mega sets; the Japanese ones are m1l, m2a ...
BUNDLE = re.compile(
    r"\bpaket\b|\bbundle\b|\blot\b|\bborong\w*|\bset of\b|\bisi\s*(?:[2-9]|\d{2,})\b|\b(?:[2-9]|\d{2,})\s*(?:pcs|pc|kartu|cards?|slabs?)\b|"
    r"\bcombo\b|\bbulk\b|\brandom\b|\bacak\b|\bpilih(?:an)?\b|\bopsi\b", re.I)
HOLO = re.compile(r"\b(holo\w*|mirror|reverse|foil)\b", re.I)
SPECIAL = re.compile(r"\b(AA|alt(?:ernate)? art|manga|full art|\bFA\b)\b", re.I)


def tokens(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()


def norm_grade(g):
    return g.replace(",", ".")


SLUG_FILLER = {"original", "ready", "stock", "murah", "slab", "grading", "graded", "pokemon", "kartu", "indonesia", "indonesian", "clear",
               "bagus", "card", "cards", "promo", "limited", "sale", "versi", "gradeing", "celar"}


def title_matches_slug(title, slug):
    """(share, missing): how much of the page address the title still says, and the address words the title lacks. Sellers sometimes
    re-use an old listing for another card: the address keeps the first title. (1.0, []) when there is nothing to compare."""
    parts = (slug or "").split("-")
    while parts and re.fullmatch(r"\d{10,}|[0-9a-f]{3,6}", parts[-1]) and (len(parts) > 2):
        parts.pop()
    words = [w for w in parts if w]
    if len(words) < 3:
        return 1.0, []
    squashed = re.sub(r"(\d)[.,](\d)", r"\1\2", title or "")
    have = set(tokens(squashed))
    missing = [w for w in words if w not in have and len(w) >= 4 and not w.isdigit()]
    return sum(1 for w in words if w in have) / len(words), missing


def reason(code, text):
    return {"code": code, "text": text}


# ---- the catalog ------------------------------------------------------------------

class Catalog:
    """The official Indonesian catalog (catalog.json from tools/fetch_catalog.py), indexed for identity checks."""

    def __init__(self, data):
        self.cards = defaultdict(list)    # (set, number key) -> official entries (regular first)
        self.sets = {}                    # set code -> {"den": main printed total, "name": product}
        main_den = defaultdict(Counter)
        for c in (data or {}).get("cards", []):
            if not c.get("number") or c.get("num") is None:
                continue
            self.cards[build_site.card_key(c["set"], c["number"])[:2]].append(c)
            main_den[c["set"].upper()][c["den"]] += 1
        for s in (data or {}).get("sets", []):
            code = s["code"].upper()
            if main_den[code]:
                self.sets[code] = {"den": main_den[code].most_common(1)[0][0], "name": s.get("name")}
        by_den = defaultdict(list)
        for code, s in self.sets.items():
            by_den[s["den"]].append(code)
        self.by_den = by_den
        self.name_index = defaultdict(list)  # first word of an official name -> entries (for suggestions)
        self.by_num = defaultdict(list)      # (set, number without total) -> regular entries
        self.by_set = defaultdict(list)      # set -> one regular entry per number
        self.set_names = {}                  # normalised official set name -> set code (only names of two words or more)
        for s in (data or {}).get("sets", []):
            words = tokens(s.get("name") or "")
            if len(words) >= 2:
                self.set_names[" ".join(words)] = s["code"].upper()
        for entries in self.cards.values():
            e = entries[0]
            self.by_num[(e["set"].upper(), e["num"])].append(e)
            self.by_set[e["set"].upper()].append(e)
            words = tokens(self.base_name(e["name"]))
            if words:
                self.name_index[words[0]].append(e)

    @staticmethod
    def base_name(name):
        """Official name without the owner in brackets: 'Pinsir <Ethan>' -> 'Pinsir'."""
        return re.sub(r"\s*[<(][^>)]*[>)]", "", name or "").strip()

    def resolve_set(self, set_code, number):
        """(set code, note): the set from the title's code when the catalog knows it, else from the printed total if one set has it."""
        den = int(number.split("/")[1])
        if set_code:
            if set_code.upper() in self.sets:
                return set_code.upper(), None
            return None, f"set {set_code} tidak ada di katalog resmi yang dimuat"   # a written set code is never overruled by the total
        owners = self.by_den.get(den, [])
        if len(owners) == 1:
            return owners[0], "set dari angka total"
        return None, "set tidak ditulis dan angka total /" + str(den) + " cocok dengan " + (f"{len(owners)} set" if owners else "tidak ada set di katalog")

    def knows_name_word(self, text):
        """Is a word of the text the first word of an official card name (Pikachu, Mega, Dreepy ...)? A weak Pokemon signal."""
        return any(w in self.name_index for w in tokens(text))

    def set_from_text(self, text):
        """The set whose official name is written in the text (a name of at least two words, exactly one set)."""
        low = " " + " ".join(tokens(text)) + " "
        hits = {name: code for name, code in self.set_names.items() if f" {name} " in low}
        # "Evolusi Mega Impian ex" also contains "Evolusi Mega": the longest name written wins
        hits = {n: c for n, c in hits.items() if not any(n != m and f" {n} " in f" {m} " for m in hits)}
        return next(iter(hits.values())) if len(set(hits.values())) == 1 else None

    def find(self, set_code, rarity, words):
        """Regular cards of a set with this rarity whose whole official name (ex included) is among the words."""
        out = []
        for e in self.by_set.get(set_code.upper(), []):
            name = tokens(self.base_name(e["name"]))
            if (e.get("rarity") or "") == rarity and name and all(w in words for w in name):
                out.append(e)
        return out

    def lookup(self, set_code, number):
        entries = self.cards.get(build_site.card_key(set_code, number)[:2], [])
        return entries[0] if entries else None

    def suggest(self, title, set_code=None, limit=4):
        """Official cards whose name appears in the title: only a help for the reviewer, never an identity."""
        words = set(tokens(title))
        out = []
        for first in words:
            for e in self.name_index.get(first, []):
                name = tokens(self.base_name(e["name"]))
                if name and all(w in words or w == "ex" for w in name) and (not set_code or e["set"].upper() == set_code.upper()):
                    out.append(e)
        out.sort(key=lambda e: (e["set"], e["num"]))
        return [{"set_code": e["set"], "card_number": e["number"], "name": e["name"], "rarity": e.get("rarity"), "image": e.get("image")}
                for e in out[:limit]]


# ---- text evidence -----------------------------------------------------------------

NEGATED = re.compile(r"\b(?:not|bukan|non|no|tanpa|bkn)\s+(?:slab\s+)?(egs|psa|bgs|cgc|tag|ars|pcg|beckett)\b", re.I)
HASH_NO = re.compile(r"#\s?(\d{1,3})\b")


def text_evidence(title, description=""):
    """Everything the words say, split by where it was written (title weighs more than description)."""
    t, d = title or "", (description or "")[:600]
    both = f"{t} {d}"
    denied = {"BGS" if w.lower() == "beckett" else w.upper() for w in NEGATED.findall(both)}   # "clear slab not TAG" says it is not a TAG slab
    companies = sorted(c for c, rx in COMPANIES.items() if rx.search(both) and c not in denied)
    grades_title = sorted({norm_grade(m.group(1)) for rx in (GRADE_AFTER, GRADE_BEFORE) for m in rx.finditer(t)}, key=float)
    grades_desc = sorted({norm_grade(m.group(1)) for rx in (GRADE_AFTER, GRADE_BEFORE) for m in rx.finditer(d)}, key=float)
    numbers = []
    for m in NUMBER.finditer(both):
        num, den = int(m.group(1)), int(m.group(2))
        key = f"{num}/{den}"
        if den > 10 and key not in numbers:   # "10/10" is a score, not a card number
            numbers.append(key)
    hash_no = HASH_NO.search(t)
    cert = CERT.search(both)
    sc = SET_CODE.search(t) or SET_CODE.search(d)
    rarities = sorted(set(RARITY.findall(t.upper())))
    return {
        "companies": companies, "slab_word": bool(SLAB_WORD.search(both)), "hash_no": int(hash_no.group(1)) if hash_no else None,
        "grades_title": grades_title, "grades_desc": grades_desc, "numbers": numbers,
        "cert": cert.group(1).upper() if cert else None,
        "set": re.sub(r"^([A-Z]+)0+(?=\d)", r"\1", sc.group(1).upper()) if sc else None,
        "rarities": rarities, "variant": variant_of(both), "holo": bool(HOLO.search(t)), "special": bool(SPECIAL.search(t)),
        "foreign": bool(FOREIGN.search(both)), "id_mark": bool(ID_MARK.search(both)), "ma_set": bool(ID_ONLY_SET.search(t)),
        "bundle": bool(BUNDLE.search(t)), "product": bool(PRODUCT.search(t)) and not NUMBER.search(t),
        "non_pokemon": bool(NON_POKEMON.search(both)), "pokemon_word": bool(POKEMON_WORD.search(both)),
    }


# ---- the verdict ---------------------------------------------------------------------

def verify(listing, catalog, photo=None, tracked=None):
    """Verdict for one listing: {"verdict", "reasons", "identity", "evidence", "suggestions"}.

    listing: title, description, ask (rupiah), stock. catalog: a Catalog. photo: what a vision reader saw
    (dict with is_slab, company, grade, cert, number, rarity, language, name) or None. tracked: the watchlist card
    this listing was already known as (the six cards followed by the bot); it is trusted, but disagreement is still reported.
    """
    title, description = listing.get("title") or "", listing.get("description") or ""
    ev = text_evidence(title, description)
    reasons, identity = [], {"company": None, "grade": None, "cert": ev["cert"], "language": None, "language_by": None,
                             "name": None, "set_code": None, "card_number": None, "rarity": None, "variant": ev["variant"]}
    out = {"verdict": None, "reasons": reasons, "identity": identity, "evidence": ev, "suggestions": []}

    def done(verdict):
        out["verdict"] = verdict
        return out

    # 1. clearly out of scope
    if ev["non_pokemon"]:
        reasons.append(reason("not_pokemon", "Bukan kartu Pokémon"))
        return done("rejected")
    if ev["product"]:
        reasons.append(reason("product", "Produk segel atau aksesori, bukan satu kartu"))
        return done("rejected")
    photo = photo or {}
    if not tracked and not photo.get("is_slab") and not (ev["pokemon_word"] or ev["set"] or ev["rarities"] or ev["numbers"] or ev["hash_no"] is not None
                                                          or catalog.knows_name_word(title)):
        reasons.append(reason("no_pokemon_signal", "Tidak ada tanda kartu Pokémon (kata Pokémon, kode set, rarity, nomor kartu, atau nama kartu di katalog)"))
        return done("rejected")
    others = [c for c in ev["companies"] if c != "EGS"]
    photo_company = (photo.get("company") or "").upper() or None
    if photo_company and photo_company != "EGS" and "EGS" not in ev["companies"]:
        reasons.append(reason("other_grader", f"Foto menunjukkan slab {photo_company}, bukan EGS"))
        return done("rejected")
    if "EGS" not in ev["companies"] and photo_company != "EGS":
        if others:
            reasons.append(reason("other_grader", "Perusahaan grading lain (" + ", ".join(others) + "), bukan EGS"))
            return done("rejected")
        if not ev["slab_word"] and not photo.get("is_slab"):
            reasons.append(reason("not_slab", "Tidak ada tanda slab atau grading EGS: kartu RAW"))
            return done("rejected")
    if ev["foreign"] and not ev["id_mark"]:
        reasons.append(reason("foreign_language", "Judul atau deskripsi menyebut bahasa lain (Jepang, Inggris, Mandarin ...)"))
        return done("rejected")
    if (photo.get("language") or "").upper() in ("JP", "EN", "OTHER"):
        reasons.append(reason("foreign_language", "Foto menunjukkan kartu berbahasa " + photo["language"].upper()))
        return done("rejected")

    # 2. grading company
    if "EGS" in ev["companies"]:
        identity["company"] = "EGS"
        if photo_company and photo_company != "EGS":
            reasons.append(reason("photo_conflict", f"Foto menunjukkan slab {photo_company}, teks menulis EGS"))
        if others:
            reasons.append(reason("company_conflict", "Menyebut EGS dan juga " + ", ".join(others) + ": perusahaan grading tidak jelas"))
    elif photo_company == "EGS":
        identity["company"] = "EGS"
    else:
        reasons.append(reason("no_egs", "Disebut slab tetapi perusahaan grading tidak tertulis"))

    # 3. grade
    grades = ev["grades_title"] or ev["grades_desc"]
    if len(ev["grades_title"]) > 1 or (ev["grades_title"] and ev["grades_desc"] and set(ev["grades_desc"]) != set(ev["grades_title"])):
        reasons.append(reason("grade_conflict", "Grade berbeda antara judul dan deskripsi: " + " / ".join(sorted(set(ev["grades_title"]) | set(ev["grades_desc"]), key=float))))
    elif len(grades) > 1:
        reasons.append(reason("multi_grade", "Beberapa grade disebut: " + " / ".join(grades)))
    elif not grades:
        pg = norm_grade(str(photo.get("grade"))) if photo.get("grade") else None
        if pg in GRADES:
            identity["grade"] = pg
        else:
            reasons.append(reason("grade_missing", "Grade EGS tidak terbaca dari judul atau deskripsi"))
    else:
        identity["grade"] = grades[0]
    pg = norm_grade(str(photo.get("grade"))) if photo.get("grade") else None
    if pg and identity["grade"] and pg != identity["grade"]:
        reasons.append(reason("photo_conflict", f"Foto menunjukkan grade {pg}, teks menulis {identity['grade']}"))
        identity["grade"] = None
    if identity["grade"] and identity["grade"] not in GRADES:
        reasons.append(reason("grade_missing", f"Grade {identity['grade']} di luar daftar yang dipantau"))
        identity["grade"] = None
    if photo.get("cert") and not identity["cert"]:
        identity["cert"] = str(photo["cert"]).upper()
    elif photo.get("cert") and identity["cert"] and str(photo["cert"]).upper() != identity["cert"]:
        reasons.append(reason("photo_conflict", "Nomor sertifikat di foto berbeda dari teks"))

    # 4. language: needs proof, absence of a foreign word is not proof
    if ev["foreign"] and ev["id_mark"]:
        reasons.append(reason("language_conflict", "Menyebut Indonesia dan juga bahasa lain: bahasa kartu tidak jelas"))
    elif ev["id_mark"]:
        identity["language"], identity["language_by"] = "ID", "teks"
    elif ev["ma_set"]:
        identity["language"], identity["language_by"] = "ID", "kode set MA (hanya dipakai kartu Indonesia)"
    elif (photo.get("language") or "").upper() == "ID":
        identity["language"], identity["language_by"] = "ID", "foto"
    else:
        reasons.append(reason("language_unproven", "Bahasa Indonesia belum terbukti (tidak ada kata ID/Indonesia, kode set MA, atau foto)"))

    if listing.get("slug") and not tracked:
        share, missing_words = title_matches_slug(title, listing["slug"])
        name_like = [w for w in missing_words if len(w) >= 5 and w.isalpha() and w not in SLUG_FILLER]
        slug_foreign = any(FOREIGN.fullmatch(w) for w in listing["slug"].split("-")) and not FOREIGN.search(title)
        if share < 0.6 or len(missing_words) >= 2 or name_like or slug_foreign:
            reasons.append(reason("title_changed", "Judul sekarang berbeda dari alamat listing (kemungkinan listing dipakai ulang untuk kartu lain): perlu cek foto"))

    # 5. one card
    if ev["bundle"]:
        reasons.append(reason("bundle", "Judul tampak berisi lebih dari satu kartu (paket, pilihan, atau jumlah)"))
    numbers = list(ev["numbers"])
    pnum = None
    if photo.get("number"):
        m = NUMBER.search(str(photo["number"]))
        pnum = f"{int(m.group(1))}/{m.group(2)}" if m else None
    if len(numbers) > 1:
        reasons.append(reason("number_multiple", "Lebih dari satu nomor kartu: " + ", ".join(numbers)))
    elif pnum and numbers and pnum != numbers[0]:
        reasons.append(reason("photo_conflict", f"Foto menunjukkan nomor {pnum}, teks menulis {numbers[0]}"))
    number = numbers[0] if len(numbers) == 1 else (pnum if not numbers else None)
    by = "nomor di judul" if numbers and number else ("nomor di foto" if number else None)
    set_hint = ev["set"] if ev["set"] and ev["set"].upper() in catalog.sets else None
    if ev["set"] and not set_hint:
        set_hint = None
    named_set = catalog.set_from_text(f"{title} {description[:200]}") if not ev["set"] else None
    known_set = set_hint or named_set
    blocked = any(r["code"] in ("photo_conflict", "number_multiple") for r in reasons)
    if number is None and not blocked and known_set:
        words = set(tokens(title))
        if ev["hash_no"] is not None:          # "#145" and a set that is written: the number within the set
            entries = catalog.by_num.get((known_set, ev["hash_no"]), [])
            if len(entries) == 1:
                number, by = entries[0]["number"], "nomor #" + str(ev["hash_no"]) + " dan set"
            else:
                reasons.append(reason("card_not_in_catalog", f"Nomor #{ev['hash_no']} tidak ada di {known_set} pada katalog resmi"))
        if number is None and ev["hash_no"] is None and len(ev["rarities"]) == 1:   # name + set + rarity, when exactly one official card fits
            fits = catalog.find(known_set, ev["rarities"][0], words)
            if len(fits) == 1:
                number, by = fits[0]["number"], "nama, set dan rarity (satu-satunya di katalog)"
            elif len(fits) > 1:
                out["suggestions"] = [{"set_code": e["set"], "card_number": e["number"], "name": e["name"], "rarity": e.get("rarity"), "image": e.get("image")}
                                      for e in fits[:4]]
                reasons.append(reason("number_missing", f"Nomor tidak ditulis; {len(fits)} kartu di {known_set} cocok dengan nama dan rarity"))
    if number is None and not blocked and not any(r["code"] in ("number_missing", "card_not_in_catalog") for r in reasons):
        reasons.append(reason("number_missing", "Nomor kartu tidak ditulis dan belum terbaca dari foto"))
    if number is None and not out["suggestions"]:
        out["suggestions"] = catalog.suggest(title, ev["set"] or named_set)
    identity["identified_by"] = by

    # 6. the card in the official catalog: name and rarity must agree with the number
    if number:
        set_code, note = catalog.resolve_set(ev["set"] or named_set, number)
        if set_code is None:
            reasons.append(reason("set_unknown", note))
        else:
            official = catalog.lookup(set_code, number)
            if official is None:
                reasons.append(reason("card_not_in_catalog", f"{set_code} {number} tidak ada di katalog resmi"))
            else:
                name_words = tokens(Catalog.base_name(official["name"]))
                title_words = set(tokens(title) + tokens(description[:200]))
                missing = [w for w in name_words if w not in title_words and w != "ex"]
                rarity_official = official.get("rarity")
                if missing:
                    reasons.append(reason("name_mismatch", f"Nama di katalog ({official['name']}) tidak cocok dengan judul"))
                if len(ev["rarities"]) > 1:
                    reasons.append(reason("rarity_multiple", "Beberapa rarity disebut: " + ", ".join(ev["rarities"])))
                elif ev["rarities"] and ev["rarities"][0] != (rarity_official or ""):
                    reasons.append(reason("rarity_mismatch", f"Rarity di judul ({ev['rarities'][0]}) berbeda dari katalog ({rarity_official or 'tanpa tanda'})"))
                elif photo.get("rarity") and str(photo["rarity"]).upper() not in ((rarity_official or "").upper(), ""):
                    reasons.append(reason("photo_conflict", f"Foto menunjukkan rarity {photo['rarity']}, katalog {rarity_official or 'tanpa tanda'}"))
                if ev["holo"] and not ev["variant"] and len(catalog.cards.get(build_site.card_key(set_code, number)[:2], [])) > 1:
                    reasons.append(reason("variant_unclear", "Nomor ini punya versi foil dan judul hanya menulis holo: versi mana belum jelas"))
                if not missing:
                    identity.update(name=official["name"], set_code=set_code, card_number=official["number"], rarity=rarity_official)
                else:
                    identity.update(set_code=set_code, card_number=official["number"])
                if note:
                    identity["set_by"] = note

    # 7. price and availability
    ask = listing.get("ask")
    if ask is not None and ask < MIN_PRICE:
        reasons.append(reason("price_implausible", f"Harga Rp{ask:,}".replace(",", ".") + f" terlalu rendah untuk sebuah slab (batas Rp{MIN_PRICE:,}".replace(",", ".") + ")"))

    # a card the bot already tracks keeps its place; disagreement is still shown
    hard = [r for r in reasons if r["code"] not in ("price_implausible",)]
    if tracked and not any(r["code"] in ("photo_conflict", "company_conflict") for r in reasons):
        identity.update(name=tracked["name"], set_code=tracked["set_code"], card_number=tracked["card_number"], rarity=tracked["rarity"] or None,
                        grade=identity["grade"] or tracked["grade"], company="EGS", language=identity["language"] or "ID",
                        language_by=identity["language_by"] or "dipantau bot")
        out["tracked"] = True
        out["disagreements"] = [r for r in hard]
        return done("accepted")

    complete = all(identity[k] for k in ("company", "grade", "language", "name", "set_code", "card_number"))
    if not reasons and complete:
        return done("accepted")
    if not reasons:
        reasons.append(reason("incomplete", "Identitas belum lengkap"))
    return done("ambiguous")


def card_key(identity):
    """Cards that are the same card: set, number, rarity and foil pattern. The grade is a separate level."""
    return "|".join([identity["set_code"], build_site.card_key(identity["set_code"], identity["card_number"])[1], identity["rarity"] or "-",
                     identity.get("variant") or ""])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("title")
    ap.add_argument("--description", default="")
    ap.add_argument("--price", type=int)
    ap.add_argument("--catalog", default="catalog.json")
    a = ap.parse_args(argv)
    with open(a.catalog, encoding="utf-8") as fh:
        catalog = Catalog(json.load(fh))
    r = verify({"title": a.title, "description": a.description, "ask": a.price}, catalog)
    print(json.dumps({k: r[k] for k in ("verdict", "reasons", "identity", "suggestions")}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
