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
      "listings": [
        {"url": "https://toco.id/id/listing/...", "ask_idr": 47000, "city": "Kota Jambi",
         "condition": "nm", "observed_at": "2026-10-02T22:30:00+00:00",
         "conf": "nomor", "title": "Mega Venusaur ex SAR 173/126 MA1 ID NM"}
      ]
    }
  ],
  "unmatched": {"count": 310, "unknown_language": 120}
}
```

Aturan field:

- `stats` dihitung dari semua listing yang dihitung, bukan hanya yang tercantum di `listings`. `n` boleh lebih besar dari jumlah `listings`.
- `listings` memuat paling banyak 30 listing yang dihitung, yang termurah. Hanya listing yang dihitung yang dipublikasikan, jadi `conf` hanya `nomor` atau `nama+set`.
- Kuantil memakai interpolasi linear, dibulatkan ke rupiah. Urutannya `min <= p25 <= median <= p75 <= max`.
- `unmatched.count` adalah listing RAW yang tidak cocok dengan kartu mana pun. `unknown_language` adalah listing tanpa penanda bahasa, yang tidak dihitung.
- `observed_at` di tiap listing adalah waktu listing itu diambil. Semua waktu ISO dengan offset `+00:00`, seperti `latest.json`.

## Listing yang dihitung

Sebuah listing masuk statistik kalau semua ini benar:

1. `classify_title` (`tools/classify.py`) menjawab `kind = raw`. Slab, produk segel (booster, box, deck, sleeve, binder), dan kartu non-Pokémon tidak dihitung.
2. Bahasa tertulis jelas: `ID`, `[ID]`, `Indo`, atau `Indonesia` di judul. Judul tanpa penanda bahasa masuk `unknown_language`, dan judul berbahasa lain (Jepang, Inggris, dan sebagainya) tidak dihitung.
3. Cocok dengan kartu: nomor kartu persis (`nomor`), atau nama dan kode set cocok tanpa rarity yang bertentangan (`nama+set`). Aturannya sama dengan `match_type` di `tools/build_site.py`.
4. Stok ada (judul diawali `Sisa N` dengan N lebih dari 0) dan harga lebih dari 0.

Kalau ada dua harga di judul (harga coret), yang dipakai harga yang lebih rendah. Itu harga yang diminta penjual.

## Alur pengumpul (dijalankan di VPS)

1. Ambil daftar kartu dari `latest.json`, lalu buat kata kunci per kartu dengan `python3 tools/watch_queries.py latest.json`. Daftar `raw` adalah kata kunci untuk dataset ini.
2. Jalankan tiap kata kunci di pencarian toco, halaman demi halaman, sampai halaman tidak lagi memberi listing baru atau sampai batas halaman. Beri jeda antar permintaan dan patuhi aturan situs.
3. Gabungkan hasil antar kata kunci lewat URL listing, supaya tidak dihitung dua kali.
4. Klasifikasi tiap judul, cocokkan ke kartu, dan hitung statistik.
5. Tulis `raw.json`, jalankan `python3 tools/validate_raw.py raw.json`, dan publikasikan hanya kalau 0 galat.

## Skala

Listing RAW jauh lebih banyak daripada slab. Simpan detail lengkap (semua listing, riwayat) di VPS, misalnya SQLite. Repo hanya menerima ringkasan di atas. Menulis ulang ribuan listing empat kali sehari akan membuat riwayat git membengkak dan membuat tiap commit sulit dibaca.

## Publikasi

Commit `raw.json` ke `main` seperti bot EGS sekarang. Lakukan `git pull --rebase` sebelum push, karena `main` juga menerima commit dari pihak lain. Bot EGS sudah terbukti aman terhadap commit dari luar (cek 2 Okt 2026). Jangan mengubah `latest.json` dalam commit yang sama.

## Tampilan

```
python3 tools/build_site.py latest.json --raw raw.json --out board.html
```

Tiap kartu menampilkan batang RAW (rentang, kotak untuk tengah 50%, tanda median) di atas baris slab, plus rasio "N× RAW" per grade. Kartu RAW yang belum punya slab di daftar pantau muncul di tabel sendiri.

## Belum diketahui

- Struktur halaman dan cara paginasi pencarian toco. Dari `latest.json` hanya terlihat bahwa tiap hasil punya URL listing dan teks `Sisa N <judul> Rp<harga> [Rp<coret> -<diskon>%] <kota>`.
- Pengaruh kondisi (NM, LP, dan sebagainya) terhadap harga. `condition` dicatat kalau tertulis, tetapi statistik belum memisahkannya.
- Nomor seri. Untuk RAW tidak ada, jadi satu kartu yang dijual dua kali bisa terhitung dua.
