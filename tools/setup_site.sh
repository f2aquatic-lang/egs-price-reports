#!/usr/bin/env bash
# One-time setup on the VPS: serve the price board over HTTPS behind a password, with Caddy.
# Usage:  sudo bash tools/setup_site.sh [hostname]
#   hostname  default: <public-ip-with-dashes>.sslip.io (a free name that points at the IP; no domain needed)
# Environment (all optional): EGS_SITE_DIR, EGS_BOT_USER, EGS_SITE_HOST, EGS_ENV, EGS_CADDYFILE
# See docs/website.md. Nothing here touches the bots; the script refuses to run if port 80 or 443 is taken.
set -euo pipefail

SITE_DIR="${EGS_SITE_DIR:-/var/www/egs}"
BOT_USER="${EGS_BOT_USER:-egsprice}"
ENV_FILE="${EGS_ENV:-/opt/egs-raw/env}"
CADDYFILE="${EGS_CADDYFILE:-/etc/caddy/Caddyfile}"
HOST="${1:-${EGS_SITE_HOST:-}}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" != 0 ]; then
  echo "Jalankan dengan sudo: sudo bash $0" >&2
  exit 1
fi

if [ -z "$HOST" ]; then
  IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
  if ! printf '%s' "$IP" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
    echo "Tidak bisa mengetahui IP publik. Jalankan: sudo bash $0 nama.domain.anda" >&2
    exit 1
  fi
  HOST="${IP//./-}.sslip.io"
fi

# Caddy needs 80 (certificate check) and 443 (the site). Stop if another program already listens there.
for port in 80 443; do
  other="$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -v caddy || true)"
  if [ -n "$other" ]; then
    echo "Port $port sudah dipakai program lain, jadi situs tidak dipasang (agar bot tidak terganggu):" >&2
    echo "$other" >&2
    exit 1
  fi
done

as_bot() {
  if [ "$BOT_USER" = "$(id -un)" ]; then "$@"; else sudo -u "$BOT_USER" -H "$@"; fi
}

echo "Memasang Caddy..."
apt-get update -qq || true
apt-get install -y caddy >/dev/null

echo
echo "Pilih nama pengguna dan kata sandi untuk masuk ke situs."
echo "Kata sandi: minimal 12 karakter, bukan yang dipakai di tempat lain. Caddy akan menanyakannya dua kali."
read -r -p "Nama pengguna [tim]: " USER_NAME
USER_NAME="${USER_NAME:-tim}"
case "$USER_NAME" in *[!A-Za-z0-9._-]*) echo "Nama pengguna hanya boleh huruf, angka, titik, garis bawah, dan strip." >&2; exit 1;; esac
HASH="$(caddy hash-password)"
[ -n "$HASH" ] || { echo "Kata sandi kosong; dibatalkan." >&2; exit 1; }

mkdir -p "$SITE_DIR" "$(dirname "$CADDYFILE")"
chown "$BOT_USER" "$SITE_DIR"
chmod 755 "$SITE_DIR"

if [ -f "$CADDYFILE" ] && ! grep -q "EGS price board" "$CADDYFILE"; then
  cp -a "$CADDYFILE" "$CADDYFILE.bak.$(date +%Y%m%d%H%M%S)"
  echo "Caddyfile lama dicadangkan di $CADDYFILE.bak.*"
fi
cat > "$CADDYFILE" <<EOF
# EGS price board (tools/setup_site.sh)
$HOST {
	root * $SITE_DIR
	encode gzip
	header {
		X-Robots-Tag "noindex, nofollow"
		Referrer-Policy "no-referrer"
		X-Content-Type-Options "nosniff"
		Strict-Transport-Security "max-age=31536000"
	}
	basicauth {
		$USER_NAME $HASH
	}
	file_server
}
EOF
chmod 644 "$CADDYFILE"
caddy validate --config "$CADDYFILE" >/dev/null

# the publisher rebuilds the page when EGS_SITE_DIR is set in the service's environment file
touch "$ENV_FILE"
grep -q '^EGS_SITE_DIR=' "$ENV_FILE" || echo "EGS_SITE_DIR=$SITE_DIR" >> "$ENV_FILE"

# first build now, so the site is not empty until the next 6-hourly run
if [ -f "$REPO/raw.json" ] && [ -f "$REPO/latest.json" ]; then
  CATALOG_ARG=""
  [ -f "$REPO/catalog.json" ] && CATALOG_ARG="--catalog catalog.json"
  (cd "$REPO" && as_bot env PYTHONDONTWRITEBYTECODE=1 python3 tools/build_site.py latest.json --raw raw.json $CATALOG_ARG --standalone --out "$SITE_DIR/index.html") \
    && chmod 644 "$SITE_DIR/index.html" \
    && printf 'User-agent: *\nDisallow: /\n' > "$SITE_DIR/robots.txt" \
    || echo "PERINGATAN: halaman pertama gagal dibangun; akan dibangun pada putaran berikutnya."
else
  echo "raw.json belum ada di $REPO; halaman dibangun pada putaran berikutnya."
fi

systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

echo
echo "Selesai. Alamat situs:  https://$HOST/"
echo "Pengguna: $USER_NAME"
echo "Supaya bisa dibuka dari luar, buka port TCP 80 dan 443 di firewall Tencent Lighthouse (lihat docs/website.md)."
echo "Sertifikat HTTPS diurus Caddy sendiri; pertama kali butuh beberapa menit setelah port terbuka."
