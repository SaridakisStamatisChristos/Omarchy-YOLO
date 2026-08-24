# Architecture

For a complete end-to-end walkthrough of one autonomous job—from submission through planning, isolated execution, verification, integration, final review, provenance, recovery, and optional source application—see [HOW_IT_WORKS.md](HOW_IT_WORKS.md). For day-to-day operation and troubleshooting, see [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md). For supported settings, validation ranges, interactions and recommended profiles, see [CONFIGURATION.md](CONFIGURATION.md). This document focuses on implementation structure, invariants, and trust boundaries.

```text
                              user / Quickshell
                                     |
                               Unix JSON-RPC
                                     |
                              omarchy-yolod
                                     |
                    daemon-wide ResourceCoordinator
                   /                 |                \
          global worker slots   per-repo locks      runtime metrics
                   |                 |                |
                   +----------- task DAG ------------+
                                     |
                         parallel isolated workers
                    +----------------+----------------+
                    |                |                |
                  Codex            Claude          OpenCode
                    |                |                |
                 worktree         worktree         worktree
                    +----------------+----------------+
                                     |
                         task gates + reviewer
                                     |
                     per-repository serialized merge
                                     |
                              final repository gates
                                     |
                  complete changed-file manifest + shards
                                     |
                    file-local shard semantic audits
                                     |
                per-file synthesis for multi-shard files
                                     |
                       global cross-file synthesis
                                     |
                         yolo/<job>/integration
```

## Invariants

1. Every worker edits a dedicated worktree and branch.
2. Dependency tasks are not scheduled until dependencies are integrated.
3. `max_parallel` limits one job while `max_global_workers` limits total active agent work across the daemon.
4. Operations that mutate shared Git topology are serialized by a canonical per-repository lock across jobs; task integration also retains its per-job merge lock. Cancellation is deferred until the bounded blocking mutation ends, so an uncancellable thread cannot outlive the lock.
5. A task is not integrated until its worker exits successfully, configured gates pass, and the reviewer returns `pass`.
6. A task keeps one isolated worktree across its retry cycle so later attempts can repair earlier partial work; its branch is never shared with another task.
7. A successfully integrated task remains successful even if later worktree cleanup fails; cleanup is recorded as housekeeping failure rather than changing durable integration truth.
8. The source branch is never rewritten. Optional auto-apply is fast-forward only and is refused when the source moved or became dirty.
9. Final release review is fail-closed: every changed file must be represented by bounded review input, all textual shards must pass, multi-shard files must pass file-level synthesis, and the global synthesis must pass. Size/file ceilings fail the job rather than truncate review silently.
10. Binary changes are rejected by default. Explicit `final_review_allow_binary = true` permits metadata-only binary review and intentionally lowers assurance.
11. Worker/planner/reviewer/integrator selection is capability-gated. Planner/reviewer roles additionally require a declared read-only command/profile; unknown custom worker commands are never assumed safe for review.
12. Daemon state is durable in SQLite before externally visible transitions are emitted.
13. Processes run as the logged-in user; the daemon refuses UID 0.

Recovered worktrees are not trusted from their persisted path alone. Before reuse, Git must prove that the path is
a non-symlink worktree belonging to the canonical repository and checked out on the expected task branch.

## Persistence

State defaults to `$XDG_STATE_HOME/omarchy-yolo` (or `~/.local/state/omarchy-yolo`). SQLite uses WAL,
foreign keys, a private file mode, and append-only events. Startup executes `PRAGMA quick_check`; corrupt
state fails closed instead of entering partial recovery. Symlinked database paths are refused. Reads that
materialize rows hold the database lock through fetch completion.

On daemon restart, jobs that were planning/running are re-queued. Tasks that were in-flight are reset to
pending, their running attempt records are closed as cancelled, and execution resumes from the durable task
worktree with new monotonic attempt numbers. Explicitly stopping jobs is different: `stop_requested` is durable
and restart recovery leaves those jobs in `stopped` until a user requests `resume`.

## Global scheduling and repository coordination

The daemon owns one `ResourceCoordinator` and passes it to every orchestrator. Its semaphore limits total
worker/planner/final-audit occupancy regardless of how many jobs are submitted. Repository locks are keyed by
the resolved repository root and serialize worktree creation/reattachment/removal, integration topology,
preflight and automatic source application across jobs that target the same repository. Jobs for unrelated
repositories can still proceed concurrently. Waiting/active accounting is cancellation-safe and exposed as
bounded utilization snapshots through status and the `runtime` RPC method. Acquisition totals, cancelled waits,
cumulative wait/busy durations and worker peak concurrency provide daemon-lifetime telemetry without an external
metrics service.

Git subprocesses have a configured finite timeout, bounded stdout/stderr and process-group termination. Ambient
`GIT_*` repository redirection/config is stripped, interactive prompts and pagers are disabled, and control-plane
commits/merges use the configured identity with signing disabled. Async cancellation waits for these bounded
operations to finish before the repository lock is released. Cancellation anywhere in the serialized integration
transaction aborts an in-progress merge and hard-resets a completed merge to its pre-transaction commit.
After final audit acceptance, optional source application and durable completion are cancellation-shielded as one
bounded control-plane step, so a successfully applied source branch cannot be reported as queued after interruption.

## Agent protocol

The core is adapter-based. An adapter receives a prompt, working directory, execution profile, timeout, and log
path. The orchestrator does not depend on any provider SDK. Default adapters shell out to the coding CLIs Omarchy
already supports; custom adapters can be configured as argv templates.

Each adapter exposes an effective capability contract over `worker`, `planner`, `reviewer`, and `integrator`.
Operators can narrow that contract with an explicit role allowlist. A review-only adapter is valid without a worker
command. The daemon exposes contracts through RPC and `doctor` verifies that all required roles are satisfiable.

Built-in Codex, Claude and OpenCode adapters have explicit read-only review transformations. A custom adapter is
eligible for planner/reviewer roles only when `review_command` is configured. With Bubblewrap, planner/reviewer
HOME and repository access are read-only. Worker mode may expose selected writable HOME subpaths. Gate mode uses
a separate policy: repository cwd is writable for builds/tests, host filesystem/HOME are read-only, and network
access follows the configured sandbox policy.

Planner and reviewer outputs are extracted from mixed CLI output by selecting the last valid JSON object. The DAG
validator rejects duplicate/invalid IDs, missing dependencies, self-dependencies, cycles, invalid risk classes,
and task counts above the configured limit.

## Hierarchical final review

The final auditor never receives one arbitrarily truncated repository diff. The integration candidate is enumerated
with NUL-safe Git path output. Each textual changed file receives a bounded no-textconv/no-external-diff
representation; large patches are split by UTF-8 byte budget without dropping their tail. Each shard contains only
one file, which preserves local semantic context and prevents unrelated files from competing for the same shard
budget. Ordinary paths retain readable labels; exotic/non-UTF-8 paths use a tagged, injective, single-line JSON
representation so a filename cannot forge prompt section boundaries or collide with another review object.

Every shard is reviewed independently. Any material shard finding blocks higher-level synthesis. If one file spans
multiple shards, those passed shard reports are then synthesized into a dedicated file-level semantic result that
checks whole-file API/state-transition coherence. Only passed file-level results are eligible for the final global
synthesis, which receives the complete manifest, final gate results and semantic file reports to detect cross-file
and release-level defects. Hard ceilings on file count, per-file diff size and total review bytes deliberately fail
closed.

Binary diffs are a separate trust case: they fail finalization by default because content semantics are unavailable
to the textual reviewer. Operators may explicitly opt into metadata-only binary review, but that is recorded policy,
not equivalent semantic assurance.

## Gate execution

Task and final repository gates are bounded subprocesses with durable private logs. Log creation failure prevents
child launch; mid-stream log/storage failure terminates the process group and propagates to orchestration. Timeouts
and cancellation also terminate the process group.

When Bubblewrap is enabled, gates use a dedicated execution profile rather than the worker/reviewer profile. The
repository cwd remains writable so normal build/test tools can create workspace artifacts, while host filesystem and
HOME are read-only. Network is shared or unshared according to `sandbox.network`.

`sandbox.hostile_repo_mode` tightens the child-process boundary for untrusted repositories: it requires Bubblewrap,
replaces HOME and runtime with fresh mounts, filters agent/gate environments, and makes `/tmp` private. Explicit
HOME exceptions are writable only for worker/integrator profiles and read-only for planner/reviewer profiles; gates
receive none and always unshare networking. Agent networking follows policy because remote CLIs may require it. The
candidate cwd remains writable because build/test gates need artifacts. In parallel,
`git.allow_repository_commands=false` enumerates and neutralizes configured clean/smudge/process filters and custom
merge drivers before control-plane Git operations.

## Release verification

CI runs Ruff, strict Mypy, branch-coverage tests with a 76% aggregate floor, raised critical-module coverage floors,
wheel build, and clean-environment
wheel execution on CPython 3.12 and 3.13. Development dependencies are pinned by version and SHA-256 artifact hash
and installed with `pip --require-hashes`. The opt-in live smoke goes further by creating a disposable repository,
starting an isolated daemon and executing a complete public CLI/RPC agent transaction through planning,
implementation, review and audited integration; Omarchy Shell IPC is checked when available.