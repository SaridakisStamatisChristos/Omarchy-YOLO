# Changelog

## 1.4.0 - 2026-08-23

- Add an executable orchestration state reference model covering every job/task/attempt transition plus cross-record terminal-state invariants; ordinary durable state mutations fail closed on illegal transitions while transactional recovery keeps its explicit modeled recovery paths.
- Introduce SQLite schema version 2 using `PRAGMA user_version`, transactional forward migrations, fail-closed future-schema detection, automatic private pre-migration backups, and post-migration integrity verification.
- Add deterministic, SHA-256-addressed execution dossiers containing accepted Git commits, effective policy/config fingerprints, agent capability contracts, task/attempt history and a hashed durable-event prefix; persist the dossier before a job is durably completed.
- Add the offline `yolo-dossier` verifier/exporter so provenance can be independently checked from SQLite even when the daemon is not running.
- Add optional systemd/cgroups-v2 resource sovereignty for all agent and gate process trees with configurable `MemoryHigh`, `MemoryMax`, `TasksMax`, `CPUQuota` and `IOWeight`; the default remains `backend = "none"` for compatibility.
- Preserve v1.3 Bubblewrap/hostile-repository isolation while composing cgroup scopes outside the sandbox so resource limits govern the complete child process tree.
- Expand state-machine, migration, provenance, resource-policy and orchestration-path regression coverage; legacy tests now seed states through legal lifecycle transitions rather than bypassing the production contract.
- Keep package runtime, wheel metadata, installer output and Quickshell manifest synchronized at `1.4.0`, and verify the `yolo-dossier` entry point from the clean installed wheel.

## 1.3.0 - 2026-08-23

- Make every control-plane Git subprocess finite and output-bounded, terminate descendant process groups even after a command leader exits, disable ambient Git redirection/config, and use the configured release identity for unsigned integration commits; apply the same surviving-descendant cleanup to agent and gate timeouts.
- Defer task cancellation until blocking Git mutations finish so repository locks cannot be released while an uncancellable worker thread still changes shared worktree/ref state, and revalidate persisted worktrees against the canonical repository and expected branch before recovery reuse.
- Roll back an integration transaction on cancellation, atomically persist acceptance around optional source application, and preserve an already completed job when post-release cleanup is interrupted.
- Add opt-in `sandbox.hostile_repo_mode`: every Bubblewrap child receives fresh HOME/runtime and filtered environment views, only explicit HOME exceptions reach agents, gates are forced offline, and repository Git filters/merge drivers are neutralized.
- Add formal worker/planner/reviewer/integrator capability contracts, optional per-agent role allowlists, review-only adapters, executable identity checks, doctor validation, and RPC introspection.
- Make hostile/non-UTF-8 changed-file labels injective and single-line so filenames cannot forge final-review prompt structure while ordinary paths retain their stable representation.
- Correct recent-event status under interleaved multi-job event IDs; expose per-attempt agent/state/duration, task state age, job duration, scheduler wait/busy counters, peaks, and cancellation telemetry; and compact oversized status diagnostics without dropping task identity/state from the RPC response.
- Expand the Omarchy Quickshell panel with active/review/integration counts, worker capacity/waiters, attempts, durations, state age, per-task agent telemetry, failure badges, and the latest durable event.
- Harden daemon/CLI lifecycle edges with bounded RPC drains/closes, bounded UI/systemd activation, private non-symlink log reads, and a no-follow/single-link singleton lock.
- Add deterministic state-matrix, recovery, cancellation, hostile-filter, timeout/output, identity, isolation, telemetry, prompt-boundary, and CLI-contract regression tests.
- Raise aggregate branch coverage enforcement to 76% and raise the Git, gate/process, orchestration, DB-record, runtime, sandbox, and review-source module floors without weakening any existing gate.
- Keep package, daemon, installer, and Omarchy plugin release versions synchronized at `1.3.0`.

## 1.2.0 - 2026-08-23

- Make gate execution fail closed on durable-log/storage errors: mid-stream failures terminate the gate process group and propagate instead of being discarded.
- Treat task worktree cleanup as post-integration housekeeping: cleanup failure emits `task.cleanup_failed` without rewriting a task/job that already integrated successfully.
- Add a dedicated Bubblewrap gate profile with writable repository cwd, read-only host filesystem/HOME, and network policy inherited from sandbox configuration.
- Upgrade final audit from shard-only synthesis to a three-level semantic hierarchy: file-local shards, per-file semantic synthesis for multi-shard files, then global cross-file synthesis.
- Reject binary changes by default because semantic content cannot be inspected; `engine.final_review_allow_binary = true` explicitly opts into metadata-only binary review.
- Require meaningful semantic summaries on reviewer `pass` results while preserving the v1.1 synthesis-call compatibility path.
- Add failure-injection and trust-boundary tests for ENOSPC gate logs, process termination, cleanup failure, binary diffs, hostile filenames, gate sandboxing, semantic synthesis, integration rollback, DB symlink refusal, and cancelled resource waiters.
- Raise aggregate branch coverage enforcement from 70% to 75% and add module-specific floors for Git, gates/process, DB, runtime/sandbox, orchestration, and review code.
- Increase measured branch coverage to more than 76%; critical paths including Git, gates, process handling and integration state transitions receive materially stronger direct coverage.
- Verify the built wheel by installing and executing it in a clean virtual environment on both Python 3.12 and 3.13.
- Pin the CI/development dependency artifacts by SHA-256 and install them with `pip --require-hashes`.
- Make package `__version__` the canonical runtime version used by CLI and daemon RPC; release version is `1.2.0`.
- Expand the opt-in live smoke into a disposable end-to-end agent transaction through daemon, RPC, planning, implementation, review, finalization and integration-branch verification; ping Omarchy Shell when available.

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
