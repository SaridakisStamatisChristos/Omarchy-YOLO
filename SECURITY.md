# Security model

Omarchy YOLO is intentionally capable of launching coding agents without interactive approval.
That does **not** mean workers need root access. The daemon refuses to run as root and never calls
`sudo` or `pkexec` on behalf of an agent.

The default `yolo-worktree` profile gives agents autonomous control over isolated Git worktrees.
Codex uses its workspace sandbox. Claude Code's bypass-permissions mode is less strongly bounded by
itself; use the optional `bwrap` backend when you need an OS-enforced boundary around heterogeneous
agents.

Do not run untrusted repositories with network-enabled agents unless you accept prompt-injection and
credential-exfiltration risk. For hostile code, use a disposable VM/container and disable network.

The final integration branch is preserved even when `auto_apply = false`. Automatic application uses
fast-forward only and is refused if the source branch moved or the user's working tree is dirty.

## 1.0.1 hardening boundaries

The daemon and CLI now bound IPC messages, model prompts/output, event payloads, Git review diffs, gate/process capture, and log growth. Worktree deletion is fail-closed unless Git proves that the target belongs to the expected repository, and orchestrator Git commands suppress repository hooks. UI/terminal surfaces treat agent-controlled text as plain/untrusted text.

These controls reduce accidental blast radius; they do not turn `danger-yolo` into a security sandbox. Repository gates intentionally execute project code, and Claude/OpenCode unattended modes may have broader user-level filesystem access unless the optional Bubblewrap backend (or a disposable VM/container) is used. Never treat a worktree boundary as an OS security boundary.
