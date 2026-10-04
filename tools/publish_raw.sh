#!/usr/bin/env bash
# Collect RAW listings and publish raw.json to main. Meant for the VPS timer (docs/vps-setup.md).
# Only runs on branch main, so it can never push unmerged work. Extra arguments go to collect_raw.py.
#
# GitHub access: the bot's own deploy key (EGS_GIT_KEY, default /opt/egs-price/.ssh/egs_report) is used with
# IdentitiesOnly, because the user has no default SSH key. Set GIT_SSH_COMMAND yourself to override.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
  echo "Berhenti: folder ini tidak di branch main. Penerbitan hanya dari main." >&2
  exit 1
fi

KEY="${EGS_GIT_KEY:-/opt/egs-price/.ssh/egs_report}"
if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -f "$KEY" ]; then
  export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=${EGS_KNOWN_HOSTS:-$(dirname "$KEY")/known_hosts}"
fi

git pull --rebase --autostash -q origin main
"$PYTHON" tools/collect_raw.py "$@"

if [ -z "$(git status --porcelain -- raw.json)" ]; then
  echo "raw.json tidak berubah"
  exit 0
fi
git add raw.json
git -c user.name="${EGS_GIT_NAME:-EGS RAW collector}" -c user.email="${EGS_GIT_EMAIL:-egs-raw@users.noreply.github.com}" \
  commit -q -m "Update RAW asking report"
# the bot pushes latest.json to the same branch: on a lost race, take its commit and push again
for attempt in 1 2 3; do
  git pull --rebase -q origin main
  if git push -q origin HEAD:main; then
    echo "raw.json dipublikasikan"
    exit 0
  fi
  echo "dorongan ke-$attempt bentrok dengan push lain, mencoba lagi" >&2
  sleep $((attempt * 5))
done
echo "Gagal mendorong raw.json setelah 3 percobaan." >&2
exit 1
