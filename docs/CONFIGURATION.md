# Omarchy YOLO Configuration Reference

This document is the authoritative operator-facing reference for Omarchy YOLO v1.4.2 configuration.

The example file is [`config.example.toml`](../config.example.toml). Operational procedures are in [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md), lifecycle behavior is in [HOW_IT_WORKS.md](HOW_IT_WORKS.md), and trust boundaries are in [../SECURITY.md](../SECURITY.md).

## 1. Configuration location

The default configuration path is:

```text
$XDG_CONFIG_HOME/omarchy-yolo/config.toml
```

or, when `XDG_CONFIG_HOME` is unset:

```text
~/.config/omarchy-yolo/config.toml
```

Override it with:

```bash
export OMARCHY_YOLO_CONFIG=/path/to/config.toml
```

The default state directory is:

```text
$XDG_STATE_HOME/omarchy-yolo
```

or:

```text
~/.local/state/omarchy-yolo
```

Override it with:

```bash
export OMARCHY_YOLO_STATE_DIR=/path/to/state
```

The state directory contains the SQLite database, worktrees, logs, migration backups, and related durable runtime state. Protect it as sensitive local engineering state.

After changing configuration for a persistent daemon, restart the user service so the new process loads the intended settings:

```bash
systemctl --user restart omarchy-yolo.service
yolo doctor
```

## 2. Complete section map

```toml
[safety]
[engine]
[git]
[gates]
[sandbox]
[resources]
[agents.codex]
[agents.claude]
[agents.opencode]
[agents.gemini]
[agents.<custom-name>]
```

Unknown TOML sections are not part of the supported contract. Use the names documented here.

## 3. `[safety]`

The safety section names the intended trust policy and, for the hostile preset, applies and enforces a coherent set of settings across sandbox, network and Git controls.

### `preset`

```toml
preset = "trusted-local"
```

- Allowed: `"trusted-local"`, `"hostile-repo"`, `"custom"`
- Default: `"trusted-local"`

`trusted-local` preserves the compatibility defaults and is appropriate only when repository contents and configured gates are trusted. `hostile-repo` selects Bubblewrap, hostile repository mode, read-only/masked HOME, offline review and gates, and `git.allow_repository_commands = false`; an explicit conflicting override is rejected. `custom` records that the operator intentionally assembled a policy from the lower-level settings below.

`yolo doctor` reports both this configured label and the effective posture computed from the resulting settings. A configured label never upgrades a weaker effective posture.

## 4. `[engine]`

The engine section controls scheduling, retries, role selection, execution mode, source application, cleanup, and final-review bounds.

### `max_parallel`

```toml
max_parallel = 4
```

- Type: integer
- Default: `4`
- Range: `1..64`
- Meaning: maximum task-level parallelism one job may consume.

This is a per-job ceiling. It does not override daemon-wide capacity.

### `max_global_workers`

```toml
max_global_workers = 4
```

- Type: integer
- Default: `4`
- Range: `1..128`
- Meaning: daemon-wide worker/planner/final-audit capacity shared by all jobs.

If several jobs run at once, they compete for this shared capacity.

### `max_attempts`

```toml
max_attempts = 3
```

- Type: integer
- Default: `3`
- Range: `1..20`
- Meaning: bounded retry budget for task execution/review repair cycles.

Higher values increase recovery opportunities but also increase time and model usage.

### `max_final_cycles`

```toml
max_final_cycles = 2
```

- Type: integer
- Default: `2`
- Range: `0..20`
- Meaning: number of bounded repair cycles available after final repository verification/review exposes defects.

`0` disables final repair cycles while retaining final verification.

### `max_tasks`

```toml
max_tasks = 12
```

- Type: integer
- Default: `12`
- Range: `1..256`
- Meaning: maximum number of planner-generated tasks accepted in a DAG.

The planner output is rejected when it exceeds this bound.

### `agent_timeout_seconds`

```toml
agent_timeout_seconds = 3600
```

- Type: integer
- Default: `3600`
- Range: `1..86400`
- Meaning: finite runtime limit for one agent invocation.

Choose this high enough for repository-scale agents but finite enough to bound unattended execution.

### `gate_timeout_seconds`

```toml
gate_timeout_seconds = 1800
```

- Type: integer
- Default: `1800`
- Range: `1..86400`
- Meaning: finite runtime limit for one repository gate command.

### `planner_agent`

```toml
planner_agent = "codex"
```

- Type: safe agent identifier
- Default: `"codex"`
- Meaning: preferred adapter for DAG planning.

The selected adapter must be eligible for the `planner` role. If selection/fallback cannot produce a valid planner, `doctor` reports the role problem.

### `reviewer_agent`

```toml
reviewer_agent = "claude"
```

- Type: safe agent identifier
- Default: `"claude"`
- Meaning: preferred adapter for per-task and final adversarial review.

The adapter must be reviewer-capable and use an accepted read-only review profile/command.

### `integrator_agent`

```toml
integrator_agent = "codex"
```

- Type: safe agent identifier
- Default: `"codex"`
- Meaning: preferred adapter for conflict/post-integration repair work.

### `worker_agents`

```toml
worker_agents = ["codex", "claude", "opencode"]
```

- Type: non-empty array of safe agent identifiers
- Default: `[`codex`, `claude`, `opencode`]`
- Meaning: ordered pool available for implementation attempts and agent rotation.

### `auto_apply`

```toml
auto_apply = false
```

- Type: boolean
- Default: `false`
- Meaning: default source-application policy when the CLI does not explicitly use `--apply` or `--no-apply`.

When enabled, application remains fast-forward only and is refused if the source branch moved or became dirty.

### `cleanup_worktrees`

```toml
cleanup_worktrees = true
```

- Type: boolean
- Default: `true`
- Meaning: remove completed task/release worktrees as housekeeping after accepted state is durable.

Cleanup failure is recorded but does not retroactively invalidate accepted work.

### `execution_profile`

```toml
execution_profile = "yolo-worktree"
```

- Type: string
- Allowed: `"yolo-worktree"`, `"danger-yolo"`
- Default: `"yolo-worktree"`

`yolo-worktree` keeps the normal unattended adapter permission model. `danger-yolo` deliberately enables the least constrained built-in worker behavior where supported. It does not make the daemon root and does not replace OS-level sandboxing.

Prefer `yolo-worktree` unless you have a concrete reason to relax the agent CLI's own sandbox.

### `final_review_chunk_bytes`

```toml
final_review_chunk_bytes = 60000
```

- Type: integer
- Default: `60000`
- Range: `8000..120000`
- Meaning: byte budget used when splitting large textual file diffs into file-local review shards.

### `final_review_max_files`

```toml
final_review_max_files = 512
```

- Type: integer
- Default: `512`
- Range: `1..4096`
- Meaning: hard changed-file ceiling for final review.

Exceeding the ceiling fails closed rather than silently dropping files from review.

### `final_review_allow_binary`

```toml
final_review_allow_binary = false
```

- Type: boolean
- Default: `false`
- Meaning: whether binary changes may proceed through metadata-only review.

`false` is the high-assurance default. Setting `true` does **not** make binary content semantically reviewed; it intentionally lowers assurance to metadata-only treatment.

## 5. `[git]`

The Git section controls repository preconditions, naming, control-plane identity, subprocess bounds, and whether repository-defined command hooks/filters may execute during orchestrator Git operations.

### `require_clean_repo`

```toml
require_clean_repo = true
```

- Type: boolean
- Default: `true`
- Meaning: require a clean source checkout when starting a job.

Keep this enabled for predictable source protection.

### `branch_prefix`

```toml
branch_prefix = "yolo"
```

- Type: validated Git-ref prefix
- Default: `"yolo"`
- Maximum length: 64 characters

Unsafe ref syntax, repeated separators, `..`, `@{`, trailing `.lock`, and other invalid forms are rejected.

### `commit_name`

```toml
commit_name = "Omarchy YOLO"
```

- Type: non-empty string
- Default: `"Omarchy YOLO"`
- Maximum: 200 characters

Used for orchestrator-owned control-plane Git commits.

### `commit_email`

```toml
commit_email = "omarchy-yolo@localhost"
```

- Type: non-empty string
- Default: `"omarchy-yolo@localhost"`
- Maximum: 320 characters

### `command_timeout_seconds`

```toml
command_timeout_seconds = 120
```

- Type: integer
- Default: `120`
- Range: `5..900`
- Meaning: timeout for control-plane Git subprocesses.

Git output is separately bounded and descendant processes are terminated with the command group.

### `allow_repository_commands`

```toml
allow_repository_commands = true
```

- Type: boolean
- Default: `true` in normal mode
- Default in hostile mode when omitted: `false`

Git repositories may configure clean/smudge/process filters and merge drivers that execute commands. Set this to `false` for untrusted repositories.

When `sandbox.hostile_repo_mode = true`, this option **must** be `false`.

## 6. `[gates]`

Repository gates are deterministic commands that must pass before candidate acceptance.

### `commands`

```toml
commands = []
```

- Type: array of shell command strings
- Default: empty
- Meaning: task-level gate commands.

An empty list means YOLO performs conservative repository gate auto-detection.

Example:

```toml
[gates]
commands = [
  "python -m pytest -q",
  "python -m compileall -q src",
]
```

### `final_commands`

```toml
final_commands = []
```

- Type: array of shell command strings
- Default: empty
- Meaning: gates run against the complete integration candidate before final semantic release review.

Example:

```toml
final_commands = [
  "python -m pytest -q",
  "python -m mypy src",
]
```

Gate commands execute through `/bin/bash -lc` in the assigned repository worktree and inherit the configured timeout, process-group termination, sandbox, environment, and cgroup resource policy.

Do not put secrets directly in gate command strings.

## 7. `[sandbox]`

The sandbox section controls outer child-process isolation. It is separate from Git worktree isolation and from cgroup resource governance.

### `backend`

```toml
backend = "native"
```

- Allowed: `"native"`, `"none"`, `"bwrap"`
- Default: `"native"`

Meaning:

- `native`: rely on the configured agent's native non-interactive permission model plus YOLO's process/Git boundaries;
- `none`: no external confinement layer beyond normal user/process controls;
- `bwrap`: wrap workers/reviewers/planners/integrators/gates in Bubblewrap profiles.

`bwrap` is required for hostile repository mode.

### `network`

```toml
network = true
```

- Type: boolean
- Default: `true`

Controls agent/gate network exposure according to the active Bubblewrap profile. In hostile mode, gates are forced offline even if remote agents need network access.

### `review_network`

```toml
review_network = false
```

- Type: optional boolean
- Default: inherit `sandbox.network`

Overrides network exposure for planning and semantic review profiles. The `hostile-repo` preset sets this to `false` and rejects `true`.

### `gate_network`

```toml
gate_network = false
```

- Type: optional boolean
- Default: inherit `sandbox.network`

Overrides network exposure for deterministic task/final gates. Hostile repository mode keeps gates offline even when this key is omitted; the `hostile-repo` preset sets it to `false` and rejects `true`.

### `read_only_home`

```toml
read_only_home = false
```

- Type: boolean
- Default: `false`

With Bubblewrap, planner/reviewer access is read-only by policy. This setting can further constrain worker-side HOME behavior, with selected writable paths re-exposed through `writable_home_paths`.

### `writable_home_paths`

```toml
writable_home_paths = []
```

- Type: array of relative HOME paths
- Default: empty

Entries must stay inside HOME; `..` traversal and absolute-style escapes are rejected.

Use this only for directories that an agent CLI genuinely needs for session/cache/auth state.

Example:

```toml
writable_home_paths = [".codex", ".cache/my-agent"]
```

In hostile mode, these are the only HOME paths selectively re-exposed to agent profiles. Gates receive no HOME exceptions.

### `hostile_repo_mode`

```toml
hostile_repo_mode = false
```

- Type: boolean
- Default: `false`

When true:

- `backend` must be `"bwrap"`;
- `git.allow_repository_commands` must be `false`;
- child HOME/runtime views are replaced;
- `/tmp` is private;
- gate environment is filtered;
- agent environment is filtered;
- gates are forced offline;
- configured HOME exceptions are tightly scoped by role.

This is a strong local hardening mode, not a kernel/VM isolation guarantee.

### `gate_env_allowlist`

```toml
gate_env_allowlist = []
```

- Type: array of valid environment-variable names
- Default: empty
- Used primarily by hostile repository mode.

Only allow variables a gate truly needs. Avoid credentials whenever possible.

### `agent_env_allowlist`

```toml
agent_env_allowlist = []
```

- Type: array of valid environment-variable names
- Default: empty
- Used primarily by hostile repository mode.

Remote model CLIs may require provider credentials. Allow only the minimum necessary names. Any allowlisted secret is readable by child code within that profile.

## 8. `[resources]`

Resource governance is independent of filesystem/network isolation.

### `backend`

```toml
backend = "none"
```

- Allowed: `"none"`, `"systemd"`
- Default: `"none"`

`systemd` launches each agent and gate process tree inside a transient systemd user scope backed by cgroups v2.

### `memory_high_mib`

```toml
memory_high_mib = 0
```

- Type: integer
- Default: `0` (disabled)
- Range: `0..1048576`

Soft/high memory pressure threshold in MiB.

If both memory limits are non-zero, `memory_high_mib` must not exceed `memory_max_mib`.

### `memory_max_mib`

```toml
memory_max_mib = 0
```

- Type: integer
- Default: `0` (disabled)
- Range: `0..1048576`

Hard memory ceiling in MiB.

### `tasks_max`

```toml
tasks_max = 0
```

- Type: integer
- Default: `0` (disabled)
- Range: `0..1000000`

Hard process/thread count for each child scope. Useful against accidental or hostile fork storms.

### `cpu_quota_percent`

```toml
cpu_quota_percent = 0
```

- Type: integer
- Default: `0` (disabled)
- Range: `0..10000`

Examples:

- `100` = approximately one full CPU worth of quota;
- `300` = approximately three CPUs worth of quota.

### `io_weight`

```toml
io_weight = 0
```

- Type: integer
- Default: `0` (unchanged/default)
- Range accepted by configuration: `0..10000`
- Active cgroups-v2 weight: use `1..10000`.

## 9. `[agents.NAME]`

Agent adapters are configuration data rather than provider SDK integrations.

Built-in names are:

- `codex`
- `claude`
- `opencode`
- `gemini`

Custom names are allowed when they use a safe identifier.

### `enabled`

```toml
enabled = true
```

- Type: boolean
- Built-in default: true for Codex/Claude/OpenCode, false for Gemini
- Custom default: true

Disabled adapters remain configured but are not eligible for execution.

### `command`

```toml
command = ["codex", "exec", "--full-auto", "--sandbox", "workspace-write"]
```

- Type: argv array
- Meaning: worker/integrator execution command.

Default built-in commands are:

```toml
[agents.codex]
command = ["codex", "exec", "--full-auto", "--sandbox", "workspace-write"]

[agents.claude]
command = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"]

[agents.opencode]
command = ["opencode", "run", "--format", "json", "--auto"]

[agents.gemini]
command = ["gemini", "-p"]
```

The prompt is appended according to the adapter protocol. Do not put credentials directly into argv; prefer environment variables and the smallest appropriate environment exposure.

### `review_command`

```toml
review_command = ["my-agent", "--read-only"]
```

- Type: argv array
- Default: empty
- Meaning: explicitly non-mutating planner/reviewer command for custom adapters.

Unknown/custom worker commands are **not** automatically trusted as read-only. To make a custom adapter eligible for planner/reviewer roles, provide `review_command`.

Built-in adapters have verified role-specific transformations and may not require an explicit entry here.

### `roles`

```toml
roles = ["worker", "planner", "reviewer", "integrator"]
```

- Type: array
- Allowed values: `worker`, `planner`, `reviewer`, `integrator`
- Default: empty, which leaves built-in effective-role inference in place.

Use this as a least-privilege allowlist.

Examples:

Worker only:

```toml
[agents.fast-worker]
enabled = true
command = ["my-agent", "--write"]
roles = ["worker"]
```

Review only:

```toml
[agents.audit-only]
enabled = true
review_command = ["my-reviewer", "--read-only"]
roles = ["planner", "reviewer"]
```

Full custom adapter:

```toml
[agents.local]
enabled = true
command = ["my-agent", "--write"]
review_command = ["my-agent", "--read-only"]
roles = ["worker", "planner", "reviewer", "integrator"]
```

After any agent change:

```bash
yolo doctor
yolo agents
```

## 10. Recommended profiles

These examples are starting points, not universal resource prescriptions.

### Trusted local development

```toml
[safety]
preset = "trusted-local"

[engine]
max_parallel = 4
max_global_workers = 4
max_attempts = 3
max_final_cycles = 2
auto_apply = false
execution_profile = "yolo-worktree"

[git]
require_clean_repo = true
allow_repository_commands = true

[sandbox]
backend = "native"
network = true
hostile_repo_mode = false

[resources]
backend = "none"
```

### Trusted project with Bubblewrap

```toml
[safety]
preset = "custom"

[engine]
auto_apply = false
execution_profile = "yolo-worktree"

[sandbox]
backend = "bwrap"
network = true
read_only_home = false
hostile_repo_mode = false

[resources]
backend = "none"
```

### Hostile/untrusted repository

```toml
[safety]
preset = "hostile-repo"

[engine]
auto_apply = false
execution_profile = "yolo-worktree"

[sandbox]
network = false
writable_home_paths = []
gate_env_allowlist = []
agent_env_allowlist = []

[resources]
backend = "systemd"
memory_high_mib = 4096
memory_max_mib = 6144
tasks_max = 512
cpu_quota_percent = 300
io_weight = 100
```

Adjust resource limits to the project and machine. A large Rust/C++ build may legitimately need more memory/processes than a small Python package.

### Remote-agent hostile repository

If a remote model CLI requires networking and one credential variable:

```toml
[safety]
preset = "hostile-repo"

[sandbox]
network = true
writable_home_paths = []
gate_env_allowlist = []
agent_env_allowlist = ["PROVIDER_API_KEY"]
```

This preserves offline review/gates while allowing worker/integrator agent profiles to reach their provider. The credential is necessarily readable by those child processes, so this is a deliberate trust trade-off.

## 11. Configuration interactions that fail closed

The loader rejects important unsafe or nonsensical combinations.

Examples:

```text
hostile_repo_mode = true
backend != "bwrap"
→ rejected
```

```text
hostile_repo_mode = true
allow_repository_commands = true
→ rejected
```

```text
memory_high_mib > memory_max_mib
when both are enabled
→ rejected
```

```text
execution_profile not in {yolo-worktree, danger-yolo}
→ rejected
```

```text
resource backend not in {none, systemd}
→ rejected
```

```text
sandbox backend not in {native, none, bwrap}
→ rejected
```

Unsafe branch prefixes, invalid environment-variable names, invalid agent identifiers, invalid role names, out-of-range integer values, and non-boolean boolean fields are also rejected rather than coerced silently.

## 12. CLI overrides versus configuration

For source application, the CLI can override the configured default per job:

```bash
yolo run --apply "goal"
yolo run --no-apply "goal"
```

If neither is supplied, `engine.auto_apply` controls the submitted job.

Most other runtime behavior is loaded from the daemon's configuration, so restart the service after changing those values.

## 13. Tuning guidance

### Parallelism

Start with:

```toml
max_parallel = 4
max_global_workers = 4
```

Increase only when CPU/RAM and agent-provider limits comfortably support it. More parallel agents can increase integration conflicts and resource contention.

### Attempts

`3` is a strong default. Raising `max_attempts` is useful when repair cycles routinely converge, but repeated model retries cannot compensate for a broken deterministic gate or impossible goal.

### Final cycles

Keep at least one final repair cycle for autonomous release work. Set to zero only when you deliberately want final defects to stop rather than repair.

### Gate timeouts

Measure the repository's slowest legitimate release checks and leave headroom. Do not use effectively infinite timeouts.

### Review limits

Prefer the defaults until a real repository exceeds them. The ceilings are security/reliability boundaries; raising them increases model context and review cost.

### Resource limits

Use cgroups when runaway builds or hostile process trees are a concern. Avoid limits so low that normal compilers or test runners fail spuriously.

## 14. Security guidance

Configuration cannot turn local agents into a perfect hostile-code sandbox.

Important distinctions:

```text
Git worktree isolation
≠ OS security isolation

Bubblewrap
≠ VM/kernel isolation

cgroups
≠ filesystem/network isolation

execution dossier SHA-256
≠ digital signature
```

For genuinely malicious code, use a disposable VM/container in addition to YOLO's local safeguards.

## 15. Validate after editing

After changing configuration:

```bash
systemctl --user restart omarchy-yolo.service
yolo doctor
yolo agents
yolo status
```

For a security-sensitive change, also inspect the effective behavior with a disposable test repository before entrusting a large unattended job to the new profile.

## 16. Canonical example

The repository's complete supported example remains [`config.example.toml`](../config.example.toml). Keep that file and this reference synchronized when adding or changing configuration fields.
