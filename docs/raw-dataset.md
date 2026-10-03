# Dataset RAW (kartu belum di-slab)

Dataset kedua di samping EGS. Isinya harga penawaran kartu Pokémon bahasa Indonesia yang belum di-slab, per kartu. Tujuannya satu papan harga yang menunjukkan RAW berapa dan slab EGS berapa untuk kartu yang sama.

Harga penawaran dipakai sebagai harga. Tidak ada yang menunggu terjual. `price_type` tetap `OBSERVED_ASKING_ONLY`.

## Dua dataset

| | EGS (slab) | RAW |
|---|---|---|
| File di repo | `latest.json` (sudah ada, tidak diubah) | `raw.json` (baru) |
| Isi | satu listing yang dipantau per kartu dan grade, riwayat harian | statistik per kartu dari banyak listing, plus contoh listing termurah |
| Bahasa | semua | hanya Indonesia (`language: "ID"`) |
| Pembuat | bot di VPS | pengumpul RAW di VPS (belum ada) |

`latest.json` tidak boleh berubah bentuk, karena report yang sudah berjalan membacanya.

## Kunci gabung

Kartu EGS dan RAW digabung lewat `(set_code, card_number)`, huruf kecil dan tanpa spasi. Contoh: `("ma3", "234/193")`. Tidak memakai nama, karena penjual menulis nama dengan banyak cara.

## Format `raw.json`

```json
{
  "schema_version": 1,
  "kind": "RAW",
  "price_type": "OBSERVED_ASKING_ONLY",
  "language": "ID",
  "source": "toco_public",
  "generated_at": "2026-10-03T00:10:00+00:00",
  "coverage": {"cards": 1, "listings_counted": 41, "last_observed_at": "2026-10-02T22:30:00+00:00"},
  "cards": [
    {
      "name": "Mega Venusaur ex", "set_code": "MA1", "card_number": "173/126", "rarity": "SAR",
      "stats": {"n": 41, "min": 47000, "p25": 70000, "median": 92000, "p75": 130000, "max": 255000},
      "history": [
        {"date_wib": "2026-10-02", "n": 38, "median": 90000, "min": 47000},
        {"date_wib": "2026-10-03", "n": 41, "median": 92000, "min": 47000}
      ],
      "listings": [
        {"url": "https://toco.id/id/listing/...", "ask_idr": 47000, "city": "Kota Jambi",
         "condition": "nm", "observed_at": "2026-10-02T22:30:00+00:00",
         "conf": "nomor", "lang": "ID", "lang_by": "image",
         "photo": "https://d13eru2cpf9i9v.cloudfront.net/toco/....webp",
         "title": "Mega Venusaur ex SAR 173/126 MA1 ID NM"}
      ]
    }
  ],
  "unmatched": {"count": 310, "language_unverified": 120}
}
```

Aturan field:

- `stats` dihitung dari semua listing yang dihitung, bukan hanya yang tercantum di `listings`. `n` boleh lebih besar dari jumlah `listings`.
- `listings` memuat paling banyak 30 listing yang dihitung, yang termurah. Hanya listing yang dihitung yang dipublikasikan, jadi `conf` hanya `nomor` atau `nama+set`.
- Kuantil memakai interpolasi linear, dibulatkan ke rupiah. Urutannya `min <= p25 <= median <= p75 <= max`.
- `unmatched.count` adalah listing RAW yang tidak cocok dengan kartu mana pun. `language_unverified` adalah listing yang cocok dengan kartu tetapi bahasanya belum terbukti Indonesia dari gambar (belum diperiksa, gambar gagal diambil, atau modelnya menjawab "tidak jelas"). Keduanya tidak dihitung.
- `history` adalah riwayat median per kartu: satu titik per hari WIB (putaran terakhir hari itu yang dipakai), paling banyak 60 titik, yang tertua di depan. Pengumpul membangunnya dari database lokalnya di VPS, dan papan harga memakainya untuk tren naik-turun dan grafik batang kecil. Putaran yang dibatasi anggaran foto mencatat `n` yang lebih kecil untuk hari itu, jadi baca `n` bersama `median`.
- `photo` (opsional) adalah alamat foto penuh listing (sekitar 900 piksel) untuk ditampilkan di website. Repo hanya menyimpan alamatnya, bukan fotonya. Foto adalah milik penjual: tampilkan untuk keperluan internal, dan minta izin atau pakai foto sendiri sebelum dipublikasikan.
- `lang` pada listing yang dipublikasikan selalu `ID`. `lang_by` adalah `image` (dicek dari foto kartu, cara utama) atau `title` (penanda eksplisit di judul, hanya boleh kalau gambar tidak tersedia).
- `observed_at` di tiap listing adalah waktu listing itu diambil. Semua waktu ISO dengan offset `+00:00`, seperti `latest.json`.

## Listing yang dihitung

Sebuah listing masuk statistik kalau semua ini benar:

1. `classify_title` (`tools/classify.py`) menjawab `kind = raw`. Slab, produk segel (booster, box, deck, sleeve, binder), dan kartu non-Pokémon tidak dihitung.
2. Bahasa kartu Indonesia, dan itu **dipastikan dari foto listing**, bukan dari judul. Penjual sering tidak menulis bahasa di judul, atau salah menulisnya, sedangkan teks pada kartu tidak bisa berbohong. Kartu berbahasa Jepang, Inggris, atau lainnya tidak dihitung. Lihat bagian "Cek bahasa dari gambar".
3. Cocok dengan kartu: nomor kartu persis (`nomor`), atau nama dan kode set cocok tanpa rarity yang bertentangan (`nama+set`). Aturannya sama dengan `match_type` di `tools/build_site.py`.
4. Stok ada (`stock` lebih dari 0, dan listing tidak diarsipkan atau dihapus) dan harga lebih dari 0. Harga yang dipakai adalah `price_special` kalau ada dan lebih rendah dari `price`, yaitu harga setelah diskon.

Kalau ada dua harga di judul (harga coret), yang dipakai harga yang lebih rendah. Itu harga yang diminta penjual.

## Cek bahasa dari gambar

Untuk tiap listing yang cocok dengan kartu tetapi belum punya hasil bahasa:

0. Saringan gratis dulu: kalau judul atau deskripsi jelas menyebut bahasa asing (Jepang, Inggris, Mandarin, atau nama set Jepang seperti "Mega Dream" dan "M2a") dan tidak menyebut Indonesia, listing itu langsung dibuang tanpa memeriksa foto.
1. Untuk sisanya, ambil foto utama listing (`primary_photo`, ukuran penuh, sekitar 900 piksel). Hanya satu foto, supaya biaya tetap kecil.
2. Tentukan bahasa teks pada kartu: `ID`, `JP`, `EN`, `lain`, atau `tidak jelas`. Jawaban `tidak jelas` dipakai untuk foto buram, pantulan cahaya, atau sisi belakang kartu.
3. Hanya `ID` yang dihitung. `tidak jelas` masuk `language_unverified`, dan bahasa lain dibuang.
4. Simpan hasilnya per URL listing, bersama URL fotonya, supaya tiap listing dicek satu kali. Cek ulang hanya kalau URL foto berubah.

Cara membaca bahasa:

- **Model vision (disarankan).** Kirim foto dengan pertanyaan tetap: "Apa bahasa teks pada kartu Pokémon ini? Jawab hanya: ID, JP, EN, lain, atau tidak jelas." Jawabannya satu kata, mudah diperiksa, dan tahan terhadap foto yang miring. Perlu kunci API dan ada biaya per foto, tetapi karena hasilnya disimpan per listing, biayanya sekali per listing.
- **OCR** (misalnya tesseract dengan bahasa `ind`, `jpn`, `eng`). Gratis, tetapi rapuh pada foto dari ponsel, pantulan cahaya, dan kartu di dalam sleeve. Huruf kana atau kanji berarti JP. Membedakan ID dari EN bergantung pada kata khas bahasa Indonesia pada kartu.

Sebelum dipakai ke seluruh data, uji pada sekitar 20 foto yang bahasanya sudah Anda ketahui. Yang harus hampir nol adalah kartu JP atau EN yang terbaca ID, karena salah arah itu yang merusak harga. Salah arah sebaliknya (ID terbaca `tidak jelas`) hanya mengurangi jumlah data.

Penanda bahasa di judul (`ID`, `Indo`, `Indonesia`) tetap terbaca oleh `tools/classify.py`, tetapi hanya sebagai petunjuk untuk mengurutkan listing mana yang dicek dulu. Judul tidak menggantikan gambar. `lang_by: "title"` hanya dipakai kalau foto tidak bisa diambil.

## Alur pengumpul (dijalankan di VPS)

Implementasi referensi ada di `tools/collect_raw.py`, dan langkah menjalankannya di VPS ada di [vps-setup.md](vps-setup.md). Uraian di bawah menjelaskan apa yang dilakukannya.

1. Ambil daftar kartu dari `latest.json` dan buat kata kunci per kartu (`tools/watch_queries.py`, daftar `raw`).
2. Panggil pencarian toco sendiri: `POST https://toco.id/api/toco-api/search` dengan `path: "/listing-search"`, 45 listing per permintaan, halaman berikutnya dengan `from` + 45 (diperiksa langsung di VPS: hasilnya sama dengan di browser, tanpa browser). Beri jeda antar permintaan.
3. Gabungkan hasil antar kata kunci lewat `slug` listing, supaya tidak dihitung dua kali. Pencarian toco mengurutkan menurut relevansi dan mencampur Jepang, Inggris, Indonesia, slab, dan RAW, jadi penyaringan berikutnya wajib.
4. Klasifikasi tiap judul (`kind = raw`), lalu cocokkan ke kartu. Nomor kartu yang berbeda berarti kartu berbeda ("003/126" bukan "173/126").
5. Cek bahasa: saringan gratis dari teks, lalu foto untuk sisanya (bagian di atas). Hasil disimpan per listing supaya tidak diulang.
6. Hitung statistik dari listing yang terbukti Indonesia.
7. Tulis `raw.json`, jalankan `python3 tools/validate_raw.py raw.json`, dan publikasikan hanya kalau 0 galat.

## Skala

Listing RAW jauh lebih banyak daripada slab. Simpan detail lengkap (semua listing, riwayat) di VPS, misalnya SQLite. Repo hanya menerima ringkasan di atas. Menulis ulang ribuan listing empat kali sehari akan membuat riwayat git membengkak dan membuat tiap commit sulit dibaca.

## Publikasi

Commit `raw.json` ke `main` seperti bot EGS sekarang. Lakukan `git pull --rebase` sebelum push, karena `main` juga menerima commit dari pihak lain. Bot EGS sudah terbukti aman terhadap commit dari luar (cek 2 Okt 2026). Jangan mengubah `latest.json` dalam commit yang sama.

## Tampilan

```
python3 tools/build_site.py latest.json --raw raw.json --out board.html
```

Tiap kartu menampilkan batang RAW (rentang, kotak untuk tengah 50%, tanda median) di atas baris slab, plus rasio "N× RAW" per grade. Kartu RAW yang belum punya slab di daftar pantau muncul di tabel sendiri.

## Sudah diperiksa di VPS (3 Okt 2026)

- Halaman pencarian toco dirender dengan JavaScript, tetapi datanya diambil lewat `POST /api/toco-api/search` yang bisa dipanggil langsung. Setiap listing punya `title`, `slug`, `stock`, `price`, `price_special`, `description`, `city`, `created_at`, `photos`, dan `primary_photo`.
- Foto asli (`primary_photo`, cloudfront) berukuran sekitar 900 piksel dan bisa diambil tanpa tanda tangan. Foto kecil 184 piksel di halaman pencarian tidak dipakai.
- Deskripsi sering menyebut bahasa ("CHINESE", "bahasa jepang"), jadi dipakai sebagai saringan awal.
- Teks halaman listing slab tidak memuat kata "serial". Nomor seri slab kemungkinan hanya ada di foto label.

## Belum diketahui

- Pengaruh kondisi (NM, LP, dan sebagainya) terhadap harga. `condition` dicatat kalau tertulis di judul, tetapi statistik belum memisahkannya.
- Akurasi pembaca bahasa dari foto pada data nyata. Diuji lewat `--sample-check` sebelum dipakai (lihat vps-setup.md).
- Nomor seri. Untuk RAW tidak ada, jadi satu kartu yang dijual dua kali bisa terhitung dua.
