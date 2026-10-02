# EGS Price Reports

Private report channel for the EGS price pilot. The VPS publishes `latest.json` with public listing asking prices and discovery candidates. Asking prices are not completed transactions.

Keep this repository private before publishing any report data.

## Tools (read-only)

These scripts only read `latest.json`; they never write it and are not part of the VPS publishing path. Python 3 standard library only.

- `python3 tools/validate_report.py [latest.json]` checks the schema, counters and date fields. Exit code 1 means a real inconsistency; warnings and info lines do not fail.
- `python3 tools/triage_candidates.py [latest.json] [--grades 10,9.5] [--json]` sorts the pending discovery candidates into auto-reject, extra listing of a tracked card, and new cards that need a decision. The rules are keyword heuristics that only order the review queue.
- `python3 tools/build_site.py [latest.json] --out board.html` builds the "Papan Harga Slab EGS" page: every asking price per tracked card and grade, plus the other discovered listings. Add `--standalone` to open the file directly in a browser. Match confidence of a candidate to a card is shown on the page; uncertain matches are displayed but not counted.
