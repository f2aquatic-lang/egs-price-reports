#!/usr/bin/env bash
# Collect RAW listings and publish raw.json to main. Meant for cron on the VPS (docs/vps-setup.md).
# Only runs on branch main, so it can never push unmerged work. Extra arguments go to collect_raw.py.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
  echo "Berhenti: folder ini tidak di branch main. Penerbitan hanya dari main." >&2
  exit 1
fi

git pull --rebase --autostash -q origin main
"$PYTHON" tools/collect_raw.py "$@"

if [ -z "$(git status --porcelain -- raw.json)" ]; then
  echo "raw.json tidak berubah"
  exit 0
fi
git add raw.json
git commit -q -m "Update RAW asking report"
git pull --rebase -q origin main
git push -q origin HEAD:main
echo "raw.json dipublikasikan"
