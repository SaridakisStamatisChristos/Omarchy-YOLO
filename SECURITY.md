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

## v1.1 trust boundaries

Planner and reviewer roles are capability-gated. Codex, Claude and OpenCode use explicit read-only/plan/deny
profiles. Built-in review trust is bound to both the adapter name and its expected executable identity; replacing
a built-in command with a wrapper or unrelated executable requires an explicit `review_command`. A custom adapter
is **not** eligible for planning or review simply because it can execute: it must declare a separate
`review_command` whose non-mutating behavior is the operator's responsibility.

With the Bubblewrap backend, planner/reviewer processes always see HOME read-only **and the repository worktree is
mounted read-only at the OS layer**. This provides defense in depth behind the CLI-specific plan/read-only policy.
Worker mode may still receive a writable HOME for compatibility, or `read_only_home = true` can be combined with
`writable_home_paths` to expose only explicitly selected credential/session directories. This is a filesystem
boundary, not a promise that an allowed networked CLI cannot exfiltrate credentials it can legitimately read.

Repository topology is coordinated across jobs. A daemon-wide resource coordinator limits total active agent
work, and operations that mutate shared worktree/ref topology are serialized by resolved repository root. This
reduces race/corruption risk when multiple jobs target the same repository; it does not make two conflicting user
goals semantically compatible.

Final release review is fail-closed. Every changed file must be represented in a bounded review shard; oversized
file counts, per-file diffs or total reviewed bytes fail finalization instead of silently truncating the audit.
Git filename bytes are round-tripped with the platform filesystem encoding so non-UTF-8 filenames cannot silently
fall out of review coverage. Shard reviews must all pass before the synthesis reviewer can approve the complete
candidate, and every non-pass verdict must contain actionable summary/finding text.

The daemon and CLI bound IPC messages, model prompts/output, event payloads, Git review data, gate/process capture,
and log growth. Durable process logs are opened before child launch; later log-write storage failures terminate
the child process group and propagate to orchestration rather than being ignored. Worktree deletion is fail-closed
unless Git proves that the target belongs to the expected repository, and orchestrator Git commands suppress
repository hooks. UI/terminal surfaces treat agent-controlled text as plain/untrusted text. SQLite startup runs an
integrity quick-check and refuses corrupt state. Post-release cleanup is housekeeping: if cleanup fails after a
candidate has already passed every gate/review and is durably marked complete, the failure is recorded without
rewriting the completed release result.

These controls reduce accidental blast radius; they do not turn `danger-yolo` into a security sandbox. Repository
gates intentionally execute project code, and unattended coding CLIs may have broader user-level access unless
Bubblewrap (or a disposable VM/container) is used. Never treat a Git worktree boundary as an OS security boundary
unless an actual sandbox policy such as the Bubblewrap review mount is enforcing it.
