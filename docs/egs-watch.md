# Pemantau slab EGS

Memperluas pemantauan slab EGS dari enam listing ke puluhan, tanpa mengubah bot yang ada. Dua alat baru, tiga berkas data, satu timer baru:

| Berkas | Isi |
|---|---|
| `tools/egs_identity.py` | memutuskan apakah satu listing adalah satu kartu Indonesia tertentu dalam satu grade EGS (tanpa jaringan) |
| `tools/egs_watch.py` | penemuan listing, pengecekan identitas, penyimpanan bukti, statistik, ekspor `egs_watch.json` |
| `tools/egs_hits.json` | kata kunci: EGS, slab EGS, EGS 10 / 9.5 / 9, nama kartu hit; tambah nama di sini |
| `tools/probe_marketplaces.py` | menguji apakah Tokopedia dan Shopee bisa dibaca sama sekali (lihat bagian terakhir) |
| `tools/systemd/egs-watch.{service,timer}` | satu pemicu baru, empat kali sehari |

Yang **tidak** disentuh: `latest.json` (hanya dibaca), bot lama di `/opt/egs-price`, jadwalnya, login atau anggota, pengumpul RAW (`collect_raw.py`), dan jadwal RAW. Pemantau punya database sendiri (`/opt/egs-raw/egs_watch.sqlite`) dan berkas keluaran sendiri (`/opt/egs-raw/egs_watch.json`); `publish_raw.sh` hanya menyalin berkas itu ke repo seperti `catalog.json`.

## Satu putaran

1. **Penemuan.** Pencarian milik toco sendiri (permintaan yang sama dengan yang dibuat situsnya), dengan kata kunci dari `egs_hits.json`, dari setiap kartu yang dipantau bot lama, dari nama kartu yang sudah diterima, dan dari judul kandidat lama di `latest.json`. Tiap kata kunci dibaca halaman demi halaman sampai hasilnya habis, halamannya berulang, atau halamannya tidak lagi tentang EGS (kurang dari 20% menyebut EGS). Ada anggaran permintaan (320 per putaran), jeda 1,5 detik, cache 20 menit, dan pengulangan bertahap (3, 8, 20 detik; `Retry-After` dihormati). Kalau situs menjawab dengan 401/403, halaman verifikasi, atau jawaban yang bukan data, **berhenti**: tidak ada langkah untuk melewatinya.
2. **Identitas.** Aturannya di bawah. Hanya bukti yang konsisten yang masuk daftar pantau; yang meragukan masuk antrean dengan foto dan alasannya.
3. **Bukti.** Tiap pengamatan (harga, stok, waktu, alamat sumber, foto) disimpan. Listing yang tidak muncul di pencarian diperiksa satu per satu.
4. **Statistik.** Per kartu dan grade: jumlah listing, seller unik, rata-rata, median, rentang. Satu atau dua listing diberi label "data terbatas".
5. **Ekspor.** `egs_watch.json` ditulis hanya kalau lolos validasinya sendiri dan sumbernya sehat; kalau tidak, berkas lama dibiarkan.

## Aturan identitas

Diterima otomatis hanya kalau **semua** ini benar:

- perusahaan grading EGS tertulis (judul/deskripsi) atau terbaca di foto; tidak ada perusahaan lain yang disebut (kata seperti "not TAG" diabaikan);
- grade terbaca dan hanya satu (judul dan deskripsi sepakat; 10, 9.5, 9, 8.5, 8, 7.5, 7);
- bahasa Indonesia terbukti: kata ID/Indo/Indonesia, kode set MA (hanya dipakai kartu Indonesia), atau foto. Tidak adanya kata asing **bukan** bukti;
- satu kartu: tidak ada kata paket/lot/pilihan/jumlah, satu nomor kartu saja;
- kartunya dikenali di katalog resmi: nomor tertulis, atau `#nomor` dengan set, atau (kalau nomor tidak ditulis) set + rarity + nama yang hanya cocok dengan **satu** kartu; nama dan rarity harus sama dengan katalog (rarity yang berbeda, misalnya judul SR padahal katalog SAR, membuatnya ambigu, tidak dibetulkan diam-diam);
- alamat listing masih cocok dengan judulnya: alamat menyimpan judul pertama, jadi nama kartu atau kata Jepang di alamat yang sudah tidak ada di judul menandakan listing dipakai ulang untuk kartu lain (kata pengisi seperti "original" atau "ready" tidak dihitung);
- harga masuk akal (di atas Rp50.000) dan, kalau nomornya punya versi foil dan judul hanya menulis "holo", polanya jelas.

Ditolak (tidak masuk antrean): bukan Pokémon (One Piece, Dragon Ball, Digimon ..., atau judul tanpa satu pun tanda Pokémon: kata Pokémon, kode set, rarity, nomor kartu, atau nama kartu di katalog), produk segel, perusahaan grading lain, kartu RAW, bahasa asing (judul Jepang, Inggris, Mandarin; foto JP atau EN). Selain itu **ambigu**: tetap disimpan dengan foto, tautan, alasan, dan daftar kartu katalog yang mungkin cocok, tetapi tidak masuk hitungan harga apa pun.

Foto dibaca (company, grade, nomor sertifikat, nomor kartu, rarity, bahasa) hanya dengan `--photo-mode openai|anthropic`: **bawaan mati**, karena butuh saldo API sendiri. Uji dulu dengan satu foto: `egs_watch.py --photo-mode openai --read-photo <alamat foto>` (kunci di `OPENAI_API_KEY`; nama model dengan `--photo-model`). Foto boleh melengkapi teks atau membantah; tidak pernah menimpa konflik. Pencocokan ilustrasi dengan katalog **belum** dibuat: tanpa nomor, tanpa foto yang terbaca, dan tanpa kecocokan tunggal di katalog, listing tetap di antrean.

## Status listing

`ok` (terbaca di pencarian, stok ada) · `unavailable` (terbaca tetapi stok 0, diarsipkan, atau disembunyikan) · `gone` (halamannya 404/410) · `missing_from_search` (tidak muncul di pencarian, halamannya masih ada) · `fetch_failed` (tidak bisa dicek). **Tidak satu pun berarti terjual.** Hanya `ok` yang dihitung penuh; `missing_from_search` dan `fetch_failed` masih dihitung selama pemeriksaan baik terakhir kurang dari 48 jam; `unavailable` dan `gone` tidak dihitung.

## Duplikat dan harga ekstrem

Satu nomor sertifikat adalah satu slab. Tanpa sertifikat, semua penawaran satu seller untuk kartu dan grade yang sama dihitung sekali (yang terbaru, lalu yang termurah), dan penawaran tanpa sertifikat di sebelah penawaran bersertifikat dari seller yang sama dianggap slab yang sama. Penawaran yang lebih murah dari 25% atau lebih mahal dari 4 kali median penawaran lain untuk kartu dan grade yang sama (kalau ada setidaknya empat penawaran) tampil tetapi tidak dihitung.

## Ringkasan di `egs_watch.json` dan di papan

`summary` memuat: kandidat ditemukan, diterima (lama / otomatis / baru putaran ini), ambigu, ditolak beserta alasannya, listing dihitung, kartu, seller unik, pemeriksaan berhasil dan gagal, status per jenis, perubahan harga 24 jam, dan kesegaran (≤8 jam, ≤24 jam, lebih lama). Papan menampilkannya di bagian "Pemantauan slab EGS", lengkap dengan antrean ambigu (foto, tautan, alasan, kemungkinan kartu). Listing yang diterima dan dihitung muncul di strip kartu seperti listing lama; `latest.json` tidak diubah.

```
python3 tools/egs_watch.py --report /opt/egs-raw/egs_watch.json     # laporan teks
python3 tools/egs_watch.py --validate /opt/egs-raw/egs_watch.json
```

## Pemasangan

Semuanya dijalankan di VPS. Urutannya sengaja: cadangan, uji kering, putaran nyata, baru jadwal.

```
# 1. cadangan (catat juga versi kodenya untuk jalan mundur)
sudo mkdir -p /opt/backup
sudo tar czf /opt/backup/egs-sebelum-pemantau-$(date +%Y%m%d-%H%M).tgz --ignore-failed-read --exclude=.ssh --exclude=__pycache__ \
  /opt/egs-price /opt/egs-raw/raw.sqlite /opt/egs-raw/catalog.json /opt/egs-raw/jp_prices.json /opt/egs-raw/rates.json \
  /etc/systemd/system/egs-*.service /etc/systemd/system/egs-*.timer
sudo -u egsprice git -C /opt/egs-raw/main rev-parse HEAD

# 2. tarik kodenya (kunci milik bot; satu baris)
sudo -u egsprice -H env GIT_SSH_COMMAND="ssh -i /opt/egs-price/.ssh/egs_report -o IdentitiesOnly=yes -o UserKnownHostsFile=/opt/egs-price/.ssh/known_hosts" git -C /opt/egs-raw/main pull --rebase

# 3. uji kering: membaca toco sungguhan, menilai, mencetak laporan; tidak menulis database atau hasil
sudo -u egsprice -H python3 /opt/egs-raw/main/tools/egs_watch.py --dry-run --budget 60 \
  --db /opt/egs-raw/egs_watch.sqlite --latest /opt/egs-raw/main/latest.json --catalog /opt/egs-raw/main/catalog.json

# 4. putaran nyata pertama (sekitar 10 menit), lalu baca hasilnya
sudo -u egsprice -H python3 /opt/egs-raw/main/tools/egs_watch.py \
  --db /opt/egs-raw/egs_watch.sqlite --out /opt/egs-raw/egs_watch.json \
  --latest /opt/egs-raw/main/latest.json --catalog /opt/egs-raw/main/catalog.json
sudo -u egsprice -H python3 /opt/egs-raw/main/tools/egs_watch.py --validate /opt/egs-raw/egs_watch.json

# 5. jadwal: satu timer baru, 05:50 / 11:50 / 17:50 / 23:50 WIB (30 menit sebelum pengumpul RAW, yang menerbitkan hasilnya)
sudo cp /opt/egs-raw/main/tools/systemd/egs-watch.service /opt/egs-raw/main/tools/systemd/egs-watch.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now egs-watch.timer

# 6. terbitkan sekarang (opsional; kalau tidak, putaran RAW berikutnya yang menerbitkan)
sudo systemctl start egs-raw.service
```

**Alasan jadwal baru:** pemantau butuh waktunya sendiri (sekitar 10 menit, 320 permintaan) dan tidak boleh memperlambat pengumpul RAW atau bot lama, jadi ia punya timer sendiri. Jadwal bot lama dan jadwal RAW tidak diubah.

**Jalan mundur:** `sudo systemctl disable --now egs-watch.timer`, lalu `sudo rm /etc/systemd/system/egs-watch.*`. Tidak ada yang dihapus dari bot lama. Menghapus `egs_watch.json` dari repo membuat papan kembali seperti sebelumnya.

Kegagalan sumber: `egs_watch.py` berakhir dengan kode 2 (layanan tercatat `failed`, tetapi tidak berbahaya) dan membiarkan `egs_watch.json` terakhir yang valid. Kode 3: hasilnya tidak lolos validasinya sendiri, berkas lama dipertahankan.

## Memperluas ke 50-100 kartu hit

1. Tambahkan nama di `hits` dan `deep_hits` pada `tools/egs_hits.json`; tidak perlu mengubah kode.
2. Katalog resmi sekarang memuat set `MA*`, `SV*`, `AC3A/B`. Listing kartu set lama (S-series, misalnya "Arceus V SR AA S9") tetap ambigu karena katalognya belum ada; tambahkan lewat `fetch_catalog.py --include` kalau mau dicakup.
3. Foto: `--photo-mode openai` (atau `anthropic`) dengan `--photo-budget 30` membaca nomor dan label dari foto kandidat yang nomornya tidak tertulis. Butuh saldo API; dihitung per foto, sekali per listing.

## Shopee dan Tokopedia

`tools/probe_marketplaces.py` menguji, dari VPS, apakah keduanya bisa dibaca tanpa login dan tanpa melewati pembatasan (halaman pencarian publik, dan panggilan pencarian milik Shopee sendiri; `--browser` membaca halaman seperti browser pengunjung kalau Playwright ada). Hasilnya satu putusan per situs: `ok`, `login`, `captcha`, `blocked`, `empty`, atau `error`. **Hanya `ok` yang bisa dijadikan sumber.** Tantangan verifikasi, halaman masuk, dan kode anti-bot tidak dilewati. Kalau ada jalur yang `ok`, sumbernya ditambahkan sebagai satu sumber lagi di `egs_watch.py` (tiap sumber cukup menghasilkan satu bentuk data seperti `normalize()`: judul, deskripsi, harga, stok, foto, seller), dengan aturan identitas, dedupe, dan statistik yang sama.

Facebook dan WhatsApp **tidak** dibaca: tidak ada akses yang tersedia secara sah.
