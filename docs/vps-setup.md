# Menjalankan pengumpul RAW di VPS

Pengumpul (`tools/collect_raw.py`) mencari listing RAW di toco untuk setiap kartu yang dipantau, membaca bahasa kartu (teks dulu, foto untuk sisanya) dengan Claude atau OpenAI, lalu menulis `raw.json`. Panduan ini mengikuti VPS Anda sebagaimana hasil pemeriksaan: Ubuntu 24.04, Python 3.12, bot EGS berjalan sebagai pengguna `egsprice` di `/opt/egs-price/` dan mendorong ke GitHub lewat kunci SSH, dengan jadwal systemd.

Pengumpul RAW memakai folder sendiri, `/opt/egs-raw`, dan **tidak menyentuh folder, jadwal, atau berkas bot yang sudah ada**. Langkah 1 sampai 5 tidak mengirim apa pun ke GitHub. Langkah 6 menambah satu jadwal baru dan hanya dikerjakan sesudah Anda setuju.

Cara kerjanya: memanggil pencarian toco langsung (tanpa browser), 45 listing per permintaan, lalu memisahkan kartu RAW Indonesia dari slab, produk segel, dan kartu Jepang atau Inggris. Rincian ada di [raw-dataset.md](raw-dataset.md).

## Langkah 1. Siapkan folder dan ambil kode

```bash
sudo mkdir -p /opt/egs-raw && sudo chown egsprice:egsprice /opt/egs-raw
sudo -u egsprice -H git clone -b claude/quirky-bardeen-bmq87l git@github.com:f2aquatic-lang/egs-price-reports.git /opt/egs-raw/repo
```

`-H` membuat Git memakai kunci SSH milik `egsprice`. Kalau pengumpul sudah ada di `main`, hilangkan `-b claude/quirky-bardeen-bmq87l`. Kalau muncul `Permission denied (publickey)`, tempel pesannya.

## Langkah 2. Coba baca toco (gratis, tanpa kunci, tanpa memasang paket)

```bash
cd /opt/egs-raw/repo
sudo -u egsprice -H python3 tools/collect_raw.py --dry-run --queries-limit 3 --db /opt/egs-raw/raw.sqlite
```

Yang harus terlihat: tiap kata kunci menemukan puluhan sampai ratusan listing, lalu ringkasan seperti `jenis_raw`, `cocok_nomor`, `cocok_nama+set`. Tidak ada panggilan ke Claude dan tidak ada `raw.json` yang ditulis. Kartu berbahasa asing yang terlihat dari teks sudah disingkirkan di tahap ini.

## Langkah 3. Pilih cara membaca bahasa dari foto

Ada tiga pilihan. Pilih satu; yang lain bisa menyusul tanpa mengubah data.

**A. Claude (bawaan)**, perlu saldo API Anthropic:

```bash
sudo -u egsprice -H python3 -m venv /opt/egs-raw/venv
sudo -u egsprice -H /opt/egs-raw/venv/bin/pip install anthropic
sudo install -m 600 -o egsprice -g egsprice /dev/null /opt/egs-raw/env
sudo nano /opt/egs-raw/env
```

Di `nano`, tulis satu baris tanpa tanda kutip dan tanpa kata `export`, lalu simpan (Ctrl+O, Enter, Ctrl+X):

```
ANTHROPIC_API_KEY=sk-ant-...
```

**B. OpenAI**, perlu saldo API OpenAI (langganan ChatGPT Plus bukan saldo API; saldo API diisi terpisah di platform.openai.com):

```bash
sudo -u egsprice -H python3 -m venv /opt/egs-raw/venv
sudo -u egsprice -H /opt/egs-raw/venv/bin/pip install openai
sudo install -m 600 -o egsprice -g egsprice /dev/null /opt/egs-raw/env
sudo nano /opt/egs-raw/env
```

```
OPENAI_API_KEY=sk-...
EGS_LANG_PROVIDER=openai
EGS_LANG_MODEL=<nama model>
```

Nama model bawaan untuk OpenAI (`gpt-4o-mini`) **belum diuji**: daftar model OpenAI berubah dan saya tidak bisa memeriksanya dari sini. Lihat model yang bisa dipakai kunci Anda, lalu pilih satu yang bisa membaca gambar:

```bash
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --provider openai --list-models
```

Kalau modelnya tidak bisa membaca gambar atau tidak ada, pengumpul berhenti dengan pesan yang jelas (bukan diam-diam menghitung salah). Apa pun model yang dipilih, Langkah 4 wajib dijalankan dulu.

**C. Tanpa kunci sama sekali**: tambahkan `EGS_LANG_MODE=text` di `/opt/egs-raw/env` (atau `--lang-mode text` di perintah). Hanya listing yang **tertulis** Indonesia di judul atau deskripsi (`ID`, `Indo`, `Indonesia`, nama set Indonesia seperti "Mega Impian") yang dihitung, dan ditandai `lang_by: "title"`. Listing tanpa keterangan bahasa tidak dihitung, jadi datanya lebih sedikit, tetapi tidak ada kartu Jepang atau Inggris yang ikut terhitung dari teks asing. Ini cara tercepat untuk mulai; ganti ke A atau B nanti dan listing yang tadinya dihitung dari judul dicek ulang dengan fotonya sesudah sehari.

Berkas `/opt/egs-raw/env` hanya bisa dibaca `egsprice`. Jangan kirim kunci ke chat dan jangan masukkan ke repo. Paket dipasang di venv sendiri, jadi lingkungan Python bot tidak berubah.

## Langkah 4. Uji pembaca bahasa pada foto nyata

```bash
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --sample-check "Zekrom N 210/193"
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --sample-check "Mega Dragonite ex 232/193"
```

Langkah ini hanya untuk pilihan A atau B (pilihan C tidak memakai foto). Tiap perintah memeriksa 6 foto dan mencetak baris seperti `foto=ID | teks=ID | jenis=raw | Rp… | judul`. Tidak ada yang disimpan. Yang harus benar: kartu Jepang atau Inggris **tidak boleh** terbaca `foto=ID`. Terbaca `UNCLEAR` tidak masalah, listing itu hanya tidak dihitung. **Tempel keluarannya ke sini** supaya saya bisa menilai akurasinya sebelum lanjut.

## Langkah 5. Putaran nyata kecil

```bash
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --queries-limit 3 --max-checks 20 --out /opt/egs-raw/raw.json
cd /opt/egs-raw/repo && python3 tools/validate_raw.py /opt/egs-raw/raw.json
```

Harus berakhir dengan `0 error(s)`. Hasilnya ditulis di `/opt/egs-raw/raw.json`, di luar repo. Skrip mencetak `pemakaian token`, dari situ biaya per foto bisa dihitung.

## Langkah 6. Jadwalkan (sesudah pengumpul di-merge ke main dan Anda setuju)

Penerbitan memakai **kunci SSH milik bot** yang sudah ada (`/opt/egs-price/.ssh/egs_report`, kunci deploy repo ini); pengguna `egsprice` tidak punya kunci bawaan, jadi tanpa kunci itu GitHub menjawab `Permission denied (publickey)`. Tidak ada kunci baru dan tidak ada yang ditulis ke repo.

Uji dulu bahwa kunci itu bisa membaca repo (hanya baca):

```bash
sudo -u egsprice -H env GIT_SSH_COMMAND="ssh -i /opt/egs-price/.ssh/egs_report -o IdentitiesOnly=yes -o UserKnownHostsFile=/opt/egs-price/.ssh/known_hosts" \
  git ls-remote git@github.com:f2aquatic-lang/egs-price-reports.git HEAD
```

Harus mencetak satu baris berisi hash. Lalu ambil `main` ke folder baru (folder ZIP lama tidak disentuh). Sejak itu pembaruan kode cukup `git pull`, tanpa ZIP:

```bash
sudo -u egsprice -H env GIT_SSH_COMMAND="ssh -i /opt/egs-price/.ssh/egs_report -o IdentitiesOnly=yes -o UserKnownHostsFile=/opt/egs-price/.ssh/known_hosts" \
  git clone -b main git@github.com:f2aquatic-lang/egs-price-reports.git /opt/egs-raw/main
```

Skrip penerbit hanya mau jalan dari branch `main` supaya tidak pernah mendorong pekerjaan yang belum di-merge. Ia menarik `main` dulu, menjalankan pengumpul, lalu hanya mendorong `raw.json`. Kalau bot mendorong `latest.json` pada saat yang sama, ia mencoba lagi sampai 3 kali.

Berkas `/opt/egs-raw/env` bisa berisi `EGS_LANG_MODE=text` (tanpa kunci), atau kunci dan penyedia seperti di Langkah 3. Kalau memakai foto, tambahkan juga `PYTHON=/opt/egs-raw/venv/bin/python` di sana. Lalu satu layanan dan satu pemicu baru, sejalan dengan jadwal bot yang sudah ada:

```bash
sudo tee /etc/systemd/system/egs-raw.service >/dev/null <<'EOF'
[Unit]
Description=EGS RAW price collector
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=egsprice
WorkingDirectory=/opt/egs-raw/main
EnvironmentFile=-/opt/egs-raw/env
ExecStart=/opt/egs-raw/main/tools/publish_raw.sh --db /opt/egs-raw/raw.sqlite
TimeoutStartSec=3000
EOF
sudo tee /etc/systemd/system/egs-raw.timer >/dev/null <<'EOF'
[Unit]
Description=EGS RAW price collector, every 6 hours

[Timer]
OnCalendar=*-*-* 01,07,13,19:20:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now egs-raw.timer
systemctl list-timers egs-raw.timer --no-pager
```

Jam di VPS memakai CST (UTC+8), jadi 01:20, 07:20, 13:20, dan 19:20 CST sama dengan 00:20, 06:20, 12:20, dan 18:20 WIB. Waktu itu sengaja jauh dari timer bot (observasi di menit 30, laporan di menit 10). Coba sekali dengan `sudo systemctl start egs-raw.service`, lalu lihat hasilnya dengan `journalctl -u egs-raw.service -n 50 --no-pager`. Untuk mematikannya: `sudo systemctl disable --now egs-raw.timer`.

## Kartu lain (katalog)

Bawaan: kartu di luar daftar pantau yang kode set dan nomornya terbaca di judul ikut didata (`catalog: true` di `raw.json`). Dengan pilihan C (teks saja) ini gratis dan langsung menambah ratusan kartu. Dengan foto (A atau B), tiap listing katalog dicek fotonya satu kali, jadi putaran awal bisa memakan banyak anggaran: batasi dengan `--max-checks` (bawaan 150, listing kartu yang dipantau didahulukan). Matikan dengan `--no-catalog`. Lihat `tools/db_report.py` untuk melihat kartu apa saja yang terdata. Pengumpul juga menelusuri set yang paling sering muncul (bawaan 8 set, paling banyak 400 permintaan pencarian per putaran, sekitar 10 menit); atur dengan `--sets`, `--set-budget`, `--set-pages`, atau matikan dengan `--sets 0`.

## Katalog resmi (nama, rarity, semua kartu per set)

Katalog resmi diambil dari situs kartu Indonesia dengan `tools/fetch_catalog.py` (hanya pustaka standar Python, satu permintaan per detik, bisa dilanjutkan). Pengambilan pertama memakan sekitar satu sampai dua jam untuk sekitar 5.000 kartu; setelah itu hanya yang baru.

```bash
# percobaan kecil: daftar set, lalu satu set dengan batas permintaan
python3 tools/fetch_catalog.py --list-sets
python3 tools/fetch_catalog.py --db /tmp/catalog_test.sqlite --out /tmp/catalog_test.json --sets MA6 --max-requests 25

# pengambilan penuh di latar belakang (melanjutkan kalau terputus)
sudo -u egsprice -H nohup python3 /opt/egs-raw/main/tools/fetch_catalog.py \
  --db /opt/egs-raw/catalog.sqlite --out /opt/egs-raw/catalog.json > /opt/egs-raw/catalog.log 2>&1 &
tail -n 3 /opt/egs-raw/catalog.log
```

Bawaannya hanya set booster utama (`MA1`..`MA6`, `SV1S`..`SV11S`, `SV1A`/`SV2A`/`SV4A`/`SV8A`, `AC3A`/`AC3B`); ubah dengan `--include` (ekspresi reguler) atau `--sets KODE,KODE`. Tiap set juga menelusuri rarity (satu daftar tersaring per kode rarity); matikan dengan `--no-rarities`.

Sesudah itu `publish_raw.sh` menyalin `/opt/egs-raw/catalog.json` ke repo dan membangun halaman dengannya, jadi papan menampilkan semua kartu tiap set. Untuk memperbarui katalog tiap minggu (set dan kartu baru), tambahkan layanan dan pemicu:

```bash
sudo tee /etc/systemd/system/egs-catalog.service >/dev/null <<'EOF'
[Unit]
Description=EGS official card catalog fetch
After=network-online.target

[Service]
Type=oneshot
User=egsprice
ExecStart=/usr/bin/python3 /opt/egs-raw/main/tools/fetch_catalog.py --db /opt/egs-raw/catalog.sqlite --out /opt/egs-raw/catalog.json
TimeoutStartSec=14400
EOF
sudo tee /etc/systemd/system/egs-catalog.timer >/dev/null <<'EOF'
[Unit]
Description=EGS official card catalog, weekly

[Timer]
OnCalendar=Sun *-*-* 03:40:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now egs-catalog.timer
```

## Biaya

- Claude: model bawaan `claude-opus-5-5` (US$4 per juta token masukan, US$20 per juta token keluaran). Yang lebih murah, `claude-haiku-4-5` (US$1 dan US$5), dipilih dengan menambah satu baris `EGS_LANG_MODEL=claude-haiku-4-5` di `/opt/egs-raw/env`. Itu keputusan Anda. Kalau memakai yang murah, ulangi Langkah 4 dulu karena akurasinya belum diuji.
- OpenAI: harga mengikuti model yang Anda pilih; lihat halaman harga OpenAI untuk model itu. Skrip mencetak jumlah token tiap putaran, jadi biaya bisa dihitung sendiri.
- Pilihan C (teks saja) tidak berbiaya.
- Saringan teks menyingkirkan kartu asing tanpa biaya, dan tiap listing dicek foto **satu kali** (hasilnya disimpan). `--max-checks` (bawaan 150 per putaran) membatasi biaya per putaran.
- Angka rupiah yang pasti baru ketahuan dari baris `pemakaian token` pada Langkah 5.

## Alat pemeriksa

`tools/probe_toco*.py` adalah skrip diagnostik yang dipakai untuk mempelajari toco. Tidak diperlukan untuk menjalankan pengumpul, tetapi berguna kalau susunan toco berubah dan pengumpul berhenti menemukan listing.
