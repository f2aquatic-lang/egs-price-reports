# Menjalankan pengumpul RAW di VPS

Pengumpul (`tools/collect_raw.py`) mencari listing RAW di toco untuk setiap kartu yang dipantau, membaca bahasa kartu (teks dulu, foto untuk sisanya) dengan Claude, lalu menulis `raw.json`. Panduan ini mengikuti VPS Anda sebagaimana hasil pemeriksaan: Ubuntu 24.04, Python 3.12, bot EGS berjalan sebagai pengguna `egsprice` di `/opt/egs-price/` dan mendorong ke GitHub lewat kunci SSH, dengan jadwal systemd.

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

## Langkah 3. Pasang paket Claude dan simpan kunci

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

Berkas itu hanya bisa dibaca `egsprice`. Jangan kirim kunci ke chat dan jangan masukkan ke repo. Paket dipasang di venv sendiri, jadi lingkungan Python bot tidak berubah.

## Langkah 4. Uji pembaca bahasa pada foto nyata

```bash
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --sample-check "Zekrom N 210/193"
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --sample-check "Mega Dragonite ex 232/193"
```

Tiap perintah memeriksa 6 foto dan mencetak baris seperti `foto=ID | teks=ID | jenis=raw | Rp… | judul`. Tidak ada yang disimpan. Yang harus benar: kartu Jepang atau Inggris **tidak boleh** terbaca `foto=ID`. Terbaca `UNCLEAR` tidak masalah, listing itu hanya tidak dihitung. **Tempel keluarannya ke sini** supaya saya bisa menilai akurasinya sebelum lanjut.

## Langkah 5. Putaran nyata kecil

```bash
sudo -u egsprice -H /opt/egs-raw/repo/tools/run_raw.sh --queries-limit 3 --max-checks 20 --out /opt/egs-raw/raw.json
cd /opt/egs-raw/repo && python3 tools/validate_raw.py /opt/egs-raw/raw.json
```

Harus berakhir dengan `0 error(s)`. Hasilnya ditulis di `/opt/egs-raw/raw.json`, di luar repo. Skrip mencetak `pemakaian token`, dari situ biaya per foto bisa dihitung.

## Langkah 6. Jadwalkan (sesudah pengumpul di-merge ke main dan Anda setuju)

Skrip penerbit hanya mau jalan dari branch `main`, supaya tidak pernah mendorong pekerjaan yang belum di-merge:

```bash
sudo -u egsprice -H git -C /opt/egs-raw/repo checkout main
sudo -u egsprice -H git -C /opt/egs-raw/repo pull
```

Lalu satu layanan dan satu pemicu baru, sejalan dengan jadwal bot yang sudah ada:

```bash
sudo tee /etc/systemd/system/egs-raw.service >/dev/null <<'EOF'
[Unit]
Description=EGS RAW price collector
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=egsprice
WorkingDirectory=/opt/egs-raw/repo
EnvironmentFile=/opt/egs-raw/env
Environment=PYTHON=/opt/egs-raw/venv/bin/python
ExecStart=/opt/egs-raw/repo/tools/publish_raw.sh --db /opt/egs-raw/raw.sqlite
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

## Biaya

- Model bawaan `claude-opus-5-5` (US$4 per juta token masukan, US$20 per juta token keluaran). Yang lebih murah, `claude-haiku-4-5` (US$1 dan US$5), dipilih dengan menambah satu baris `EGS_LANG_MODEL=claude-haiku-4-5` di `/opt/egs-raw/env`. Itu keputusan Anda. Kalau memakai yang murah, ulangi Langkah 4 dulu karena akurasinya belum diuji.
- Saringan teks menyingkirkan kartu asing tanpa biaya, dan tiap listing dicek foto **satu kali** (hasilnya disimpan). `--max-checks` (bawaan 150 per putaran) membatasi biaya per putaran.
- Angka rupiah yang pasti baru ketahuan dari baris `pemakaian token` pada Langkah 5.

## Alat pemeriksa

`tools/probe_toco*.py` adalah skrip diagnostik yang dipakai untuk mempelajari toco. Tidak diperlukan untuk menjalankan pengumpul, tetapi berguna kalau susunan toco berubah dan pengumpul berhenti menemukan listing.
