# Security model

Omarchy YOLO is intentionally capable of launching coding agents without interactive approval. That does **not**
mean workers need root access. The daemon refuses to run as root and never calls `sudo` or `pkexec` on behalf of
an agent.

The default `yolo-worktree` profile gives agents autonomous control over isolated Git worktrees. Codex uses its
workspace sandbox. Claude Code's bypass-permissions mode is less strongly bounded by itself; use the optional
`bwrap` backend when you need an OS-enforced boundary around heterogeneous agents.

Do not run untrusted repositories with network-enabled agents unless you accept prompt-injection and
credential-exfiltration risk. For hostile code, use a disposable VM/container and disable network.

The final integration branch is preserved even when `auto_apply = false`. Automatic application uses
fast-forward only and is refused if the source branch moved or the user's working tree is dirty.

## v1.2 trust boundaries

Planner and reviewer roles are capability-gated. Codex, Claude and OpenCode use explicit read-only/plan/deny
profiles. Built-in review trust is bound to both the adapter name and its expected executable identity; replacing
a built-in command with a wrapper or unrelated executable requires an explicit `review_command`. A custom adapter
is **not** eligible for planning or review simply because it can execute: it must declare a separate
`review_command` whose non-mutating behavior is the operator's responsibility.

With the Bubblewrap backend, planner/reviewer processes see HOME and the repository worktree read-only at the OS
layer. Worker mode may receive explicitly configured writable HOME subpaths. Gate mode is separate: the repository
cwd remains writable so test/build tools can produce normal workspace artifacts, while host filesystem/HOME are
read-only and network access follows `sandbox.network`. These are filesystem/network boundaries, not a promise
that a permitted networked process cannot exfiltrate credentials it can legitimately read.

Repository topology is coordinated across jobs. A daemon-wide resource coordinator limits total active agent
work, and operations that mutate shared worktree/ref topology are serialized by resolved repository root. This
reduces race/corruption risk when multiple jobs target the same repository; it does not make conflicting user goals
semantically compatible.

Final release review is fail-closed. Every changed file must be represented in bounded review input. Textual
changes are reviewed as file-local shards; a multi-shard file must then pass a file-level semantic synthesis before
its result can enter the final cross-file synthesis. Hard ceilings fail finalization rather than truncate review.
Git filename bytes are round-tripped with the platform filesystem encoding so non-UTF-8 filenames cannot silently
fall out of review coverage. A `pass` verdict must include a meaningful semantic summary.

Binary changes are rejected by default because the reviewer cannot inspect their semantic contents. Setting
`engine.final_review_allow_binary = true` is an explicit opt-in to metadata-only binary review; it does not make
binary content semantically audited. Treat that option as a deliberate reduction in assurance.

The daemon and CLI bound IPC messages, model prompts/output, event payloads, Git review data, gate/process capture,
and log growth. Durable worker and gate logs are opened before child launch. Later log-write/storage failures
terminate the relevant process group and propagate to orchestration rather than being ignored. Worktree deletion
is fail-closed unless Git proves the target belongs to the expected repository, and orchestrator Git commands
suppress repository hooks. UI/terminal surfaces treat agent-controlled text as plain/untrusted text. SQLite startup
runs an integrity quick-check and refuses corrupt state or symlinked database paths.

Post-integration cleanup is housekeeping. If cleanup fails after a task has already integrated successfully, the
failure is recorded as `task.cleanup_failed` without rewriting the accepted task/job result. Likewise, post-release
cleanup cannot retroactively turn an already completed candidate into a failed release.

CI is part of the trust boundary. Development dependencies are version-pinned and SHA-256 artifact-pinned for the
supported Python 3.12/3.13 Linux matrix and installed with `pip --require-hashes`. The built wheel is installed and
executed in a clean virtual environment. Aggregate branch coverage has a 75% floor, with additional module floors
on Git, gates/process, database, runtime/sandbox, orchestration and review code.

These controls reduce accidental blast radius; they do not turn `danger-yolo` into a security sandbox. Repository
gates intentionally execute project code, and unattended coding CLIs may have broader user-level access unless
Bubblewrap (or a disposable VM/container) is used. Never treat a Git worktree boundary as an OS security boundary
unless an actual sandbox policy is enforcing it.
