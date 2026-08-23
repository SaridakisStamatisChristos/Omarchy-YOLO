# Changelog

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
