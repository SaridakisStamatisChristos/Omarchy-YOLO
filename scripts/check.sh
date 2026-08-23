#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python -m compileall -q src tests
python -m json.tool shell-plugin/manifest.json >/dev/null
bash -n install.sh uninstall.sh scripts/dev-run.sh scripts/check.sh
pytest -q

echo "all checks passed"
