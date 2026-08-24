# How Omarchy YOLO Works

Omarchy YOLO is a persistent local control plane for autonomous software engineering. It does not simply launch a coding model and trust the result. The daemon owns scheduling, durable state, isolated worktrees, retries, verification, integration, rollback, final acceptance, and provenance. Coding agents are replaceable workers inside that control loop.

This guide explains one job from submission to completion. For lower-level implementation invariants and trust boundaries, see [ARCHITECTURE.md](ARCHITECTURE.md) and [../SECURITY.md](../SECURITY.md).

## Mental model

The shortest useful model is:

```text
Codex / Claude / OpenCode
        │
        │ propose code and reasoning
        ▼
┌──────────────────────────────────┐
│          Omarchy YOLO            │
│                                  │
│ owns scheduling                  │
│ owns durable state               │
│ owns worktrees and Git topology  │
│ owns retries                     │
│ owns deterministic gates         │
│ owns adversarial review          │
│ owns integration and rollback    │
│ owns final acceptance            │
│ owns provenance                  │
└──────────────────────────────────┘
        │
        ▼
 verified integration candidate
```

A raw coding-agent invocation says, in effect, "here is my repository; solve this." Omarchy YOLO instead gives agents bounded roles inside a transaction-oriented engineering process.

## End-to-end lifecycle

```text
user goal
  ↓
yolo CLI / Quickshell
  ↓
Unix JSON-RPC
  ↓
persistent daemon
  ↓
durable SQLite job
  ↓
read-only planner
  ↓
validated task DAG
  ↓
parallel isolated workers
  ↓
task gates
  ↓
adversarial task review
  ↓
serialized Git integration
  ↓
post-merge verification / repair
  ↓
final repository gates
  ↓
file-local semantic review
  ↓
per-file synthesis
  ↓
global cross-file synthesis
  ↓
deterministic execution dossier
  ↓
yolo/<job>/integration
  ↓
optional conservative fast-forward of source branch
```

## 1. Submit a goal

From a clean Git repository:

```bash
yolo run --watch \
  "Audit this repository, repair every material defect, run the complete test suite, and leave a release-ready candidate"
```

The CLI is a control client. It sends the request to the persistent daemon over the private Unix-socket RPC interface. Closing the terminal does not terminate the job.

The Quickshell panel uses the same control plane for status and controls.

## 2. Repository preflight

Before autonomous work begins, YOLO resolves and validates the repository, records the original source branch and exact base commit, and applies the selected Git/security policy.

Conceptually:

```text
base_branch = main
base_commit = abc123...
```

The original checkout is not the worker workspace. It remains protected while workers operate elsewhere.

When hostile-repository mode is enabled, control-plane Git also neutralizes repository-configured filters and merge drivers so tracked repository configuration cannot silently execute commands during orchestration-owned Git operations.

## 3. Create durable job state

The daemon creates a durable job record in SQLite. Jobs, tasks, attempts, events, summaries, errors, worktree metadata, and provenance survive terminal closure and daemon restart.

State lives under the configured state directory, normally under `$XDG_STATE_HOME/omarchy-yolo`.

SQLite is treated as control-plane state, not disposable cache. The database uses WAL, foreign keys, private permissions, integrity checks, schema versioning, and fail-closed migration behavior.

## 4. Enforce the executable lifecycle model

Ordinary job, task, and attempt mutations pass through the v1.4 state-transition contract.

A normal job progresses broadly as:

```text
QUEUED
  ↓
PLANNING
  ↓
RUNNING
  ↓
COMPLETED
```

Other legal paths include stopping, failure, recovery, and resume.

A normal task progresses as:

```text
PENDING
  ↓
RUNNING
  ↓
REVIEWING
  ↓
INTEGRATING
  ↓
COMPLETED
```

Successful terminal states are absorbing. Recovery transitions are explicit rather than accidental. This makes lifecycle bugs observable at the database boundary instead of allowing impossible durable history.

## 5. Reserve daemon-wide resources

The daemon owns a shared `ResourceCoordinator`.

Two levels of scheduling matter:

- per-job parallelism limits how much one job may consume;
- daemon-global worker capacity limits all active agent work across every job.

Repository topology mutations also use canonical per-repository locks. Jobs for unrelated repositories can proceed concurrently, while worktree/ref/integration changes targeting the same repository are serialized.

## 6. Plan the job as a DAG

YOLO chooses an adapter that satisfies the planner capability contract and invokes it read-only.

The planner converts the user goal into a bounded directed acyclic graph. For example:

```text
T1: Audit authentication
T2: Repair API logic        depends on T1
T3: Add regression tests    depends on T2
T4: Update documentation    depends on T2
```

YOLO validates the structure itself. Invalid task IDs, duplicate IDs, missing dependencies, self-dependencies, cycles, invalid risk classes, and task counts above policy are rejected.

The planner proposes a decomposition; the orchestrator determines whether the decomposition is structurally legal.

## 7. Schedule only dependency-ready tasks

A dependency is not considered satisfied merely because an agent exited successfully. Dependent work becomes runnable only after the prerequisite task has passed verification and been integrated into the shared candidate.

For example:

```text
        T1
        │
        T2
       /  \
     T3    T4
```

T3 and T4 may run in parallel only after T2 is integrated.

## 8. Give each task an isolated Git worktree

Every task gets its own branch and worktree.

```text
T1 → worktree-T1 → branch-T1
T2 → worktree-T2 → branch-T2
T3 → worktree-T3 → branch-T3

                    ↓ later
             integration worktree
```

Workers therefore do not edit one shared checkout.

A task keeps the same isolated worktree across its retry cycle so later attempts can repair partial work left by earlier attempts without contaminating other tasks.

## 9. Select agents by declared capability

Adapters expose effective roles over:

```text
worker
planner
reviewer
integrator
```

Operators may narrow those roles explicitly.

A worker-only adapter is not silently trusted as a reviewer. Planner/reviewer use requires a declared non-mutating review path. Custom review-capable adapters require an explicit `review_command` rather than inheriting write capability by assumption.

The daemon exposes the resulting contracts through RPC and `yolo agents`; `yolo doctor` verifies that required roles are satisfiable.

## 10. Run the worker in the task worktree

A selected worker receives a prompt, task worktree, execution profile, timeout, log path, environment policy, sandbox policy, and optional resource policy.

The worker may inspect and modify its assigned worktree. It does not own global integration topology.

YOLO remains provider-SDK independent: adapters shell out to Codex, Claude Code, OpenCode, or configured custom CLIs.

## 11. Apply process and resource bounds

Agent and gate processes are bounded control-plane children.

They receive finite timeouts, bounded output capture, bounded durable logging, process-group termination, and descendant cleanup.

If `[resources].backend = "systemd"` is enabled, YOLO additionally launches the child process tree in a transient systemd user scope backed by cgroups v2. Policy may include:

```text
MemoryHigh
MemoryMax
TasksMax
CPUQuota
IOWeight
```

This limits resource exhaustion outside the child itself.

Resource scopes compose with Bubblewrap rather than replacing it:

```text
systemd / cgroup scope
        ↓
Bubblewrap boundary
        ↓
agent or gate process tree
```

## 12. Run deterministic task gates

Worker exit success is not acceptance.

The task must pass configured or auto-detected repository gates, for example:

```text
pytest
npm test
cargo test
go test
make test
```

Gate commands are timeout-bounded and durably logged. Log-open or mid-stream storage failure fails closed and terminates the relevant process group.

With Bubblewrap enabled, gates use a dedicated profile: the repository remains writable for normal build/test artifacts while host filesystem/HOME access and networking follow policy.

In hostile-repository mode, gates receive a fresh runtime/HOME view, filtered environment, private `/tmp`, and no network.

## 13. Run adversarial task review

After deterministic gates pass, YOLO invokes a review-capable adapter read-only.

The reviewer examines whether the implementation is actually correct, complete, safe, in-scope, and compatible with surrounding code. A successful worker process and green tests are not enough on their own.

Task acceptance is effectively:

```text
worker succeeded
AND
deterministic gates passed
AND
adversarial reviewer returned pass
```

Malformed review output or a material finding blocks progression.

## 14. Retry and repair when necessary

A task may enter another bounded attempt when:

- the worker fails;
- deterministic gates fail;
- review rejects the candidate;
- an integration/repair cycle requires more work.

Attempt records remain durable and monotonic:

```text
attempt 1 → failed
attempt 2 → review failed
attempt 3 → passed
```

Configured agent rotation can use another worker on a later attempt.

## 15. Enter serialized integration

A task that passes its worker/gate/review cycle still has not joined the release candidate.

YOLO serializes task integration into the dedicated integration worktree.

```text
worker branch
     │
     ▼
integration transaction
     │
     ├── merge
     ├── post-merge gates
     ├── conflict/repair cycle when allowed
     └── rollback on failure
```

This prevents parallel coding from becoming uncontrolled parallel mutation of the candidate branch.

## 16. Make integration cancellation-safe

Git topology operations are bounded blocking side effects. Async cancellation cannot safely stop a worker thread in the middle of such a mutation.

YOLO therefore defers cancellation propagation until the bounded mutation settles and the repository lock can be released safely.

Conceptually:

```text
cancel requested
      ↓
bounded Git mutation finishes
      ↓
rollback / settle transaction
      ↓
release repository lock
      ↓
propagate cancellation
```

Cancellation during integration aborts an active merge or hard-resets a completed merge to the exact pre-transaction commit.

## 17. Re-run verification on the combined candidate

Individually correct tasks can interact incorrectly after integration. Once the DAG is integrated, YOLO runs final repository gates against the complete candidate.

```text
T1 ✓
T2 ✓
T3 ✓
T4 ✓
  ↓
combined integration candidate
  ↓
final repository gates
```

Only a combined candidate that survives this stage proceeds to final semantic review.

## 18. Build a complete changed-file review manifest

The final reviewer does not receive one arbitrarily truncated giant diff.

YOLO enumerates the changed-file set using NUL-safe Git output and constructs bounded per-file review representations. Exotic or non-UTF-8 filenames are represented with injective single-line labels so a filename cannot forge prompt section boundaries.

Hard file-count, per-file-size, and total-review-size ceilings fail finalization rather than silently dropping evidence.

## 19. Review file-local shards

Large textual diffs are split by UTF-8 byte budget without dropping their tail. Every shard contains one file only.

```text
file A
 ├─ shard A1 → reviewer
 └─ shard A2 → reviewer

file B
 └─ shard B1 → reviewer
```

Every shard must pass before higher-level synthesis is allowed.

## 20. Synthesize multi-shard files

If one file spans multiple shards, YOLO performs a dedicated file-level semantic synthesis over the passed shard reports.

This checks whole-file properties that isolated shards can miss, such as:

- API consistency across distant sections;
- state-transition coherence;
- duplicated or contradictory logic;
- local caller/callee assumptions within the same file.

Single-shard files can reuse their shard result directly as their file report.

## 21. Perform global cross-file synthesis

Only passed file-level reports reach the global final reviewer.

The global synthesis receives the complete manifest, final gate results, and semantic file reports so it can detect cross-file defects such as:

```text
file A changes an API contract
file B still expects the old contract

schema changes
migration or consumer does not

config name changes
runtime reader is not updated
```

The release is accepted only when this final synthesis passes.

## 22. Treat binary changes explicitly

Binary changes fail finalization by default because textual semantic review cannot inspect their contents.

`engine.final_review_allow_binary = true` is an explicit opt-in to metadata-only binary review. It intentionally lowers assurance and is not treated as equivalent to semantic inspection.

## 23. Cross the final acceptance boundary

After final gates and hierarchical review pass, YOLO has an accepted integration candidate.

This is a critical lifecycle boundary: downstream provenance and optional source application refer to an already accepted candidate rather than an unverified working state.

## 24. Build the deterministic execution dossier

v1.4 generates a canonical JSON dossier containing provenance for the accepted run, including the accepted Git commits, effective policy/configuration fingerprints, agent capability contracts, task/attempt history, summary hashes, and a digest of the durable event prefix at the publication boundary.

Conceptually:

```text
accepted job state
        +
base/final commits
        +
policy/config fingerprints
        +
agent contracts
        +
tasks and attempts
        +
durable event prefix
        ↓
canonical JSON
        ↓
SHA-256
```

The dossier content and digest are stored durably in SQLite.

The offline `yolo-dossier` command verifies the stored digest before printing or exporting the dossier, so provenance can be checked even when the daemon is not running.

The dossier is provenance, not a cryptographic signature: a user able to rewrite the state database can rewrite both the content and digest.

## 25. Persist provenance before auto-apply

Dossier generation and storage occur before optional source application.

This ordering preserves an important invariant: a provenance-storage failure cannot advance the user's source branch and then leave the job reported as failed.

The dossier records source-apply intent. The actual apply/skipped outcome is recorded afterward in the durable event stream and completion event.

## 26. Preserve the integration branch as the primary result

Without `--apply`, the result is simply a verified integration candidate:

```text
source branch:
main @ A

YOLO candidate:
yolo/<job>/integration @ B
```

The user's source checkout remains untouched. The candidate can be inspected, tested, compared, or merged later.

## 27. Optionally fast-forward the source branch

With `--apply`, YOLO performs a deliberately conservative source update.

Before applying it verifies that the source branch is still the original branch, is still at the original base commit, remains clean, and can be advanced by fast-forward only.

Safe case:

```text
main: A ─────────→ B
```

If the user changed the source while YOLO was running:

```text
main:      A → X
candidate: A → B
```

YOLO refuses auto-apply and preserves the candidate on its integration branch. `job.apply_skipped` records the reason.

## 28. Let accepted completion win the late stop race

Final acceptance and optional source application are protected bounded control-plane work.

A stop request can arrive after final audit acceptance but while protected completion is still settling. If source application has already succeeded, YOLO must not subsequently record the job as stopped.

The executable state model therefore permits accepted completion to win that narrow race while `COMPLETED` itself remains absorbing.

This prevents an impossible durable outcome such as:

```text
source successfully applied
but job recorded as stopped
```

## 29. Mark the job durably completed

The completed job records the final summary, accepted candidate information, provenance digest, and actual source-apply outcome.

At this point the durable truth is broadly:

```text
job       = COMPLETED
all tasks = COMPLETED
attempts  = preserved
candidate = accepted integration commit
dossier   = stored and SHA-256 addressed
```

## 30. Treat cleanup as housekeeping

Temporary worktree/branch cleanup happens only after accepted state is durable.

Cleanup failure therefore produces housekeeping telemetry such as `task.cleanup_failed` or `job.cleanup_failed`; it cannot retroactively rewrite accepted engineering work as failed.

```text
accepted release
      ↓
cleanup failure
      ↓
record housekeeping failure
      ↓
release remains accepted
```

## 31. Recover after daemon crash or reboot

SQLite is the source of execution truth.

On startup YOLO validates database integrity and performs explicit recovery. Jobs interrupted during active work are made schedulable again, in-flight attempts are closed as cancelled, and tasks return to a legal resumable state.

Persisted worktree paths are not trusted blindly. Git must prove that a recovered path belongs to the expected canonical repository and branch before it is reused.

Explicit user stop is different: `stop_requested` is durable, so restart does not resurrect a job that the user intentionally stopped.

## 32. Resume without losing history

`yolo resume JOB_ID` continues the durable job rather than pretending a new run started.

Task DAG, integration state, prior attempts, events, and monotonic attempt numbering are preserved.

```text
attempts before restart: 1, 2
resume
next attempt:            3
```

## 33. Observe and control the job

The Quickshell widget and CLI expose the daemon's durable/control-plane state, including job state, task graph, active/waiting worker capacity, attempt telemetry, task age, recent events, and stop/resume controls.

Useful commands include:

```bash
yolo status [JOB_ID]
yolo jobs
yolo events JOB_ID
yolo logs JOB_ID
yolo agents
yolo stop JOB_ID
yolo resume JOB_ID --watch
yolo-dossier --help
```

The UI is not the source of truth. The daemon and SQLite are.

## Security boundaries in one picture

For trusted local repositories, the basic isolation model is task worktrees plus process/user boundaries.

For less-trusted repositories, Bubblewrap and hostile-repository mode narrow filesystem, HOME, runtime, environment, and network exposure.

Optional cgroup/systemd resource policy adds CPU/RAM/PID/I/O governance.

```text
user account
  ↓
systemd cgroup scope        optional
  ↓
Bubblewrap boundary         optional
  ↓
agent/gate process tree
  ↓
dedicated task worktree
```

These controls do not create a VM. All local child processes ultimately share the host Linux kernel. Deliberately malicious code or exploit research still belongs in a disposable VM/container.

## Why this is different from a raw agent call

A raw coding-agent workflow usually makes the model responsible for both implementation and much of its own acceptance.

Omarchy YOLO separates those concerns:

```text
agent proposes implementation
        ↓
YOLO runs deterministic gates
        ↓
independent reviewer judges task
        ↓
YOLO serializes integration
        ↓
combined candidate is re-tested
        ↓
hierarchical final review
        ↓
YOLO records provenance
        ↓
optional safe source apply
```

The important architectural principle is:

> **Agents are replaceable workers. YOLO is the authority.**

That is what turns the project from an agent launcher into a persistent local autonomous software-engineering control plane.

## Related documentation

- [README](../README.md) — installation, commands, configuration, and quick start.
- [Architecture](ARCHITECTURE.md) — implementation structure, invariants, scheduling, recovery, review, and verification internals.
- [Security](../SECURITY.md) — exact trust boundaries and hostile-repository guidance.
- `config.example.toml` — executable configuration reference.
- `CHANGELOG.md` — release history and behavioral changes.
