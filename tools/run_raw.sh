#!/usr/bin/env bash
# Run collect_raw.py with the API key from /opt/egs-raw/env and the collector's own Python venv.
# Usage (as the bot user):  tools/run_raw.sh [collect_raw.py options]
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
. "${EGS_ENV:-/opt/egs-raw/env}"
set +a
exec "${PYTHON:-/opt/egs-raw/venv/bin/python}" tools/collect_raw.py --db "${EGS_DB:-/opt/egs-raw/raw.sqlite}" "$@"
