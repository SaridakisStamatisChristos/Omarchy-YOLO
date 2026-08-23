#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python -m omarchy_yolo --version
git --version

agents=()
for name in codex claude opencode; do
  if command -v "$name" >/dev/null 2>&1; then
    agents+=("$name")
  fi
done
if ((${#agents[@]} == 0)); then
  echo "live smoke requires at least one authenticated coding-agent CLI" >&2
  exit 1
fi
printf 'available live agents: %s\n' "${agents[*]}"

if command -v omarchy-shell >/dev/null 2>&1; then
  echo "omarchy-shell: $(command -v omarchy-shell)"
else
  echo "omarchy-shell not present; agent runtime smoke will still run"
fi

# Doctor treats missing optional shell UI/daemon as advisory while enforcing the
# Linux/non-root/Git/agent requirements needed by the control plane.
python -m omarchy_yolo doctor

echo "live smoke passed"
