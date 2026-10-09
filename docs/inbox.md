# Folder masuk: listing dari alat lain (Dots)

Alat lain yang melihat listing di marketplace (Shopee, Tokopedia, dan sebagainya) menyerahkannya ke RadarKartu **lewat berkas yang dijatuhkan ke satu folder di VPS**. Pengumpul membaca folder itu tiap putaran, memeriksa setiap baris, lalu memasukkannya ke papan lewat jalur yang sama dengan listing toco.

```
Dots  →  /opt/egs-raw/inbox/*.json|csv  →  collect_raw.py (RAW)   →  raw.json        →  papan
                                         →  egs_watch.py (slab)   →  egs_watch.json  →  papan
```

**Jangan menulis langsung ke `raw.json`, `egs_watch.json`, database SQLite, atau repo.** Berkas itu dibuat ulang pengumpul tiap putaran (perubahan langsung hilang), dan menulis ke sana melompati pemeriksaan identitas, bahasa, dan harga yang ganjil. Folder masuk adalah satu-satunya pintu.

RadarKartu tidak mengetahui dan tidak memeriksa bagaimana listing itu diperoleh. Sumber dan syarat situs yang dibaca adalah tanggung jawab pemilik alatnya; akun yang dipakai untuk membaca halaman orang lain bisa dibatasi oleh situsnya.

## Menyiapkan folder (sekali)

```bash
sudo mkdir -p /opt/egs-raw/inbox
sudo chown egsprice:egsprice /opt/egs-raw/inbox
sudo chmod 2775 /opt/egs-raw/inbox
# kalau Dots berjalan sebagai pengguna lain (mis. ubuntu), masukkan ke grup egsprice lalu login ulang:
sudo usermod -aG egsprice ubuntu
```

Pengumpul RAW (`collect_raw.py`) dan pemantau slab (`egs_watch.py`) membaca `/opt/egs-raw/inbox`; ubah dengan `EGS_INBOX=/folder/lain` atau `--inbox`. Folder yang tidak ada dianggap kosong. Berkas hanya dibaca, tidak pernah dihapus atau dipindah.

## Format berkas

- Nama berakhiran `.json`, `.csv`, atau `.tsv`, UTF-8. Nama lain diabaikan.
- **Tulis ke nama sementara lalu ganti nama saat lengkap**: nama yang berakhiran `.tmp` atau `.part`, atau diawali titik, tidak dibaca. Berkas yang berubah dalam 20 detik terakhir ditunda ke putaran berikutnya, jadi berkas setengah jadi tidak pernah terbaca.
- JSON: daftar objek, atau `{"listings": [...]}`. CSV/TSV: baris pertama nama kolom (pemisah koma, titik koma, atau tab).
- Paling banyak 50.000 baris dan 20 MB per berkas.

| Kolom (nama lain yang diterima) | Wajib | Isi |
|---|---|---|
| `url` (`link`, `tautan`, `alamat`) | ya | alamat halaman listing, `http(s)://`; parameter pelacak dan `#...` dibuang |
| `judul` (`title`, `nama`) | ya | judul listing apa adanya, paling panjang 400 karakter |
| `harga` (`price`, `ask`) | ya | harga tawar dalam rupiah utuh: `2450000` atau `Rp2.450.000`; antara Rp500 dan Rp2.000.000.000. `1,250,000` (koma) ditolak karena bisa berarti desimal |
| `jenis` (`kind`, `tipe`) | ya | `raw` (kartu tanpa slab) atau `slab` (kartu yang sudah di-grading, mis. EGS) |
| `seen_at` (`dilihat`, `waktu`) | disarankan | kapan alat Anda melihat listing itu, ISO 8601 **dengan zona**: `2026-10-09T18:30:00+07:00` atau `2026-10-09T11:30:00Z`. Tanpa zona ditolak; tanpa kolom ini dipakai waktu berkas |
| `toko` (`seller`, `penjual`) | tidak | nama toko |
| `foto` (`photo`, `gambar`) | tidak | alamat gambar utama listing; alamat yang mengarah ke komputer lokal atau jaringan pribadi dibuang (barisnya tetap masuk) |
| `stok` (`stock`) | tidak | bawaan 1; 0 = habis |
| `deskripsi` | tidak | paling panjang 600 karakter dipakai |
| `kota` (`lokasi`) | tidak | kota penjual |
| `collector` (`pengumpul`) | tidak | nama alat pengirim, untuk catatan |

Contoh JSON:

```json
[
  {"url": "https://shopee.co.id/Pikachu-ex-SAR-i.111.222", "judul": "Pikachu ex SAR MA3 234/193 Kartu Pokemon TCG Indonesia",
   "harga": 2450000, "jenis": "raw", "seen_at": "2026-10-09T18:30:00+07:00", "toko": "Toko A", "foto": "https://down-id.example.com/a.jpg", "collector": "dots"},
  {"url": "https://www.tokopedia.com/tokob/pikachu-ex-egs-10", "judul": "Pikachu ex SAR 234/193 MA3 ID EGS 10",
   "harga": "Rp2.400.000", "jenis": "slab", "seen_at": "2026-10-09T18:31:00+07:00", "toko": "Toko B", "collector": "dots"}
]
```

Contoh CSV:

```
url,judul,harga,jenis,seen_at,toko
https://shopee.co.id/Pikachu-ex-SAR-i.111.222,Pikachu ex SAR MA3 234/193 Kartu Pokemon TCG Indonesia,2450000,raw,2026-10-09T18:30:00+07:00,Toko A
```

## Apa yang terjadi pada tiap baris

1. **Pemeriksaan berkas** (`tools/intake.py`): alamat http(s) dan dari situs yang diterima, judul, harga, jenis, waktu. Baris yang gagal **dilaporkan dan dilewati**, tidak diperbaiki dengan menebak.
2. **Situs yang diterima**: toco.id, shopee.co.id, shopee.com, shp.ee, tokopedia.com, tokopedia.link, tiktok.com, bukalapak.com, lazada.co.id, carousell.co.id, olx.co.id, kaskus.co.id, blibli.com, facebook.com, fb.com, instagram.com. **cardtell.id dan pokepedia.id tidak diterima**: syarat layanan Cardtell mewajibkan izin tertulis (Pasal 13.2) dan Pokepedia memblokir akses otomatis. Menambah situs berarti mengubah `ALLOWED_HOSTS` di `tools/intake.py`, sebaiknya hanya dengan izin situsnya.
3. **Penilaian yang sama dengan listing toco.** RAW: dicocokkan ke kartu (nomor, set, katalog), bahasa Indonesia harus terbukti (kata "Indonesia", "Indo", atau "ID" di judul atau deskripsi; bila mode foto aktif, dari foto), versi foil dikenali, listing yang lebih dari 6 kali harga Yuyutei tidak dihitung. Slab: identitas EGS, grade, nomor, rarity, bahasa; yang meragukan masuk antrean ambigu dan tidak masuk harga.
4. **Umur ikut `seen_at`, bukan waktu pembacaan.** Listing RAW dihitung 36 jam sejak `seen_at`, listing slab 48 jam; sesudah itu tetap tampil sebagai lama dan tidak masuk harga. Karena itu **kirim ulang listing yang masih ada** setiap hari (dengan `seen_at` baru); yang tidak dikirim ulang menua dan hilang dengan sendirinya.
5. **Membaca ulang berkas yang sama tidak mengubah apa pun**: baris dengan alamat dan `seen_at` yang sudah diambil dilewati, jadi berkas boleh dibiarkan di folder (sebaiknya ditimpa atau dihapus tiap hari). Alamat yang sama di beberapa berkas: pengamatan terbaru menang.
6. Pengumpul RAW hanya menulis `raw.json` kalau toco terbaca (penjaga lama tetap berlaku); selama toco gagal, berkas masuk menunggu putaran berikutnya.

## Laporan balik untuk Dots

Tiap putaran menulis `status/laporan-raw.txt` (pengumpul RAW) dan `status/laporan-slab.txt` (pemantau slab) di dalam folder masuk: berapa berkas dan baris dibaca, berapa diambil, berapa ditolak atau kedaluwarsa, sumbernya, dan **alasan tiap baris yang ditolak** (hingga 200). Dots sebaiknya membacanya dan memperbaiki format, bukan menebak.

## Pesan siap tempel untuk Dots

> Serahkan listing yang kamu lihat ke RadarKartu hanya lewat berkas di folder `/opt/egs-raw/inbox/` di VPS. Jangan menulis ke `raw.json`, `egs_watch.json`, database, atau repo.
> Tulis satu berkas `.json` (daftar objek) per pengiriman, ke nama sementara berakhiran `.tmp` lalu ganti nama menjadi `.json` setelah lengkap. Tiap objek: `url` (halaman listing), `judul` (apa adanya), `harga` (rupiah utuh, angka), `jenis` (`raw` atau `slab`), `seen_at` (waktu kamu melihatnya, ISO 8601 dengan zona, mis. `2026-10-09T18:30:00+07:00`), dan bila ada `toko`, `foto`, `stok`, `kota`, `deskripsi`, `collector: "dots"`.
> Kirim ulang listing yang masih aktif tiap hari dengan `seen_at` baru; yang tidak dikirim ulang akan dianggap lama setelah 36 jam (RAW) atau 48 jam (slab). Jangan mengarang atau mengira-ngira nilai: kosongkan kolom yang tidak ada.
> Baca `/opt/egs-raw/inbox/status/laporan-raw.txt` dan `laporan-slab.txt` setelah tiap putaran untuk melihat baris yang ditolak dan alasannya, dan perbaiki bentuk datanya. cardtell.id dan pokepedia.id tidak diterima.
