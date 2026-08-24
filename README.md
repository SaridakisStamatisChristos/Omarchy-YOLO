# Omarchy YOLO

**Persistent, multi-agent autonomous software engineering for Omarchy Quattro.**

Omarchy YOLO adds a durable agent control plane on top of Omarchy instead of forking Arch, Hyprland,
or `omarchy-shell`. Give it a repository and an engineering goal; it plans a DAG, assigns isolated Git
worktrees to coding agents, runs repository gates, asks an adversarial reviewer to judge each candidate,
serializes integration, repairs conflicts, runs a final release audit, and leaves a candidate branch for you.

The normal mode is intentionally unattended. The source checkout is protected by default: workers operate
in separate worktrees and the integration branch is separate from your current branch. Automatic fast-forward
of the source branch is opt-in and is refused if the source branch has moved or become dirty.

> **New to Omarchy YOLO?** Read [How Omarchy YOLO Works](docs/HOW_IT_WORKS.md) for the complete step-by-step lifecycle from goal submission through planning, isolated execution, verification, integration, final audit, provenance, and optional source application.

> **YOLO does not mean root.** The daemon refuses to run as root. Unattended coding agents are powerful and
> can execute commands as your user. Read [SECURITY.md](SECURITY.md) before pointing this at untrusted code.

## Architecture

```text
                           yolo CLI / Omarchy panel
                                      │
                               Unix-socket RPC
                                      │
                              ┌────── agentd ──────┐
                              │ durable SQLite WAL │
                              │ scheduler / DAG    │
                              │ retries / recovery │
                              └─────────┬──────────┘
                                        │
                 ┌──────────────────────┼──────────────────────┐
                 │                      │                      │
              Planner                Workers                Reviewer
              read-only        Codex / Claude / OpenCode    read-only
                 │                      │                      │
                 └─────────────── task graph ─────────────────┘
                                        │
                  ┌─────────────────────┼─────────────────────┐
                  │                     │                     │
              worktree T1           worktree T2           worktree T3
                  │                     │                     │
              tests/gates           tests/gates           tests/gates
                  └──────────────┬──────┴──────┬──────────────┘
                                 │ serialized merge
                                 ▼
                         integration worktree
                                 │
                       post-merge gates/repair
                                 │
                         final adversarial audit
                                 │
                         yolo/<run>/integration
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for state transitions and recovery behavior.

## Features

- Persistent daemon with durable SQLite WAL state and event stream.
- Autonomous planner with validated acyclic task DAG and bounded task count.
- Parallel workers across isolated Git worktrees.
- Codex, Claude Code, OpenCode, and configurable custom CLI adapters.
- Per-attempt agent rotation and persistent attempt history.
- Automatic repository gate detection for common Python, JS/TS, Rust, Go, and Make projects.
- Adversarial per-task review and final release audit.
- Serialized integration with automatic conflict-repair agent cycles.
- Post-merge verification and rollback if integration damages the candidate branch.
- Crash recovery: interrupted workers become schedulable again; in-flight attempts are preserved as cancelled.
- Cancellation-safe, timeout-bounded Git topology mutations with deterministic integration rollback.
- Explicit stop/resume with a fresh retry budget while preserving monotonic attempt numbers.
- Optional Bubblewrap outer sandbox, including a stricter hostile-repository gate profile.
- Formal per-agent worker/planner/reviewer/integrator capability contracts.
- Durable scheduler, attempt, timing, wait, utilization, and recovery telemetry.
- Omarchy Quickshell bar widget with live task graph, capacity, attempt/event telemetry, stop, resume, and IPC controls.
- systemd user service; daemon refuses UID 0.
- Optional safe auto-apply via `git merge --ff-only` only when the source worktree is unchanged and clean.

## Requirements

- Linux; Omarchy Quattro for the shell panel integration.
- Python 3.12+.
- Git.
- At least one supported coding-agent CLI authenticated and on `PATH`.
- Optional: `bubblewrap` for an additional filesystem/network boundary.

The runtime has **no Python package dependencies outside the standard library**.

## Install on Omarchy

```bash
./install.sh
yolo doctor
```

The installer copies the Python package to `~/.local/share/omarchy-yolo`, installs the `yolo` launcher in
`~/.local/bin`, installs/enables the user service, copies the shell plugin to
`~/.config/omarchy/plugins/dev.aether.yolo`, and asks Omarchy to rescan/enable it when those commands exist.
It does not modify Omarchy's own source tree.

To uninstall:

```bash
./uninstall.sh
```

State is intentionally not deleted by default; see the uninstaller output if you also want to remove it.

## First run

From a **clean Git repository**:

```bash
yolo run --watch \
  "Audit this repository, repair every material defect you find, run the complete test suite, and leave a release-ready candidate"
```

The source branch remains untouched. Inspect the result:

```bash
yolo status
yolo jobs
yolo events <job-id>
yolo logs <job-id>
git log --oneline --decorate --all --graph
```

If you want YOLO to fast-forward the current source branch after every gate and audit passes:

```bash
yolo run --apply --watch "Implement the next release and verify it end-to-end"
```

`--apply` is intentionally conservative: the source branch must still be the original branch, clean, and at
exactly the original base commit. Otherwise the candidate stays on its integration branch and `job.apply_skipped`
is recorded.

## Commands

```text
yolo run [--repo PATH] [--apply|--no-apply] [--watch] GOAL
yolo status [JOB_ID] [--json]
yolo jobs [--limit N] [--json]
yolo events JOB_ID [--after ID] [--limit N] [--json]
yolo watch JOB_ID
yolo stop JOB_ID
yolo resume JOB_ID [--watch]
yolo agents [--json]
yolo doctor
yolo ui
yolo logs JOB_ID [--latest] [--bytes N]
```

## Agent profiles

The execution profile is configured under `[engine]`.

### `yolo-worktree` — default

Fully unattended workers, but Codex is kept in its workspace-write sandbox. Claude Code uses bypass-permissions
mode and OpenCode uses its current `--auto` mode. Git topology is still owned by the orchestrator. Planning and
review runs are forced read-only for Codex/Claude and receive explicit deny rules for OpenCode edits/shell/web.

```toml
[engine]
execution_profile = "yolo-worktree"
```

### `danger-yolo`

Explicitly switches Codex to its no-approval/no-sandbox mode. Use only when you deliberately want the agent CLI
itself unconstrained by Codex's sandbox. The daemon still runs as your ordinary user and still uses worktrees.

```toml
[engine]
execution_profile = "danger-yolo"
```

If you want a stronger outer boundary while retaining unattended behavior, use Bubblewrap instead of relying
only on the agent CLI's own permission model:

```toml
[sandbox]
backend = "bwrap"
network = true
read_only_home = false
```

See [SECURITY.md](SECURITY.md) for the exact trust boundaries and the stricter `read_only_home` option.

### Hostile repository mode

For a repository whose tracked files and gate commands are not trusted, enable the stricter local boundary:

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

This mode is deliberately opt-in. All child profiles receive a fresh HOME/runtime view and filtered environment.
Only `writable_home_paths` are re-exposed to agents (writable for workers/integrators, read-only for
planners/reviewers); gates receive no HOME exceptions and no network. Control-plane Git neutralizes configured
clean/smudge/process filters and merge drivers. Agent network follows `sandbox.network`, because remote model CLIs
may require it. A disposable VM remains the appropriate boundary for code that may exploit the kernel or an allowed
agent CLI.

## Configuration

Copy/edit `config.example.toml` or the installer-created `~/.config/omarchy-yolo/config.toml`.

```toml
[engine]
max_parallel = 4
max_attempts = 3
max_final_cycles = 2
max_tasks = 12
planner_agent = "codex"
reviewer_agent = "claude"
integrator_agent = "codex"
worker_agents = ["codex", "claude", "opencode"]
auto_apply = false
cleanup_worktrees = true
execution_profile = "yolo-worktree"

[git]
command_timeout_seconds = 120
allow_repository_commands = true

[gates]
# Empty arrays mean auto-detect from the repository.
commands = []
final_commands = []
```

Agent commands are data, not hard-coded adapters:

```toml
[agents.codex]
enabled = true
command = ["codex", "exec", "--full-auto", "--sandbox", "workspace-write"]

[agents.claude]
enabled = true
command = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"]
```

A custom CLI can be added as another `[agents.NAME]` table if it accepts the prompt as its final argument and
returns useful stdout. Structured planner/reviewer roles must return the JSON contract described in
`src/omarchy_yolo/prompts.py`. An optional `roles` allowlist makes the capability boundary explicit:

```toml
[agents.local]
command = ["my-agent", "--write"]
review_command = ["my-agent", "--read-only"]
roles = ["worker", "planner", "reviewer", "integrator"]
```

A dedicated review-only adapter may omit `command` and declare only `review_command` plus planner/reviewer roles.
`yolo doctor` fails if no available adapter satisfies a role required by the pipeline.

## Gates

If `[gates].commands` is empty, YOLO detects a small conservative set of repository checks. You can make the
contract exact for a project:

```toml
[gates]
commands = [
  "python -m pytest -q",
  "python -m compileall -q src",
]
final_commands = [
  "python -m pytest -q",
]
```

Commands execute directly through `/bin/bash -lc` in the assigned worktree, with a timeout and process-group
termination so descendants do not survive a stop/timeout.

## Recovery model

The daemon persists every job, task, attempt, and event. On restart:

1. nonterminal jobs are requeued;
2. tasks interrupted while running/reviewing/integrating return to `pending`;
3. corresponding `running` attempt records are closed as `cancelled` rather than deleted;
4. an in-progress Git merge is aborted before scheduling resumes;
5. a dirty integration worktree is hard-reset to its current HEAD;
6. a resumed failed task receives a fresh configured attempt budget with monotonic attempt numbers.

This is deliberately **at-least-once task execution**, with Git commits/worktrees providing the practical
idempotency boundary. Cancellation cannot release a repository lock until a blocking Git mutation has terminated;
an interrupted integration transaction is rolled back before durable task state becomes schedulable again.

## Omarchy shell integration

The plugin is a normal third-party Omarchy shell plugin. It does not patch `omarchy-shell`.

- Plugin ID: `dev.aether.yolo`
- Kind: `bar-widget`
- IPC target: `dev.aether.yolo`
- Toggle from CLI: `yolo ui`

The widget polls the daemon through `yolo status --json`, shows the current task graph, active review/integration
work, scheduler capacity/waiters, attempt durations, state age, failures and the latest durable event, and exposes
stop/resume.
The daemon remains independent of the UI; closing the panel or terminal does not terminate jobs.

## Development

```bash
./scripts/check.sh
```

Or:

```bash
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m compileall -q src
bash -n install.sh uninstall.sh scripts/*.sh
python -m json.tool shell-plugin/manifest.json >/dev/null
```

The test suite uses fake agents and temporary real Git repositories, so it verifies the orchestrator without
requiring Codex/Claude/OpenCode credentials.

## Design principles

1. **Autonomous by default, not root by default.**
2. **Git is the transaction boundary.** Workers never share a working tree.
3. **Integration is serialized.** Parallel coding should not mean parallel merges.
4. **Every candidate proves itself.** Worker success alone is never acceptance.
5. **State survives terminals and reboots.** The daemon owns jobs, not a shell pane.
6. **No silent mutation of the user's branch.** Auto-apply is explicit and conditional.
7. **Degrade rather than deadlock.** A failed planner falls back to a bounded single implementation task.
8. **Preserve forensic history.** Attempts and event records are durable across retries/restarts.

## License

MIT. See [LICENSE](LICENSE).