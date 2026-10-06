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

Foto dibaca (company, grade, nomor sertifikat, nomor kartu, rarity, bahasa) hanya dengan `--photo-mode openai|anthropic`: **bawaan mati**, karena butuh saldo API sendiri. Uji dulu dengan satu foto: `egs_watch.py --photo-mode openai --read-photo <alamat foto>` (kunci di `OPENAI_API_KEY`; nama model dengan `--photo-model`). **Dua model membaca foto yang sama dan hanya yang disepakati keduanya dipakai** (bawaan openai: `gpt-4.1,gpt-4o`; pilih lain dengan `--photo-model a,b`). Percobaan pertama pada satu foto slab memberi tiga model tiga jawaban berbeda untuk nomor kartu (330/193, 234/193, 22/100) dan nomor sertifikat; bacaan satu model karena itu tidak pernah dijadikan dasar nomor. Kalau pembacanya tidak sepakat, nomor dari foto dibuang dan listing tetap di antrean dengan alasan `photo_unsure`. Dua model berarti dua panggilan per foto (batas `--photo-budget` menghitung listing, bukan panggilan). Foto boleh melengkapi teks atau membantah; tidak pernah menimpa konflik. Kalau teks menulis Indonesia tetapi satu bacaan foto menyebut EN/JP, listing masuk antrean dengan alasan `language_conflict` (bukan ditolak): satu bacaan model tidak cukup untuk membuang listing, dan nomor yang terbaca pun bisa keliru (percobaan pertama: nomor 330/193, yang tidak ada di set Indonesia mana pun). Pencocokan ilustrasi dengan katalog **belum** dibuat: tanpa nomor, tanpa foto yang terbaca, dan tanpa kecocokan tunggal di katalog, listing tetap di antrean.

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

### Listing Shopee/Tokopedia yang Anda kumpulkan sendiri (`--import`)

Akun Shopee atau Tokopedia tidak mengubah apa yang boleh dibaca bot: API penjual (Shopee Open Platform, Tokopedia/TikTok Shop Partner) hanya membuka data **toko Anda sendiri**, bukan listing penjual lain, dan akun yang dipakai bot untuk menelusuri halaman orang lain melanggar syarat kedua situs serta berisiko membuat akunnya diblokir. Karena itu bot **tidak membuka, masuk ke, atau menelusuri** Shopee dan Tokopedia. Yang tersedia adalah jalur impor: Anda melihat listing di aplikasi/situs, menyalin isinya ke satu berkas, dan berkas itu melewati pemeriksaan identitas, dedupe, dan statistik yang sama dengan listing toco.

Format CSV (baris pertama = nama kolom; pemisah koma, titik koma, atau tab; atau JSON berupa daftar objek dengan kunci yang sama). Kolom wajib: `url`, `judul`, `harga`. Kolom lain boleh kosong: `toko`, `foto` (alamat gambar: tekan lama pada foto lalu "salin alamat gambar"), `stok` (bawaan 1; 0 = habis), `deskripsi`, `kota`.

```
url,judul,harga,toko,foto,stok
https://shopee.co.id/Pikachu-ex-SAR-i.111.222,Pikachu ex SAR 234/193 MA3 ID EGS 10,Rp2.450.000,Toko A,https://down-id.example/a.jpg,1
https://www.tokopedia.com/tokob/pikachu-ex-egs-10,Pikachu ex SAR 234/193 MA3 ID EGS 10,2400000,Toko B,,1
```

```bash
sudo -u egsprice -H python3 /opt/egs-raw/main/tools/egs_watch.py --import /opt/egs-raw/impor.csv --no-search \
  --db /opt/egs-raw/egs_watch.sqlite --out /opt/egs-raw/egs_watch.json --latest /opt/egs-raw/main/latest.json --catalog /opt/egs-raw/main/catalog.json
```

Aturannya:

- Alamat dibersihkan dari parameter pelacak (`?sp_atk=...`, `#...`); halaman yang sama dua kali menjadi satu listing, dan impor ulang dengan harga baru menambah riwayat harga.
- Baris dengan harga bukan angka rupiah, judul kosong, atau alamat bukan http(s) **dilaporkan dan dilewati**, tidak ditebak. Harga "1,250,000" (koma) ditolak karena bisa berarti desimal.
- Setiap baris dinilai seperti listing toco (EGS, grade, bahasa Indonesia, set, nomor, rarity); yang meragukan masuk antrean ambigu dan tidak masuk harga. Dengan `--photo-mode` dan kolom `foto`, nomor/label dibaca dari foto.
- Bot tidak pernah mengambil ulang alamat Shopee/Tokopedia. Karena itu baris impor **berhenti dihitung 48 jam setelah terakhir diimpor** (ditandai `stale`, tetap tampil, tidak masuk harga mana pun) kecuali Anda mengimpornya lagi. Ini sama seperti listing toco yang tidak diperiksa dua hari.
- Di `egs_watch.json` tiap listing membawa `source` (`toco`, `shopee`, `tokopedia`) dan `origin: "import"`; `summary.by_source` menghitung per sumber, dan laporan menampilkan "impor manual". Facebook dan WhatsApp diperlakukan sama: hanya lewat berkas yang Anda isi, tidak pernah diambil bot.

### Lembar kerja: foto kartu dengan kolom kosong di sebelahnya (`--make-worksheet`, `--worksheet`)

Mengisi CSV `--import` dari nol itu berat. Lembar kerja membalik pekerjaannya: bot menyiapkan fotonya, Anda hanya mengetik di kolom di sebelahnya. Bagian yang tidak bisa dibaca bot (nomor di label slab yang buram, bahasa kartu, harga di Tokopedia/Shopee) Anda isi sendiri, dan hanya itu.

Dua jenis baris dalam satu berkas:

| jenis | isinya | yang Anda isi |
|---|---|---|
| `antrean` | listing yang belum bisa diputuskan bot: foto listing, judul, harga, alasan ragu | `nomor` (lengkap, mis. 234/193), `rarity`, `bahasa` (ID/EN/JP), `grade`; atau `tolak` di `keputusan` untuk membuangnya |
| `pasar` | kartu dan grade yang sudah dihitung dari toco: gambar kartu resmi, harga median toco | `bahasa` dan harga plus link listing Tokopedia dan/atau Shopee yang Anda lihat sendiri |

```bash
# 1. buat lembar dari hasil terakhir (tidak mengubah apa pun selain berkas CSV ini)
sudo -u egsprice -H python3 /opt/egs-raw/main/tools/egs_watch.py --make-worksheet /opt/egs-raw/lembar.csv \
  --out /opt/egs-raw/egs_watch.json --catalog /opt/egs-raw/main/catalog.json
```

Buka di Google Sheets (File → Import → Upload, "Replace spreadsheet"). Kolom `foto` berisi rumus `=IMAGE(...)` sehingga fotonya tampil; besarkan tinggi baris (pilih baris, klik kanan, ubah ukuran baris ke 90 piksel). Jangan ubah kolom `kunci`. Isi kolom di sebelah foto.

Mengirim hasil ke VPS: simpan lagi sebagai CSV (File → Download → CSV) lalu tempel isinya ke `/opt/egs-raw/lembar_terisi.csv` (`sudo nano`), atau terbitkan lembar ke web (File → Share → Publish to web → pilih lembar → CSV) dan pakai alamat yang diberikan. Alamat itu bisa dibuka siapa saja yang memegangnya (isinya hanya judul, harga, dan alamat listing publik).

```bash
# 2. terapkan hasil isian (berkas atau alamat web), tanpa pencarian toco
sudo -u egsprice -H /opt/egs-raw/venv/bin/python /opt/egs-raw/main/tools/egs_watch.py --worksheet /opt/egs-raw/lembar_terisi.csv --no-search \
  --db /opt/egs-raw/egs_watch.sqlite --out /opt/egs-raw/egs_watch.json --latest /opt/egs-raw/main/latest.json --catalog /opt/egs-raw/main/catalog.json
```

Aturannya:

- Baris `antrean`: isian Anda dicatat seperti bacaan foto tetapi ditandai **manual** ("nomor diisi manual dari foto") dan listingnya dinilai ulang. Hasilnya bisa diterima, tetap ragu (misalnya rarity bertentangan dengan katalog), atau ditolak karena bahasa EN/JP. Tidak ada jawaban yang dipaksakan lolos: aturan identitas yang sama berlaku. `tolak` membuang listing selamanya; putaran pencarian berikutnya tidak memasukkannya lagi ke antrean.
- Baris `pasar`: harga dicatat sebagai listing impor (`source` shopee/tokopedia, `origin: import`) untuk kartu dan grade pada baris itu, dengan bahasa yang Anda nyatakan. Bahasa selain ID dilewati dan dilaporkan; harga tanpa link (atau sebaliknya) dilaporkan dan dilewati; bahasa yang kosong ditolak.
- Nilai yang tidak dikenali (nomor "abc", grade "11") membuat **seluruh baris** dilewati dan dilaporkan, tidak diterapkan separuh.
- Jalankan perintah ini **setelah mengisi**, jangan dipasang di timer: tiap penerapan dianggap "baru dilihat sekarang" untuk baris pasar, sehingga timer akan membuat harga lama tampak segar. Seperti impor biasa, baris pasar berhenti dihitung 48 jam setelah penerapan terakhir.
- Membuat lembar baru menimpa berkas CSV-nya, bukan lembar Google Anda. Terapkan isian dulu, baru buat lembar baru.

### Sumber lain yang dipertimbangkan (Cardtell, Pokepedia, Carousell)

`tools/probe_marketplaces.py --skip-marketplaces --candidates` menguji ketiganya dari VPS, hanya membaca: robots.txt, satu halaman publik, dan tautan syarat dan ketentuan yang ditemukan (dicetak supaya dibaca orang sebelum apa pun dibangun). Untuk sumber-sumber ini probe **mematuhi robots.txt**: halaman yang dilarang untuk semua bot tidak dibaca sama sekali (verdict `robots`). Jalur halaman Pokepedia (`/marketplace`) hanyalah tebakan; ganti dengan `--site "Pokepedia=https://pokepedia.id|/jalur?q={q}"`. Satu situs lain: `--site "NAMA=https://alamat|/jalur"`.

Hasil `ok` berarti halamannya terbaca; itu **bukan izin** menyalin datanya. Cardtell memuat harga yang dikirim anggotanya sendiri; menarik semuanya ke papan lain adalah keputusan pemilik RadarKartu (dan sebaiknya dengan izin Cardtell), bukan sekadar urusan teknis. Tidak ada pengambil Cardtell yang dibuat sebelum robots.txt dan syaratnya dibaca dan keputusan itu diambil.

### Hasil uji akses dari VPS (Tencent, `ubuntu@VM-0-7-ubuntu`, 2026-10-06) dan apa artinya

Tiga penyebab yang berbeda sering tertukar. Yang mana yang berlaku, menentukan penanganannya:

| tempat | yang terlihat | penyebab |
|---|---|---|
| **Lingkungan Claude (bukan VPS)** | `curl: (56) CONNECT tunnel failed, response 403` ke asia.pokemon-card.com dan cloudfront; `EGRESS_BLOCKED ... blocked by the network egress proxy` untuk cardtell.id, shopee.co.id, affiliate.shopee.co.id | daftar domain yang boleh dihubungi dari lingkungan itu. Tidak berarti apa-apa tentang situsnya maupun VPS |
| **VPS, Tokopedia** | `robots.txt` status 200 (boleh dibaca); halaman pencarian: `status 0, TimeoutError: The read operation timed out`; browser tanpa kepala: `Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR` | pencarian itu sendiri dilarang robots.txt (`Disallow: /search?*`); jalur dinamisnya juga memutus sambungan sebelum jawaban HTTP. Bukan kode 403, jadi bukan bukti IP VPS ditolak seluruhnya (robots.txt dijawab normal) |
| **VPS, Shopee** | halaman pencarian status 200 tetapi 0 harga (daftar dimuat JavaScript); panggilan pencarian milik situs: `status 403`, kode `90309999`; browser tanpa kepala: 250 karakter | pertahanan anti-bot milik Shopee pada panggilan datanya, dan halaman kosong tanpa sesi browser yang penuh |

Karena penghalangnya adalah pertahanan anti-bot dan robots.txt, bukan satu IP yang diblokir, **membeli proxy atau IP rumahan bukan jawabannya**: itu berarti menyamar untuk melewati pembatasan, dan itu tidak dilakukan di proyek ini. Jalur yang tersisa untuk Shopee dan Tokopedia adalah yang diizinkan situsnya: API resmi (Shopee Affiliate Open API, bila akunnya disetujui dan syaratnya mengizinkan), data toko sendiri, atau input manual. Probe kini juga mematuhi robots.txt di kedua situs (termasuk tanda `*` dan `$`): alamat yang dilarang tidak diminta sama sekali.

Verdict `notfound` (404/410) berarti alamat yang ditebak tidak ada di situs itu, **bukan** pemblokiran: kirim alamat halaman yang benar dari browser (misalnya halaman satu kartu, dan halaman daftar yang memuatnya) dan uji lagi dengan `--site`. Untuk situs yang diprobe di bawah robots.txt, semua aturan dalam berkasnya dicetak (bukan hanya yang menyebut pencarian). SNKRDUNK pertama kali diprobe dengan `/en/trading-cards` dan dijawab 404; robots.txt-nya terbaca (200).

### Syarat penggunaan Cardtell (dibaca 2026-10-06) dan keputusannya

Teks lengkap syarat Cardtell (PT Cardtell Teknologi Nusantara) dibaca. Tidak ada kalimat yang menyebut kata scraping, tetapi:

- **Pasal 13.2:** nama, logo, tampilan antarmuka, algoritma agregasi harga, dan konten milik Cardtell "tidak boleh digunakan tanpa izin tertulis dari Cardtell". Izin tertulis adalah jalur yang mereka sendiri buka.
- **Pasal 8:** akses data mereka dijatah menurut langganan (Free: 10 pemindaian per hari dan hingga 3 data harga jual terakhir; Pro Rp29.900 per bulan: tak terbatas dan hingga 10 data). Pembaca otomatis akan melewati jatah itu, yaitu model bisnis mereka.
- **Pasal 12.2 dan 3.1:** data transaksi tampil publik tanpa identitas, dan Harga Pasar adalah agregasi hasil olahan mereka.
- **Pasal 14 (butir terakhir) dan 18:** data situs tidak boleh dipakai untuk tujuan yang merugikan pihak lain, dan data pribadi anggota (nama, tautan Facebook) tunduk pada Kebijakan Privasi mereka.
- Pasal 1 dan pembukaan: syarat ini mengikat siapa pun yang **mengakses** situs.

Keputusan: pembaca otomatis Cardtell **tidak dibangun**. Jalur yang tersisa adalah izin tertulis atau kerja sama data (API, umpan data, atau izin menampilkan Harga Pasar mereka dengan atribusi dan tautan). Robots.txt mereka hanya melarang `/api/`, tetapi robots.txt bukan izin atas isi.

