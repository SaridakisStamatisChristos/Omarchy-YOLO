# Security model

Omarchy YOLO is intentionally capable of launching coding agents without interactive approval. That does **not**
mean workers need root access. The daemon refuses to run as root and never calls `sudo` or `pkexec` on behalf of
an agent.

The default `yolo-worktree` profile gives agents autonomous control over isolated Git worktrees. Codex uses its
workspace sandbox. Claude Code's bypass-permissions mode is less strongly bounded by itself; use the optional
`bwrap` backend when you need an OS-enforced boundary around heterogeneous agents.

Do not run untrusted repositories with network-enabled agents unless you accept prompt-injection and
credential-exfiltration risk. Hostile-repository mode materially narrows local exposure, but hostile code that may
exploit the shared kernel or an allowed agent CLI still belongs in a disposable VM/container.

The final integration branch is preserved even when `auto_apply = false`. Automatic application uses
fast-forward only and is refused if the source branch moved or the user's working tree is dirty.

## v1.4 trust boundaries

Planner and reviewer roles are capability-gated. Codex, Claude and OpenCode use explicit read-only/plan/deny
profiles. Built-in review trust is bound to both the adapter name and its expected executable identity; replacing
a built-in command with a pathed wrapper or unrelated executable requires an explicit `review_command`. Optional
role allowlists restrict adapters to worker/planner/reviewer/integrator capabilities; review-only adapters do not
need a worker command.

With the Bubblewrap backend, planner/reviewer processes see HOME and the repository worktree read-only at the OS
layer. Worker mode may receive explicitly configured writable HOME subpaths. Gate mode keeps repository cwd
writable for build artifacts while host filesystem/HOME are read-only and networking follows policy.

The recommended `[safety] preset = "hostile-repo"` fails closed unless Bubblewrap, masked/read-only HOME,
offline review/gates, and neutralized repository Git commands remain effective; `yolo doctor` reports both the
configured preset and effective posture. Its underlying `sandbox.hostile_repo_mode` strengthens every child profile. HOME, `XDG_RUNTIME_DIR`, and
`/tmp` are replaced with fresh mounts and inherited environments are reduced to a non-secret baseline plus explicit
allowlists. Only configured HOME exceptions are re-exposed to agents; gates never receive them and always unshare
networking. `git.allow_repository_commands=false` neutralizes repository-configured clean/smudge/process filters
and custom merge drivers during control-plane Git operations. Any explicitly allowlisted secret remains readable by
hostile child code, so prefer offline credential-free execution whenever practical.

v1.4 adds an **independent resource boundary**. When `[resources].backend = "systemd"`, every agent and gate
process tree is launched in a transient systemd user scope backed by cgroups v2. Optional MemoryHigh/MemoryMax,
TasksMax, CPUQuota and IOWeight settings limit resource exhaustion outside the child itself. The systemd scope
wraps the Bubblewrap command rather than replacing it, so filesystem/network isolation and resource governance are
composable. `backend = "none"` is the compatibility default and provides no hard CPU/RAM/PID quota beyond existing
process timeouts and output/log bounds.

Repository topology is coordinated across jobs. A daemon-wide resource coordinator limits total active agent work,
and operations that mutate shared worktree/ref topology are serialized by resolved repository root. Cancellation
waits for bounded Git topology work to finish before the lock is released, and cancellation during integration
rolls the candidate back to its pre-transaction commit.

The durable lifecycle now has an executable state reference model. Ordinary job/task/attempt mutations are checked
against allowed transitions, terminal success states are absorbing, and impossible terminal snapshots are rejected
by the model. Recovery remains a deliberately separate atomic SQL transaction because attempts, tasks and jobs must
move together after a crash; its target transitions are explicitly represented by the same reference model.

SQLite persistence is schema-versioned with `PRAGMA user_version`. An older supported database is backed up using
the SQLite backup API to a private 0600 file before a transactional migration begins. Unknown future schema
versions, missing migration paths, migration failures and post-upgrade integrity failures all fail closed instead of
silently continuing with a partially understood state store. Backups can contain goals, paths, summaries and other
sensitive job state and must be protected like the primary database.

Every accepted v1.4 run receives a bounded canonical JSON execution dossier stored in SQLite before optional source
application. The dossier includes accepted Git commits, effective security/resource policy, agent capability and
command fingerprints, task/attempt history, summary hashes, and a SHA-256 digest of the durable event prefix up to
the dossier publication boundary. The dossier itself is SHA-256 addressed; `yolo-dossier` verifies that digest
before printing or exporting it. Dossiers are provenance, not cryptographic signatures: a user who can write the
state database can replace both content and digest. Signed external attestations remain a future optional layer.

Provenance generation occurs before source auto-apply. This preserves the v1.3 invariant that a fallible provenance
write cannot leave the user's source branch advanced while the durable job is later reported failed. Actual
apply/skipped outcome is recorded in the durable event stream after the dossier boundary.

Final release review remains fail-closed. Every changed file must be represented in bounded review input. Textual
changes are reviewed as file-local shards, multi-shard files receive a file-level semantic synthesis, and only
passed file reports reach the global cross-file synthesis. Binary changes are rejected by default; explicit
metadata-only binary review intentionally lowers assurance. Hostile/non-UTF-8 filename labels are injective and
single-line so names cannot forge prompt structure.

The daemon and CLI bound IPC messages, model prompts/output, event payloads, Git review data, gate/process capture,
and log growth. Durable worker/gate log failures terminate the process group and propagate. Git subprocesses have
bounded output, finite timeouts, process-group termination, sanitized ambient `GIT_*` state and noninteractive I/O.
Recovered worktrees are re-proven against canonical repository/branch ownership before use. The daemon singleton
lock and SQLite path reject unsafe symlink cases.

Post-integration cleanup is housekeeping. If cleanup fails after a task has integrated successfully, it is recorded
without rewriting accepted task/job truth. Likewise, post-release cleanup cannot retroactively turn an already
completed candidate into a failed release.

CI is part of the trust boundary. Development dependencies are version- and SHA-256-artifact-pinned for the
supported Python 3.12/3.13 Linux matrix and installed with `pip --require-hashes`. The built wheel is installed and
executed in a clean virtual environment, including its provenance verification entry point. Aggregate branch
coverage and trust-critical per-module floors remain release gates and are raised only after measured tests support
them.

These controls reduce accidental blast radius; they do not turn `danger-yolo`, Bubblewrap, or cgroups into a VM.
Repository gates intentionally execute project code, and all local child processes ultimately share the host Linux
kernel. Use a disposable VM/container for deliberately malicious code or exploit research.
