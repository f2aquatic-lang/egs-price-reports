#!/usr/bin/env bash
# Collect RAW listings and publish raw.json to main. Meant for the VPS timer (docs/vps-setup.md).
# Only runs on branch main, so it can never push unmerged work. Extra arguments go to collect_raw.py.
#
# GitHub access: the bot's own deploy key (EGS_GIT_KEY, default /opt/egs-price/.ssh/egs_report) is used with
# IdentitiesOnly, because the user has no default SSH key. Set GIT_SSH_COMMAND yourself to override.
set -euo pipefail

# Everything lives in one function that is read completely before it runs, because the git commands below can
# replace this very file (a newer publish_raw.sh arriving from main) while bash is still executing it.
main() {
  cd "$(dirname "$0")/.."
  PYTHON="${PYTHON:-python3}"
  export PYTHONDONTWRITEBYTECODE=1  # no tools/__pycache__ in the checkout

  # a rebase that died half-way (no identity, lost race) leaves HEAD detached: clear it before looking at the branch
  git rebase --abort 2>/dev/null || true
  if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
    echo "Berhenti: folder ini tidak di branch main. Penerbitan hanya dari main." >&2
    exit 1
  fi

  KEY="${EGS_GIT_KEY:-/opt/egs-price/.ssh/egs_report}"
  if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -f "$KEY" ]; then
    export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=${EGS_KNOWN_HOSTS:-$(dirname "$KEY")/known_hosts}"
  fi

  # Every git command here may create a commit (a rebase replays ours onto the bot's newer one), and the
  # service user has no git identity of its own.
  export GIT_AUTHOR_NAME="${EGS_GIT_NAME:-EGS RAW collector}" GIT_COMMITTER_NAME="${EGS_GIT_NAME:-EGS RAW collector}"
  export GIT_AUTHOR_EMAIL="${EGS_GIT_EMAIL:-egs-raw@users.noreply.github.com}" GIT_COMMITTER_EMAIL="${EGS_GIT_EMAIL:-egs-raw@users.noreply.github.com}"

  # This checkout only publishes raw.json. A failed earlier run can leave an unpushed commit or a half-done rebase:
  # start from origin/main again, but refuse to throw away anything other than raw.json.
  git fetch -q origin main
  foreign="$( { git diff --name-only origin/main...HEAD; git status --porcelain --untracked-files=no | cut -c4-; } | grep -vx -e raw.json -e catalog.json -e jp_prices.json -e rates.json -e egs_watch.json -e card_marks.json || true)"
  if [ -n "$foreign" ]; then
    echo "Berhenti: ada perubahan lokal selain raw.json di folder ini:" >&2
    echo "$foreign" >&2
    exit 1
  fi
  git reset -q --hard origin/main

  # the official card catalog (tools/fetch_catalog.py) is fetched separately and changes rarely; publish it with raw.json
  CATALOG="${EGS_CATALOG:-/opt/egs-raw/catalog.json}"
  if [ -f "$CATALOG" ] && ! cmp -s "$CATALOG" catalog.json; then
    cp "$CATALOG" catalog.json
  fi
  # the Japanese reference prices (tools/fetch_jp_prices.py) are fetched separately too and published the same way
  JP_PRICES="${EGS_JP_PRICES:-/opt/egs-raw/jp_prices.json}"
  # refreshed once a day (about 70 seconds, one request per set every 2 seconds); a failure keeps the old file and never stops the run
  "$PYTHON" tools/fetch_jp_prices.py --out "$JP_PRICES" --max-age-hours 20 || echo "PERINGATAN: harga Jepang tidak diperbarui (harga lama dipakai)" >&2
  if [ -f "$JP_PRICES" ] && ! cmp -s "$JP_PRICES" jp_prices.json; then
    cp "$JP_PRICES" jp_prices.json
  fi
  # the EGS slab watch (tools/egs_watch.py, its own timer and database) writes egs_watch.json; it is published here with the rest
  EGS_WATCH="${EGS_WATCH_JSON:-/opt/egs-raw/egs_watch.json}"
  if [ -f "$EGS_WATCH" ] && ! cmp -s "$EGS_WATCH" egs_watch.json; then
    cp "$EGS_WATCH" egs_watch.json
  fi
  # the rarity printed on cards the official site lists without one, and the reprint flags (tools/read_card_marks.py, kept in /opt/egs-raw: this checkout is reset every run)
  MARKS="${EGS_CARD_MARKS:-/opt/egs-raw/card_marks.json}"
  if [ -f "$MARKS" ] && ! cmp -s "$MARKS" card_marks.json; then
    cp "$MARKS" card_marks.json
  fi
  # today's yen and dollar rates (tools/fetch_rates.py): refreshed when older than 20 hours; a failure keeps the old file and never stops the run
  RATES="${EGS_RATES:-/opt/egs-raw/rates.json}"
  "$PYTHON" tools/fetch_rates.py --out "$RATES" --max-age-hours 20 || echo "PERINGATAN: kurs tidak diperbarui (kurs lama dipakai)" >&2
  if [ -f "$RATES" ] && ! cmp -s "$RATES" rates.json; then
    cp "$RATES" rates.json
  fi
  "$PYTHON" tools/collect_raw.py "$@"

  # Optional website: rebuild the page from the checkout (latest.json from the bot, raw.json from this run).
  # A failure here must never stop raw.json from being published.
  build_site() {
    [ -n "${EGS_SITE_DIR:-}" ] || return 0
    local catalog_arg=""
    [ -f catalog.json ] && catalog_arg="--catalog catalog.json"
    [ -f jp_prices.json ] && catalog_arg="$catalog_arg --jp jp_prices.json"
    [ -f rates.json ] && catalog_arg="$catalog_arg --rates rates.json"
    [ -f egs_watch.json ] && catalog_arg="$catalog_arg --egs egs_watch.json"
    [ -f card_marks.json ] && catalog_arg="$catalog_arg --marks card_marks.json"
    if "$PYTHON" tools/build_site.py latest.json --raw raw.json $catalog_arg --standalone --out "$EGS_SITE_DIR/.index.html.new" \
        && chmod 644 "$EGS_SITE_DIR/.index.html.new" && mv -f "$EGS_SITE_DIR/.index.html.new" "$EGS_SITE_DIR/index.html"; then
      # robots.txt is static and setup_site.sh creates it (as root): write it only when it is missing, and never fail the run over it
      [ -e "$EGS_SITE_DIR/robots.txt" ] || printf 'User-agent: *\nDisallow: /\n' > "$EGS_SITE_DIR/robots.txt" \
        || echo "PERINGATAN: robots.txt tidak bisa ditulis di $EGS_SITE_DIR" >&2
      echo "situs diperbarui: $EGS_SITE_DIR/index.html"
    else
      echo "PERINGATAN: situs gagal dibangun; penerbitan raw.json tidak terpengaruh" >&2
    fi
  }

  if [ -z "$(git status --porcelain -- raw.json catalog.json jp_prices.json rates.json egs_watch.json card_marks.json)" ]; then
    echo "raw.json tidak berubah"
    build_site
    exit 0
  fi
  git add raw.json
  [ -f catalog.json ] && git add catalog.json
  [ -f jp_prices.json ] && git add jp_prices.json
  [ -f rates.json ] && git add rates.json
  [ -f egs_watch.json ] && git add egs_watch.json
  [ -f card_marks.json ] && git add card_marks.json
  git commit -q -m "Update RAW asking report"
  # the bot pushes latest.json to the same branch: on a lost race, take its commit and push again
  for attempt in 1 2 3; do
    git pull --rebase -q origin main
    if git push -q origin HEAD:main; then
      echo "raw.json dipublikasikan"
      build_site
      exit 0
    fi
    echo "dorongan ke-$attempt bentrok dengan push lain, mencoba lagi" >&2
    sleep $((attempt * 5))
  done
  echo "Gagal mendorong raw.json setelah 3 percobaan." >&2
  exit 1
}
main "$@"; exit $?
