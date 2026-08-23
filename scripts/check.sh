#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python -m compileall -q src tests scripts/check_coverage.py
python -m json.tool shell-plugin/manifest.json >/dev/null
bash -n install.sh uninstall.sh scripts/dev-run.sh scripts/check.sh scripts/live-smoke.sh
python -m pytest \
  --cov=omarchy_yolo \
  --cov-branch \
  --cov-report=term-missing \
  --cov-report=json:coverage.json \
  --cov-fail-under=75
python scripts/check_coverage.py coverage.json
rm -f coverage.json

echo "all checks passed"
