#!/usr/bin/env bash
# Run collect_raw.py with the API key from /opt/egs-raw/env (if present) and the collector's own Python venv
# (if present; otherwise python3, which is enough for --lang-mode text and --dry-run).
# Usage (as the bot user):  tools/run_raw.sh [collect_raw.py options]
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="${EGS_ENV:-/opt/egs-raw/env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
PY="${PYTHON:-/opt/egs-raw/venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" tools/collect_raw.py --db "${EGS_DB:-/opt/egs-raw/raw.sqlite}" "$@"
