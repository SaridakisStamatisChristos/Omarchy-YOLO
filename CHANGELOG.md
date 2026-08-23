# Changelog

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
