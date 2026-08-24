# Omarchy YOLO Operator Guide

This guide is for the person running Omarchy YOLO day to day. It focuses on installation verification, launching and supervising jobs, understanding state, inspecting failures, recovering interrupted work, handling execution dossiers, and operating trusted versus hostile repositories.

For the end-to-end execution model, see [HOW_IT_WORKS.md](HOW_IT_WORKS.md). For implementation invariants, see [ARCHITECTURE.md](ARCHITECTURE.md). For configuration details, see [CONFIGURATION.md](CONFIGURATION.md). For security boundaries, see [../SECURITY.md](../SECURITY.md).

## 1. Operating model

Omarchy YOLO is a persistent local control plane. The daemon owns jobs; a terminal window does not.

```text
operator
  ↓
yolo CLI / Quickshell panel
  ↓
Unix-socket RPC
  ↓
omarchy-yolod
  ↓
SQLite state + scheduler + agents + Git worktrees
```

The safest default assumption is:

- the source checkout should remain clean;
- workers edit isolated worktrees, not the source checkout;
- the integration branch is the release candidate;
- `--apply` is optional and conservative;
- `completed` means the full acceptance path succeeded;
- cleanup failures do not rewrite already accepted task/job truth.

## 2. Install and verify

Install from the repository:

```bash
./install.sh
```

Then run:

```bash
yolo --version
yolo doctor
```

`yolo doctor` verifies the local Python/Linux/Git environment, confirms the daemon state, inspects configured agent executables, checks that required worker/planner/reviewer/integrator roles are satisfiable, and checks Bubblewrap when configured.

A healthy installation should have at least one usable agent and valid agents for all required roles.

Useful service commands on systemd-based Omarchy systems:

```bash
systemctl --user status omarchy-yolo.service
systemctl --user restart omarchy-yolo.service
journalctl --user -u omarchy-yolo.service -n 200
```

The CLI also attempts to start the user service automatically when the daemon socket is unavailable.

## 3. Before launching a job

From the target repository, check Git state first:

```bash
git status --short
git branch --show-current
git rev-parse HEAD
```

With the default `git.require_clean_repo = true`, the repository must be clean before a new job starts.

For an unfamiliar repository, decide which trust mode you want before launch:

- trusted/local project: native or Bubblewrap with normal networking;
- untrusted tracked contents or untrusted gate commands: hostile repository mode;
- deliberately malicious/exploit code: use a disposable VM/container rather than treating Bubblewrap as a kernel boundary.

See [CONFIGURATION.md](CONFIGURATION.md#recommended-profiles) for ready-to-copy profiles.

## 4. Launch a normal job

From a clean repository:

```bash
yolo run --watch \
  "Audit this repository, repair every material defect, run the complete test suite, and leave a release-ready candidate"
```

To target another repository:

```bash
yolo run --repo /path/to/repo --watch \
  "Implement the requested release and verify it end-to-end"
```

Without `--watch`, submission returns immediately while the daemon continues the job:

```bash
yolo run "Improve the parser and add regression coverage"
```

You can reconnect later with:

```bash
yolo status
yolo jobs
yolo watch <job-id>
```

## 5. Source application policy

The default is to leave the accepted result on the integration branch.

```bash
yolo run --no-apply --watch "Implement the change"
```

To request automatic application:

```bash
yolo run --apply --watch "Implement the change"
```

Auto-apply is fast-forward only. It is refused when the original source branch moved, became dirty, or is no longer at the recorded base commit. A refused auto-apply does not discard the candidate; the accepted integration branch remains available.

For unattended production-style use, `--no-apply` is the safer default because it separates autonomous acceptance from mutation of the operator's source branch.

## 6. Inspect job state

Show the latest job:

```bash
yolo status
```

Show a specific job:

```bash
yolo status <job-id>
```

Machine-readable form:

```bash
yolo status <job-id> --json
```

List recent jobs:

```bash
yolo jobs
yolo jobs --limit 20
yolo jobs --json
```

A normal status view includes the repository, goal, candidate integration branch, task counts, worker capacity, waiting workers, repository-lock activity, attempt telemetry, task state, preferred agents, final summary, and any terminal error.

## 7. Understand job states

Job states are:

| State | Meaning |
| --- | --- |
| `queued` | Schedulable or waiting to begin/resume. |
| `planning` | A planner is building the validated task DAG. |
| `running` | The DAG, reviews, gates or integration work is active. |
| `stopping` | A user stop was requested and bounded in-flight work is settling. |
| `stopped` | Explicitly stopped; it remains stopped across daemon restart until resumed. |
| `completed` | Final acceptance succeeded and durable completion was recorded. |
| `failed` | The job exhausted or hit an unrecoverable failure. |

`completed` is a terminal success state. A cleanup failure after completion is housekeeping and does not convert the job to failed.

## 8. Understand task states

Task states are:

| State | Meaning |
| --- | --- |
| `pending` | Waiting for dependencies, capacity or retry scheduling. |
| `running` | A worker attempt is executing. |
| `reviewing` | Worker output passed execution/gates and is under adversarial review. |
| `integrating` | Accepted task work is entering the integration transaction. |
| `completed` | The task was accepted and successfully integrated. |
| `failed` | The task exhausted its allowed attempts or hit a terminal failure. |
| `blocked` | A dependency or scheduler condition prevents execution. |
| `stopped` | Explicit job stop settled the in-flight task. |

Attempt states are `running`, `passed`, `failed`, and `cancelled`.

## 9. Watch durable events

Follow a job until terminal state:

```bash
yolo watch <job-id>
```

Inspect the durable event stream:

```bash
yolo events <job-id>
```

Page from a known event ID:

```bash
yolo events <job-id> --after 120 --limit 200
```

Machine-readable events:

```bash
yolo events <job-id> --json
```

Events are often the fastest way to understand where a job is spending time or why it changed state. Important families include planner events, task/attempt events, gate events, review events, integration/rollback events, recovery events, source-apply events, dossier publication, completion, and cleanup failures.

## 10. Inspect logs

List logs for a job:

```bash
yolo logs <job-id>
```

Tail the most recently modified log:

```bash
yolo logs <job-id> --latest
```

Increase the bounded tail size:

```bash
yolo logs <job-id> --latest --bytes 100000
```

By default terminal control sequences are stripped before output. `--raw` disables that sanitization:

```bash
yolo logs <job-id> --latest --raw
```

Use `--raw` only when you deliberately want the original terminal data.

## 11. Inspect configured agents

```bash
yolo agents
```

or:

```bash
yolo agents --json
```

The output identifies configured adapters, whether the executable is present, and effective role contracts.

Before an important unattended run, use both:

```bash
yolo doctor
yolo agents
```

This catches missing CLIs and role-contract mistakes before the job depends on them.

## 12. Stop a job safely

Request stop:

```bash
yolo stop <job-id>
```

A stop is durable. The daemon does not simply kill arbitrary control-plane work and release locks immediately. Bounded Git/process operations settle according to the cancellation rules, and integration is rolled back when required before schedulable state is exposed.

After stop:

```bash
yolo status <job-id>
yolo events <job-id>
```

If the job was already beyond final acceptance and a protected completion/apply transaction was finishing, accepted completion may legitimately win the stop race rather than producing an impossible state where the source was applied but the job was recorded as stopped.

## 13. Resume or retry

Resume a stopped or failed job:

```bash
yolo resume <job-id>
```

Resume and watch:

```bash
yolo resume <job-id> --watch
```

The job keeps its durable DAG/history. New attempts continue with monotonic attempt numbering; previous attempts and events are not erased.

## 14. Crash and daemon-restart recovery

If the daemon or machine stops unexpectedly, restart the user service or simply run a CLI command that needs the daemon.

On recovery, nonterminal work is reconstructed from SQLite. Planning/running jobs are re-queued, in-flight task attempts are closed as cancelled, interrupted tasks become schedulable again, and persisted worktrees are revalidated against canonical Git ownership and expected branches before reuse.

Explicitly stopped jobs remain stopped because `stop_requested` is durable.

After an unexpected restart:

```bash
yolo jobs
yolo status <job-id>
yolo events <job-id>
```

Do not manually delete YOLO worktrees while a recoverable job is still important unless you intend to abandon that recovery path.

## 15. Inspect the integration candidate

The integration branch name is shown by `yolo status`.

Typical inspection commands:

```bash
git log --oneline --decorate --all --graph
git diff <base-commit>..<integration-branch>
git show <integration-branch>
```

You may check out or merge the integration branch yourself after the job is terminal, but avoid manually mutating YOLO-owned branches/worktrees while the corresponding job is still running.

## 16. Execution dossiers

A completed, finally accepted job receives a deterministic execution dossier stored in SQLite.

Verify only and print its SHA-256:

```bash
yolo-dossier <job-id> --verify-only
```

Print canonical dossier JSON:

```bash
yolo-dossier <job-id>
```

Export it to a private file:

```bash
yolo-dossier <job-id> --output ~/private/yolo-job.json
```

The exporter verifies the stored SHA-256 before printing or writing. It refuses dossier export before final acceptance and refuses unsafe symlink output paths.

The dossier is provenance, not a digital signature. A user who can modify the SQLite state database can replace both dossier content and digest.

## 17. Quickshell panel

Toggle the panel:

```bash
yolo ui
```

The panel is a control/observation surface over the same daemon. Closing it does not stop jobs.

The widget exposes live task graph state, capacity/wait information, attempt telemetry, latest durable events, and stop/resume controls.

If the panel is not loaded:

```bash
yolo doctor
./install.sh
```

then verify the `dev.aether.yolo` plugin is enabled in Omarchy.

## 18. Trusted repository operating profile

A practical trusted-project profile is:

```toml
[engine]
execution_profile = "yolo-worktree"
auto_apply = false
cleanup_worktrees = true

[sandbox]
backend = "native"
network = true
hostile_repo_mode = false

[resources]
backend = "none"
```

For a stronger local boundary without hostile mode, use Bubblewrap:

```toml
[sandbox]
backend = "bwrap"
network = true
read_only_home = false
hostile_repo_mode = false
```

## 19. Hostile repository operation

For untrusted repository contents or gate commands:

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
```

This gives child profiles a fresh HOME/runtime view, private `/tmp`, filtered environments, and offline gates. Control-plane Git neutralizes repository-configured filter/process commands and custom merge drivers.

Remote coding CLIs may require network access. If you set `sandbox.network = true` in hostile mode, remember that explicitly allowlisted credentials remain readable by child code. Prefer the smallest possible environment allowlist.

For intentionally malicious code or exploit research, use a disposable VM/container. Bubblewrap and cgroups still share the host kernel.

## 20. Resource governance

Enable transient systemd user scopes:

```toml
[resources]
backend = "systemd"
memory_high_mib = 4096
memory_max_mib = 6144
tasks_max = 512
cpu_quota_percent = 300
io_weight = 100
```

These limits apply to agent and gate process trees and compose with Bubblewrap.

Choose limits that fit the repository. Compilers, linkers, browser tests and language servers can legitimately use substantial memory and process counts; excessively small limits create false failures.

## 21. Common failure patterns

### `yolo doctor` reports a missing agent

Check that the configured executable is on `PATH`:

```bash
command -v codex
command -v claude
command -v opencode
```

Then compare with the corresponding `[agents.NAME].command` in your config.

### Required role is unavailable

An installed executable is not enough. The adapter must also be eligible for the requested `worker`, `planner`, `reviewer`, or `integrator` role. Custom planner/reviewer adapters require a declared read-only `review_command`.

### Job remains pending

Inspect:

```bash
yolo status <job-id>
yolo events <job-id>
```

Typical causes are unsatisfied DAG dependencies, global worker capacity, repository-lock contention, or a previous task failure.

### Gate failure

Inspect the latest logs and gate events:

```bash
yolo logs <job-id> --latest
yolo events <job-id>
```

Fix the underlying project problem or resume if the job is in a resumable terminal state. Do not weaken gates solely to get an autonomous run green.

### Auto-apply skipped

This is normally protective behavior. Check whether the source branch moved or the working tree became dirty while YOLO was running. The integration candidate should remain available.

### Bubblewrap launch failure

Run:

```bash
yolo doctor
command -v bwrap
```

and verify your system permits the selected Bubblewrap behavior.

### systemd resource backend fails

Verify a working user systemd session:

```bash
systemctl --user status
command -v systemd-run
```

If the machine does not provide a usable systemd user scope environment, use `resources.backend = "none"` until the host is corrected.

### Database cannot start

Do not replace the SQLite database casually. Startup intentionally fails closed on corruption, unsafe symlink paths, unknown future schema versions, failed migrations, or failed integrity checks. Preserve the state directory before manual repair.

## 22. Operational safety rules

1. Keep the source checkout clean before submission.
2. Treat `--apply` as an explicit privilege, not the default.
3. Do not manually mutate YOLO-owned worktrees during a live job.
4. Prefer deterministic gates that represent the repository's real release contract.
5. Use capability-restricted reviewer/planner commands.
6. Use Bubblewrap for stronger local filesystem/network isolation.
7. Use hostile repository mode for untrusted repository code/gates.
8. Use cgroups/systemd scopes when runaway resource use matters.
9. Use a disposable VM/container for genuinely hostile code.
10. Preserve event/log/dossier evidence before destructive troubleshooting.

## 23. Recommended operating sequence

For an important unattended job:

```text
1. git status
2. yolo doctor
3. yolo agents
4. review configuration/trust mode
5. yolo run --no-apply --watch "goal"
6. inspect yolo status/events/logs
7. verify integration branch
8. verify/export yolo-dossier
9. manually fast-forward/merge, or use --apply only when desired
```

The central operational principle is simple: **agents propose and implement; the YOLO control plane decides what becomes accepted repository state.**
