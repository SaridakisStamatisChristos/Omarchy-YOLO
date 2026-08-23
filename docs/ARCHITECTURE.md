# Architecture

```text
                              user / Quickshell
                                     |
                               Unix JSON-RPC
                                     |
                              omarchy-yolod
                                     |
                  +------------------+------------------+
                  |                  |                  |
               planner           scheduler          event log
                  |                  |                  |
                  +------------ task DAG ---------------+
                                     |
                         parallel worker slots
                    +----------------+----------------+
                    |                |                |
                  Codex            Claude          OpenCode
                    |                |                |
                 worktree         worktree         worktree
                    +----------------+----------------+
                                     |
                              gates + reviewer
                                     |
                           serialized integration
                                     |
                             final gates/audit
                                     |
                         yolo/<job>/integration
```

## Invariants

1. Every worker edits a dedicated worktree and branch.
2. Dependency tasks are not scheduled until dependencies are integrated.
3. Integration is serialized under a per-job lock.
4. A task is not integrated until its worker exits successfully, configured gates pass, and the
   reviewer returns `pass`.
5. A task keeps one isolated worktree across its retry cycle so later attempts can repair earlier partial work; its branch is never shared with another task.
6. The source branch is never rewritten. Optional auto-apply is fast-forward only.
7. Daemon state is durable in SQLite before externally visible transitions are emitted.
8. Processes run as the logged-in user; the daemon refuses UID 0.

## Persistence

State defaults to `$XDG_STATE_HOME/omarchy-yolo` (or `~/.local/state/omarchy-yolo`). SQLite uses WAL
mode and append-only events. On daemon restart, jobs that were planning/running are re-queued. Tasks
that were in-flight are reset to pending, their running attempt records are closed as cancelled, and execution
resumes from the durable task worktree with new monotonic attempt numbers. Explicitly stopping jobs is different:
`stop_requested` is durable and restart recovery leaves those jobs in `stopped` until a user requests `resume`.

## Agent protocol

The core is adapter-based. An adapter receives a prompt, working directory, execution profile,
timeout, and log path. The orchestrator does not depend on any provider SDK. Default adapters shell
out to the coding CLIs Omarchy already supports; custom adapters can be configured as argv templates.

Planner and reviewer outputs are extracted from mixed CLI output by selecting the last valid JSON object.
The DAG validator rejects duplicate/invalid IDs, missing dependencies, self-dependencies, cycles, invalid risk
classes, and task counts above the configured limit. Planner/reviewer execution uses non-mutating profiles where
the supported CLIs provide them.
