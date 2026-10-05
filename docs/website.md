# Situs papan harga (RAW dan slab EGS)

Papan harga adalah satu halaman web: tiap kartu yang dipantau menampilkan harga RAW dan slab EGS berdampingan, di bawahnya tabel semua kartu RAW yang terdata (bisa dicari, ada foto dan tautan ke listing termurah, serta tren naik-turun). Halaman dibangun dari `latest.json` (bot EGS) dan `raw.json` (pengumpul RAW) dengan `tools/build_site.py`, dan diperbarui sendiri setiap kali `raw.json` terbit (tiap 6 jam).

## Pengelompokan per set

Tabel RAW dikelompokkan per set (MA1 sampai MA6, SV1S sampai SV11S, dan seterusnya) dengan pilihan set di atasnya, dikelompokkan per seri (MEGA, Scarlet & Violet, lainnya). Tiap set menampilkan nama Indonesia, nama Inggris, tanggal rilis, dan berapa kartu yang sudah punya harga. Kartu diurutkan menurut nomor; seri sampingan di dalam satu set (misalnya kartu Pikachu bernomor n/30 di MA6) ditaruh setelah nomor utama. Kotak cari mencari di semua set.

Kalau `catalog.json` (katalog resmi, lihat [raw-dataset.md](raw-dataset.md)) ada, tabel menampilkan **semua kartu resmi** tiap set: kartu dengan listing menampilkan rentang harga listing; kartu common tanpa listing menampilkan harga dasar Rp500 dengan tanda "harga dasar"; kartu lain tanpa listing menulis "belum ada listing" (tidak pernah 0). Foto kartu memakai gambar resmi (dimuat langsung dari situs resmi oleh browser pengunjung), dan kotak "Sembunyikan kartu tanpa harga" menyembunyikan yang belum ada listing. Nama set, tanggal rilis, dan jumlah kartu juga dari katalog resmi.

Versi foil dari satu nomor kartu (Poké Ball dan Master Ball di set High Class) tampil sebagai baris sendiri dengan tanda versinya, karena harganya berbeda; lihat [raw-dataset.md](raw-dataset.md).

Nama set tulisan tangan ada di `tools/sets.json`, ditulis tangan dari hasil pencarian web dan dari judul listing. Kolom `basis` di berkas itu menunjukkan seberapa pasti: **pencarian web** (nama dan sebagian tanggal dari Bulbapedia, pokeunlimited, krystalkollectz; halamannya belum dibuka langsung), **judul listing** (nama Indonesia terbaca dari judul), **perkiraan**. Set yang belum ada di berkas itu tetap tampil dengan kodenya saja. Koreksi nama, tanggal, atau jumlah kartu langsung di `sets.json`; halaman memakainya pada putaran berikutnya.

## Cara kerja

```
VPS: egs-raw.service  →  git pull  →  collect_raw.py  →  git push raw.json  →  build_site.py  →  /var/www/egs/index.html
                                                                                                        ↓
                                                                        Caddy: HTTPS + kata sandi (alamat sslip.io)
```

- Situs dilayani Caddy dari VPS Anda sendiri. Tidak ada akun pihak ketiga dan tidak ada biaya.
- Alamatnya `https://<ip-publik-dengan-strip>.sslip.io/`, misalnya `https://43-156-136-140.sslip.io/`. sslip.io adalah layanan nama gratis yang menunjuk ke IP itu, jadi tidak perlu membeli domain. Kalau nanti punya domain sendiri, jalankan ulang skrip dengan nama itu.
- **Kunci:** satu nama pengguna dan satu kata sandi (HTTP basic auth lewat HTTPS). Tanpa itu halaman tidak bisa dibuka, dan mesin pencari dilarang lewat `robots.txt`, header `X-Robots-Tag`, dan meta `noindex`.
- Foto kartu dimuat browser pengunjung langsung dari tempat penjual menyimpannya (toco), tanpa Referer. Foto itu milik penjual: halaman ini untuk pemakaian internal, bukan untuk dijadikan situs terbuka.

## Pemasangan (sekali)

**1. Buka port di firewall Tencent Lighthouse.** Di konsol Tencent Cloud: Lighthouse → instance `OpenClaw-GGsa` → tab **Firewall** → **Add rule**. Tambahkan dua aturan TCP: port **80** dan port **443**, sumber semua IPv4 (`0.0.0.0/0`). Templat "HTTP (80)" dan "HTTPS (443)" juga boleh. Port 80 diperlukan Caddy untuk mengambil sertifikat HTTPS gratis dari Let's Encrypt, port 443 untuk situsnya.

Di VPS, kalau `sudo ufw status` menjawab `Status: active`, buka juga di sana: `sudo ufw allow 80,443/tcp`. Kalau `inactive`, tidak perlu.

**2. Jalankan skrip pemasang** (di VPS, dari folder kode):

```bash
cd /opt/egs-raw/main
sudo bash tools/setup_site.sh
```

Skrip ini:
- berhenti kalau port 80 atau 443 sudah dipakai program lain (bot tidak diganggu),
- memasang Caddy dari paket Ubuntu,
- menanyakan nama pengguna (bawaan `tim`) dan kata sandi (diminta dua kali, minimal 12 karakter, jangan yang dipakai di tempat lain; kata sandi tidak disimpan, hanya hash-nya di `/etc/caddy/Caddyfile`),
- menulis `/etc/caddy/Caddyfile` (yang lama dicadangkan), mengisi `EGS_SITE_DIR=/var/www/egs` di `/opt/egs-raw/env` supaya tiap putaran memperbarui halaman, membangun halaman pertama dari `raw.json` yang ada, lalu menjalankan Caddy,
- mencetak alamat situs.

**3. Buka alamatnya** di browser. Beberapa menit pertama sertifikat HTTPS bisa belum siap (peringatan keamanan atau tidak terbuka); tunggu lalu muat ulang. Kalau tetap gagal: `journalctl -u caddy -n 30 --no-pager` dan periksa bahwa kedua port sudah terbuka di firewall.

## Sehari-hari

- Halaman diperbarui otomatis bersama `raw.json`. Cek waktu terakhir di bagian atas halaman (laporan, observasi, data RAW; semua WIB).
- Memperbarui halaman sekarang juga: `sudo systemctl start egs-raw.service`.
- Mengganti kata sandi atau pengguna: jalankan `sudo bash tools/setup_site.sh` lagi (aman diulang).
- Mematikan situs: `sudo systemctl disable --now caddy`, lalu tutup port 80 dan 443 di firewall.
- Kalau membangun halaman gagal, penerbitan `raw.json` tetap berjalan; halaman lama tetap tampil dan peringatannya ada di `journalctl -u egs-raw.service`.

## Batasan yang perlu diketahui

- Satu kata sandi bersama: siapa pun yang memilikinya bisa melihat dan menyalin semua data di halaman. Tidak ada log siapa yang masuk per orang. Kalau perlu akun per orang atau masuk lewat email, jalur berikutnya adalah Cloudflare Access (gratis sampai 50 orang, perlu akun Cloudflare dan domain).
- Basic auth tidak membatasi percobaan tebak kata sandi. Karena itu kata sandinya harus panjang.
- Ini melindungi dari pengambilan data oleh orang luar yang tidak punya kata sandi. Orang yang sudah masuk tetap bisa menyalin halaman.
- Satu halaman memuat seluruh data (sekitar 0,7 MB sekarang, naik seiring kartu bertambah). Kalau sudah terlalu berat, halaman dipecah per set.
