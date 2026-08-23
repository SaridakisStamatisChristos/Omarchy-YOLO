# Operations

## State locations

Default paths follow XDG conventions:

- config: `~/.config/omarchy-yolo/config.toml`
- environment overrides: `~/.config/omarchy-yolo/env`
- database: `~/.local/state/omarchy-yolo/state.sqlite3`
- worktrees: `~/.local/state/omarchy-yolo/worktrees/`
- logs: `~/.local/state/omarchy-yolo/logs/`
- RPC socket: `$XDG_RUNTIME_DIR/omarchy-yolo.sock`

`OMARCHY_YOLO_CONFIG` and `OMARCHY_YOLO_STATE_DIR` override the config and state roots respectively.

## Service

```bash
systemctl --user status omarchy-yolo.service
systemctl --user restart omarchy-yolo.service
journalctl --user -u omarchy-yolo.service -f
```

Jobs are not owned by client terminals. Restart recovery requeues interrupted active jobs from SQLite, while an
explicit `stop` remains stopped across daemon restarts. `resume` atomically closes stale running attempts before
making tasks schedulable again. Startup runs SQLite `PRAGMA quick_check`; corrupt durable state is rejected with a
clear initialization error instead of being partially recovered.

## Global scheduling

`engine.max_parallel` is a per-job task ceiling. `engine.max_global_workers` is the daemon-wide ceiling shared by
all jobs, including planner/final-audit occupancy. Status JSON includes a `runtime` object with active/waiting
workers and repository-lock utilization; the same data is available through the `runtime` RPC method. Runtime
telemetry also includes cumulative worker/repository wait and busy time, acquisition counts, cancelled waits and
peak active workers. Per-job `telemetry` includes attempt state/agent/duration aggregates, job/state age, and each
task's latest attempt. If extreme diagnostic text would exceed the RPC ceiling, status marks itself `truncated` and
compacts errors/events while retaining every task's identity and state.

Jobs targeting different repositories may run concurrently. Shared Git topology operations for the same resolved
repository root are serialized across jobs. This prevents worktree/ref races but does not resolve contradictory
engineering goals; submit dependent goals sequentially when semantic ordering matters.

## Job lifecycle

```text
queued -> planning -> running -> completed
                         |  \
                         |   -> failed -> resume -> queued
                         -> stopping -> stopped -> resume -> queued
```

Tasks move through `pending`, `running`, `reviewing`, `integrating`, and `completed`. `failed`, `blocked`, and
`stopped` are recoverable through explicit `resume` unless the job itself is already completed.

## Candidate branches

Every job creates:

```text
yolo/<run-id>/integration
```

and each task creates a temporary child branch beneath the job ID. Successful task branches/worktrees are
removed when cleanup is enabled; the integration branch remains as the audit/release artifact.

## Final audit ceilings

The release audit is hierarchical. Every changed file must fit the configured review envelope:

```toml
[engine]
final_review_chunk_bytes = 60000
final_review_chunk_files = 8
final_review_max_files = 512
```

There are additional hard safety ceilings on per-file and total review bytes. Exceeding a ceiling is an explicit
finalization failure, not a truncated approval. Split unusually large releases into smaller goals rather than
raising limits indiscriminately.

## Custom reviewer agents

Custom agent commands are worker-only by default. To make one eligible for planner/reviewer roles, declare a
separate non-mutating command:

```toml
[agents.local]
command = ["my-agent", "--write"]
review_command = ["my-agent", "--read-only"]
```

For Bubblewrap deployments, planner/reviewer HOME is always read-only. Worker mode can use
`read_only_home = true` with `writable_home_paths` for selected credential/session directories.

The optional `roles` list restricts an adapter to an explicit subset of `worker`, `planner`, `reviewer`, and
`integrator`. Omitting it preserves built-in inference. A dedicated review-only adapter may omit `command`:

```toml
[agents.audit]
review_command = ["my-auditor", "--read-only"]
roles = ["planner", "reviewer"]
```

`yolo agents --json` reports each effective contract and `yolo doctor` validates that all pipeline roles have an
available implementation.

## Hostile repository mode

For repositories whose tracked code and gate commands are hostile, install Bubblewrap and use:

```toml
[sandbox]
backend = "bwrap"
hostile_repo_mode = true
network = false
gate_env_allowlist = []
agent_env_allowlist = []
writable_home_paths = []

[git]
allow_repository_commands = false
command_timeout_seconds = 120
```

All child profiles receive fresh HOME, `XDG_RUNTIME_DIR`, and `/tmp` mounts plus filtered environments. Paths in
`writable_home_paths` are the only host HOME content re-exposed: workers/integrators receive writable binds and
planners/reviewers receive read-only binds. Gates receive no HOME exceptions, retain write access to the candidate
repository, and always run without network. Agent network still follows `sandbox.network` for remote model CLIs.
Use `agent_env_allowlist` and `gate_env_allowlist` sparingly: any listed secret is readable by the corresponding
untrusted process. Orchestrator Git also disables repository-configured clean/smudge/process filters and custom
merge drivers.

The user service allows up to 16 minutes for orderly shutdown because cancellation waits for a bounded Git
topology mutation before releasing its repository lock. `git.command_timeout_seconds` is capped at 900 seconds.

## Validation

Normal PR CI is deterministic and does not require proprietary coding CLIs. It runs pinned development tooling,
Ruff, strict Mypy, branch-coverage tests, source/shell checks, and a wheel build on Python 3.12 and 3.13.

A separate `live-smoke` workflow is manual and intended for a trusted self-hosted runner labelled
`omarchy-yolo-live` with at least one authenticated Codex/Claude/OpenCode CLI. It validates the real workstation
environment without making secrets/proprietary CLIs part of ordinary pull-request CI.

## Troubleshooting

Start with:

```bash
yolo doctor
yolo status --json
yolo events <job-id>
yolo logs <job-id>
```

If an agent CLI was upgraded and flags changed, edit the corresponding `[agents.NAME].command` array. If its
read-only invocation changed, update `review_command` (custom adapters) or validate the built-in profile before
using it for review.

If the Omarchy panel is missing, verify the plugin and rescan:

```bash
omarchy plugin validate ~/.config/omarchy/plugins/dev.aether.yolo
omarchy-shell -q shell rescanPlugins
omarchy plugin enable dev.aether.yolo
yolo ui
```
