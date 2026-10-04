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
  git rebase --abort 2>/dev/null || true
  git fetch -q origin main
  foreign="$( { git diff --name-only origin/main...HEAD; git status --porcelain | cut -c4-; } | grep -vx raw.json || true)"
  if [ -n "$foreign" ]; then
    echo "Berhenti: ada perubahan lokal selain raw.json di folder ini:" >&2
    echo "$foreign" >&2
    exit 1
  fi
  git reset -q --hard origin/main
  "$PYTHON" tools/collect_raw.py "$@"

  if [ -z "$(git status --porcelain -- raw.json)" ]; then
    echo "raw.json tidak berubah"
    exit 0
  fi
  git add raw.json
  git commit -q -m "Update RAW asking report"
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
}
main "$@"; exit $?
