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
agent="${agents[0]}"
printf 'selected live agent: %s\n' "$agent"

if command -v omarchy-shell >/dev/null 2>&1; then
  omarchy-shell -q shell ping >/dev/null
  echo "omarchy-shell IPC ping passed"
else
  echo "omarchy-shell not present; running the full agent control-plane smoke only"
fi

sandbox_root="$(mktemp -d -t omarchy-yolo-live.XXXXXX)"
daemon_pid=""
cleanup() {
  if [[ -n "$daemon_pid" ]]; then
    kill "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
  fi
  rm -rf -- "$sandbox_root"
}
trap cleanup EXIT INT TERM

repo="$sandbox_root/repo"
state="$sandbox_root/state"
runtime="$sandbox_root/runtime"
config="$sandbox_root/config.toml"
mkdir -p "$repo" "$state" "$runtime"
chmod 700 "$sandbox_root" "$state" "$runtime"

git -C "$repo" init -b main
git -C "$repo" config user.name "Omarchy YOLO Live Smoke"
git -C "$repo" config user.email "omarchy-yolo-smoke@localhost"
printf '# disposable live smoke\n' >"$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -m initial

cat >"$config" <<EOF
[engine]
max_parallel = 1
max_global_workers = 1
max_attempts = 1
max_final_cycles = 0
max_tasks = 2
agent_timeout_seconds = 300
gate_timeout_seconds = 120
planner_agent = "$agent"
reviewer_agent = "$agent"
integrator_agent = "$agent"
worker_agents = ["$agent"]
auto_apply = false
cleanup_worktrees = false
execution_profile = "yolo-worktree"
final_review_chunk_bytes = 60000
final_review_max_files = 16
final_review_allow_binary = false

[gates]
commands = ["git diff --check"]
final_commands = ["git diff --check"]

[sandbox]
backend = "native"
network = true
EOF

export OMARCHY_YOLO_CONFIG="$config"
export OMARCHY_YOLO_STATE_DIR="$state"
export XDG_RUNTIME_DIR="$runtime"

python -m omarchy_yolo daemon >"$sandbox_root/daemon.log" 2>&1 &
daemon_pid=$!

ready=0
for _ in {1..50}; do
  if python - <<'PY' >/dev/null 2>&1
import asyncio
from omarchy_yolo.rpc import rpc_call
from omarchy_yolo.util import xdg_runtime_dir

asyncio.run(rpc_call(xdg_runtime_dir() / "omarchy-yolo.sock", "ping"))
PY
  then
    ready=1
    break
  fi
  sleep 0.1
done
if [[ "$ready" != 1 ]]; then
  cat "$sandbox_root/daemon.log" >&2 || true
  echo "isolated live-smoke daemon did not become ready" >&2
  exit 1
fi

python -m omarchy_yolo doctor
python -m omarchy_yolo run \
  --repo "$repo" \
  --no-apply \
  --watch \
  "Create a file named YOLO_SMOKE.txt containing exactly the text omarchy-yolo-live-smoke followed by a newline. Do not modify any other repository file."

status_json="$(python -m omarchy_yolo status --json)"
STATUS_JSON="$status_json" python - <<'PY'
import json
import os
from pathlib import Path

status = json.loads(os.environ["STATUS_JSON"])
job = status["job"]
assert job["state"] == "completed", status
candidate = Path(job["integration_path"])
content = (candidate / "YOLO_SMOKE.txt").read_text()
assert content == "omarchy-yolo-live-smoke\n", content
assert status["counts"].get("completed", 0) >= 1, status
print(f"live job completed: {job['id']} -> {job['integration_branch']}")
PY

echo "real-agent disposable live smoke passed"
