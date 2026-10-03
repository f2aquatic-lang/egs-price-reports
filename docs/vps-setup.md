# Menjalankan pengumpul RAW di VPS

Pengumpul (`tools/collect_raw.py`) mencari listing RAW di toco untuk setiap kartu yang dipantau, membaca bahasa kartu dari foto listing dengan Claude, lalu menulis `raw.json`. Panduan ini untuk dikerjakan di VPS, satu langkah demi satu. Langkah 1 sampai 6 aman: tidak ada yang dikirim ke GitHub.

Yang perlu ada: Python 3.10 atau lebih baru, akses git ke repo ini (bot EGS sudah punya), dan kunci API Anthropic.

## Mulai di sini: satu perintah, tanpa kunci

Ini yang pertama dijalankan. Tidak perlu kunci API, tidak memasang apa pun, tidak menulis file, dan tidak menyentuh GitHub. Skripnya mandiri (`tools/probe_toco.py`, hanya pustaka standar Python) dan mengirim sekitar enam permintaan ke toco.id, berjeda 2 detik.

Yang diperlukan adalah terminal ke VPS: SSH dari HP, atau konsol web di panel penyedia VPS. Asisten yang tidak punya terminal ke VPS (misalnya ChatGPT dalam percakapan biasa) tidak bisa menjalankannya.

Tanpa git, paling mudah dari HP:

1. Buka https://github.com/f2aquatic-lang/egs-price-reports/blob/claude/quirky-bardeen-bmq87l/tools/probe_toco.py dan salin isinya.
2. Di VPS: `nano probe_toco.py`, tempel, simpan.
3. Jalankan `python3 probe_toco.py`.

Kirim **seluruh keluarannya** (sekitar 30 baris). Itu cukup untuk menyesuaikan pengumpul dengan susunan halaman toco, dan sekaligus menunjukkan apakah nomor seri slab tertulis di teks halaman.

Jangan pernah mengirim kunci pribadi SSH atau kunci API ke chat, ke saya, atau ke siapa pun. Saya tidak membutuhkannya dan tidak punya akses ke VPS Anda.

Langkah 1 sampai 7 di bawah dikerjakan sesudah itu.

## Langkah 1. Ambil kode

Pakai folder baru, jangan folder bot EGS. Alamat repo sama dengan yang dipakai bot: `git -C <folder bot> remote get-url origin`.

```bash
cd ~
git clone <ALAMAT REPO> egs-raw-collector
cd egs-raw-collector
git checkout claude/quirky-bardeen-bmq87l   # hanya kalau pengumpul belum di-merge ke main
git config user.name "EGS RAW Bot"
git config user.email "egs-raw@local.invalid"
```

## Langkah 2. Pasang paket Claude

```bash
python3 -m venv ~/egs-raw/venv
~/egs-raw/venv/bin/pip install anthropic
```

Seterusnya `~/egs-raw/venv/bin/python` ditulis `$PY`. Kalau `python3 --version` di bawah 3.10, pasang Python yang lebih baru dulu.

## Langkah 3. Simpan kunci API

```bash
nano ~/egs-raw/env        # isi satu baris: export ANTHROPIC_API_KEY=sk-ant-...
chmod 600 ~/egs-raw/env
```

Kunci hanya ada di VPS. Jangan dimasukkan ke repo. Database kerja pengumpul ada di `~/.egs-raw/raw.sqlite`, di luar repo.

## Langkah 4. Coba baca halaman toco (gratis)

```bash
cd ~/egs-raw-collector
$PY tools/collect_raw.py --dry-run --queries-limit 3 --save-html ~/egs-raw/html
```

Ini tidak memanggil Claude dan tidak menulis `raw.json`. Yang harus terlihat: tiap kata kunci membaca satu halaman atau lebih dan menemukan puluhan listing, lalu ringkasan seperti `jenis_raw`, `cocok_nomor`, `cocok_nama+set`.

Kalau keluar "Tidak ada listing yang terbaca" atau angkanya aneh, **berhenti dan kirim hasilnya**: seluruh output, plus satu file dari `~/egs-raw/html`. Susunan halaman toco belum pernah saya lihat langsung (dari sandbox saya toco diblokir), jadi parsernya mungkin perlu disesuaikan. Dua hal lain yang belum terverifikasi: nama parameter halaman pada URL pencarian (`--page-param`, bawaannya `page`) dan apakah foto muncul di hasil pencarian atau harus diambil dari halaman listing.

## Langkah 5. Uji pembaca bahasa dengan foto

Siapkan sekitar 20 foto kartu yang bahasanya sudah Anda ketahui (campuran Indonesia, Jepang, Inggris). Untuk tiap foto:

```bash
. ~/egs-raw/env
$PY tools/collect_raw.py --test-image ~/foto/kartu1.jpg
```

Hasilnya `bahasa kartu = ID`, `JP`, `EN`, `OTHER`, atau `UNCLEAR`, dengan jumlah token yang dipakai. Yang tidak boleh terjadi: kartu Jepang atau Inggris terbaca `ID`. Kalau terbaca `UNCLEAR`, tidak masalah, listing itu hanya tidak dihitung.

## Langkah 6. Putaran nyata kecil

```bash
. ~/egs-raw/env
$PY tools/collect_raw.py --queries-limit 3 --max-checks 10
$PY tools/validate_raw.py raw.json
```

Harus berakhir dengan `0 error(s)`. Skrip mencetak `pemakaian token`, dari situ biaya per foto bisa dihitung.

## Langkah 7. Jadwalkan (setelah di-merge ke main)

`tools/publish_raw.sh` hanya mau jalan dari branch `main`, supaya tidak pernah mendorong pekerjaan yang belum di-merge. Setelah pengumpul ada di `main`:

```bash
cd ~/egs-raw-collector && git checkout main && git pull -q origin main
crontab -e
```

Tambahkan satu baris. Waktunya sengaja jauh dari bot EGS, yang terbit di menit 10 pada jam 02, 08, 14, 20 UTC:

```
40 1,7,13,19 * * * . $HOME/egs-raw/env; PYTHON=$HOME/egs-raw/venv/bin/python $HOME/egs-raw-collector/tools/publish_raw.sh >> $HOME/egs-raw/log.txt 2>&1
```

Cek hasilnya dengan `tail -30 ~/egs-raw/log.txt`.

## Biaya

- Model bawaan `claude-opus-5-5` (US$4 per juta token masukan, US$20 per juta token keluaran). Model yang lebih murah, `claude-haiku-4-5` (US$1 dan US$5), dipilih dengan `export EGS_LANG_MODEL=claude-haiku-4-5` di file `~/egs-raw/env`. Pilihan ini keputusan Anda. Kalau memakai yang murah, ulangi Langkah 5 dulu karena akurasinya belum diuji.
- Tiap listing dicek **satu kali**, hasilnya disimpan. `--max-checks` (bawaan 150) membatasi jumlah foto per putaran, jadi biaya per putaran punya batas atas.
- Angka rupiah yang pasti belum bisa saya berikan. Ukur dari baris `pemakaian token` pada Langkah 6.

## Kalau memakai asisten yang punya terminal di VPS

Hanya kalau asisten itu benar-benar bisa menjalankan perintah di VPS, tempel ini:

> Di VPS, simpan isi tools/probe_toco.py dari branch claude/quirky-bardeen-bmq87l repo f2aquatic-lang/egs-price-reports sebagai probe_toco.py, lalu jalankan `python3 probe_toco.py` dan kirim seluruh keluarannya. Jangan ubah latest.json, folder atau jadwal bot EGS yang sudah ada, dan jangan push apa pun ke GitHub. Jangan lanjut ke langkah lain sebelum saya setuju.

Kalau asisten itu hanya bisa membaca repo, file di branch `claude/quirky-bardeen-bmq87l` dapat dibaca, tetapi di branch utama (`main`) baru ada setelah di-merge.
