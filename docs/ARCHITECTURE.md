# Architecture

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
                              gates + reviewer
                                     |
                     per-repository serialized merge
                                     |
                              final repository gates
                                     |
                  complete changed-file manifest + shards
                                     |
                     shard audits -> synthesis audit
                                     |
                         yolo/<job>/integration
```

## Invariants

1. Every worker edits a dedicated worktree and branch.
2. Dependency tasks are not scheduled until dependencies are integrated.
3. `max_parallel` limits one job while `max_global_workers` limits total active agent work across the daemon.
4. Operations that mutate shared Git topology are serialized by a canonical per-repository lock across jobs; task integration also retains its per-job merge lock.
5. A task is not integrated until its worker exits successfully, configured gates pass, and the reviewer returns `pass`.
6. A task keeps one isolated worktree across its retry cycle so later attempts can repair earlier partial work; its branch is never shared with another task.
7. The source branch is never rewritten. Optional auto-apply is fast-forward only and is refused when the source moved or became dirty.
8. Final release review is fail-closed: every changed file must be represented by a bounded audit shard, every shard must pass, and a synthesis audit must pass. Size/file ceilings fail the job rather than truncate review silently.
9. Planner/reviewer roles require a declared read-only capability. Unknown custom worker commands are never assumed safe for review.
10. Daemon state is durable in SQLite before externally visible transitions are emitted.
11. Processes run as the logged-in user; the daemon refuses UID 0.

## Persistence

State defaults to `$XDG_STATE_HOME/omarchy-yolo` (or `~/.local/state/omarchy-yolo`). SQLite uses WAL,
foreign keys, a private file mode, and append-only events. Startup executes `PRAGMA quick_check`; corrupt
state fails closed instead of entering partial recovery. Reads that materialize rows hold the database lock
through fetch completion.

On daemon restart, jobs that were planning/running are re-queued. Tasks that were in-flight are reset to
pending, their running attempt records are closed as cancelled, and execution resumes from the durable task
worktree with new monotonic attempt numbers. Explicitly stopping jobs is different: `stop_requested` is durable
and restart recovery leaves those jobs in `stopped` until a user requests `resume`.

## Global scheduling and repository coordination

The daemon owns one `ResourceCoordinator` and passes it to every orchestrator. Its semaphore limits total
worker/planner/final-audit occupancy regardless of how many jobs are submitted. Repository locks are keyed by
the resolved repository root and serialize worktree creation/reattachment/removal, integration topology,
preflight and automatic source application across jobs that target the same repository. Jobs for unrelated
repositories can still proceed concurrently. The coordinator exposes bounded utilization snapshots through
status and the `runtime` RPC method.

## Agent protocol

The core is adapter-based. An adapter receives a prompt, working directory, execution profile, timeout, and log
path. The orchestrator does not depend on any provider SDK. Default adapters shell out to the coding CLIs Omarchy
already supports; custom adapters can be configured as argv templates.

Built-in Codex, Claude and OpenCode adapters have explicit read-only review transformations. A custom adapter is
eligible for planner/reviewer roles only when `review_command` is configured. With Bubblewrap, planner/reviewer
HOME is always read-only; worker mode can optionally expose only selected writable HOME subpaths.

Planner and reviewer outputs are extracted from mixed CLI output by selecting the last valid JSON object. The DAG
validator rejects duplicate/invalid IDs, missing dependencies, self-dependencies, cycles, invalid risk classes,
and task counts above the configured limit.

## Hierarchical final review

The final auditor no longer receives one arbitrarily truncated diff. The integration candidate is enumerated with
NUL-safe Git path output, each changed file receives a bounded no-textconv/no-external-diff representation, and
large textual patches are split by UTF-8 byte budget without dropping their tail. Shards are grouped by both byte
and file-count limits. Every shard is reviewed independently; any material finding blocks synthesis. Only after all
shards pass does a synthesis reviewer receive the complete changed-file manifest, final gate results and shard
summaries to look for cross-file/integration defects. Hard ceilings on file count, per-file diff size and total
review bytes deliberately fail closed.
