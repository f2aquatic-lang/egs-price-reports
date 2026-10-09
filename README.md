# EGS Price Reports

Private report channel for the EGS price pilot. The VPS publishes `latest.json` with public listing asking prices and discovery candidates. Asking prices are not completed transactions.

Keep this repository private before publishing any report data.

## Tools (read-only)

These scripts only read `latest.json`; they never write it and are not part of the VPS publishing path. Python 3 standard library only.

- `python3 tools/validate_report.py [latest.json]` checks the schema, counters and date fields. Exit code 1 means a real inconsistency; warnings and info lines do not fail.
- `python3 tools/triage_candidates.py [latest.json] [--grades 10,9.5] [--json]` sorts the pending discovery candidates into auto-reject, extra listing of a tracked card, and new cards that need a decision. The rules are keyword heuristics that only order the review queue.
- `python3 tools/build_site.py [latest.json] --out board.html` builds the "Papan Harga Slab EGS" page: every asking price per tracked card and grade, plus the other discovered listings. Add `--standalone` to open the file directly in a browser. Match confidence of a candidate to a card is shown on the page; uncertain matches are displayed but not counted.
- `python3 tools/classify.py "judul listing"` memisahkan slab, kartu RAW, produk segel, dan non-Pokémon dari judul listing.
- `python3 tools/watch_queries.py [latest.json]` mencetak kata kunci pencarian per kartu yang dipantau, untuk dataset EGS dan RAW.
- `python3 tools/validate_raw.py [raw.json]` memeriksa format `raw.json`.
- Folder masuk `/opt/egs-raw/inbox` ([docs/inbox.md](docs/inbox.md)): alat lain (Dots) menjatuhkan berkas listing di sana; `collect_raw.py` dan `egs_watch.py` memeriksa dan memasukkannya seperti listing toco. `tools/intake.py` memegang aturan berkasnya.
- `python3 tools/audit_board.py [--dir .]` mengaudit berkas data dan papan yang dibangun darinya (umur data, validasi, aturan harga dasar, cakupan harga, kewajaran terhadap harga Yuyutei, ketipisan harga slab, acuan JP dan EN); hanya membaca, keluar 1 bila ada yang GAGAL.
- `python3 tools/test_tools.py` menjalankan uji otomatis semua alat di atas.

## Dataset RAW

Dataset kedua untuk kartu bahasa Indonesia yang belum di-slab, supaya papan harga bisa menunjukkan RAW dan slab EGS berdampingan. Format, aturan hitung, dan alur pengumpul ada di [docs/raw-dataset.md](docs/raw-dataset.md). `latest.json` tidak berubah.
- `python3 tools/collect_raw.py --dry-run` (di VPS) mengumpulkan listing RAW lewat pencarian toco dan menulis `raw.json`; `tools/run_raw.sh` menjalankannya dengan kunci API (Claude atau OpenAI; atau `--lang-mode text` tanpa kunci) dan venv-nya, dan `tools/publish_raw.sh` menerbitkannya ke `main` (hanya mau jalan dari branch `main`). Langkah pemasangan ada di [docs/vps-setup.md](docs/vps-setup.md). Situs papan harga dengan kata sandi (Caddy, `tools/setup_site.sh`) dijelaskan di [docs/website.md](docs/website.md).
- `tools/probe_toco*.py` adalah skrip diagnostik yang dipakai untuk mempelajari cara kerja toco.

## Pemantau slab EGS

Memperluas daftar slab EGS dari enam listing ke puluhan tanpa mengubah bot lama atau `latest.json`: penemuan di toco (halaman demi halaman, banyak kata kunci), pengecekan identitas terhadap katalog resmi, bukti per pengamatan, statistik per kartu dan grade, dan ekspor `egs_watch.json`. Kandidat yang meragukan masuk antrean dengan foto dan alasannya, bukan ke harga. Aturan, pemasangan, dan jalan mundur ada di [docs/egs-watch.md](docs/egs-watch.md).
- `python3 tools/egs_watch.py --dry-run` (di VPS) membaca toco dan mencetak laporan tanpa menulis apa pun; `--report` dan `--validate` membaca `egs_watch.json`.
- `python3 tools/egs_identity.py "judul listing"` menunjukkan putusan identitas untuk satu judul.
- `python3 tools/probe_marketplaces.py` menguji apakah Tokopedia dan Shopee bisa dibaca tanpa melewati pembatasan.
