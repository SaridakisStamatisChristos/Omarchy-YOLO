# Changelog

## 1.1.0 - 2026-08-23

- Add a daemon-wide worker semaphore so multiple jobs share one global execution budget instead of multiplying `max_parallel` per job.
- Add canonical per-repository async locks for worktree topology, integration merges, cleanup, preflight, and source application across concurrent jobs.
- Replace the old bounded single-diff final audit with fail-closed hierarchical review: every changed file is enumerated, split into bounded shards, reviewed, then synthesized for cross-file defects.
- Refuse finalization when file count, per-file diff size, total review size, or synthesis manifest limits are exceeded instead of silently truncating review coverage.
- Require an explicit read-only capability for planner/reviewer agents; custom adapters must declare `review_command` rather than inheriting their mutating worker command.
- Force Bubblewrap planner/reviewer HOME read-only and allow worker mode to expose only explicitly configured writable HOME subpaths when desired.
- Expose daemon-wide runtime utilization in status/RPC responses.
- Hold SQLite reads through row fetch completion and run `PRAGMA quick_check` at state initialization so corrupt databases fail closed with a clear error.
- Make process logging fail safely under storage errors: the durable log is opened before spawning a child, and stream-write failures terminate the process group.
- Pin GitHub Actions revisions and the CI/development Python toolchain; build wheels without dependency re-resolution.
- Add branch-coverage enforcement, global-scheduler tests, hierarchical-review coverage tests, corrupt-database and interrupted-merge failure injection, and ENOSPC process-launch regression coverage.
- Add an opt-in self-hosted `live-smoke` workflow for real Codex/Claude/OpenCode and Omarchy environments without making proprietary CLIs a normal PR dependency.

## 1.0.1 - 2026-08-23

- Repair the live GitHub database schema/recovery corruption that could prevent fresh daemon startup.
- Bound and distinguish RPC transport/application failures; clamp list/event requests.
- Preserve explicit stops across daemon restarts and make resume atomic with stale-attempt cleanup.
- Close in-flight task/attempt state durably on stop, interruption, and fatal orchestration errors.
- Harden Git worktree ownership/branch checks before destructive cleanup or branch operations.
- Bound agent prompts, process/gate output, and logs to prevent `E2BIG`, memory growth, and disk exhaustion.
- Harden read-only Codex/Claude review profiles and structured planner/reviewer output validation.
- Validate config ranges/profiles/branch prefixes and harden XDG runtime directory handling.
- Sanitize terminal output/log paths and force plain-text rendering in the Quickshell panel.
- Fix installer plugin-validation semantics and XDG-aware purge behavior.

## 1.0.0 - 2026-08-23

- Persistent user daemon with Unix-socket RPC.
- SQLite WAL state, event ledger, crash recovery, stale-worktree branch reattachment, singleton daemon lock, and stop/resume semantics with preserved attempt history.
- Multi-agent planner/worker/reviewer/integrator pipeline.
- Parallel Git worktrees with deterministic branch naming, hook-suppressed orchestrator commits, and merge serialization.
- Codex, Claude Code, current OpenCode `--auto`, and generic command adapters; read-only planner/reviewer profiles.
- Recursive retry and final-audit loops, process-group termination on worker/gate cancellation and timeout.
- Auto-detected project test gates plus explicit configurable gates.
- Optional automatic fast-forward application to the user's branch.
- Omarchy Quattro bar/panel plugin and systemd user service.
- `yolo doctor`, `run`, `watch`, `status`, `jobs`, `events`, `stop`, `resume`, `ui` commands.
