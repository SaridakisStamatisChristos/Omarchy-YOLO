# Operations

## State locations

Default paths follow XDG conventions:

- config: `~/.config/omarchy-yolo/config.toml`
- environment overrides: `~/.config/omarchy-yolo/env`
- database: `~/.local/state/omarchy-yolo/state.sqlite3`
- worktrees: `~/.local/state/omarchy-yolo/worktrees/`
- logs: `~/.local/state/omarchy-yolo/logs/`
- RPC socket: `$XDG_RUNTIME_DIR/omarchy-yolo.sock`

`OMARCHY_YOLO_CONFIG` and `OMARCHY_YOLO_STATE_DIR` override the first and state roots respectively.

## Service

```bash
systemctl --user status omarchy-yolo.service
systemctl --user restart omarchy-yolo.service
journalctl --user -u omarchy-yolo.service -f
```

Jobs are not owned by client terminals. Restart recovery requeues nonterminal jobs from SQLite.

## Job lifecycle

```text
queued -> planning -> running -> completed
                         |  \
                         |   -> failed -> resume -> queued
                         -> stopping -> stopped -> resume -> queued
```

Tasks move through `pending`, `running`, `reviewing`, `integrating`, and `completed`. `failed`, `blocked`, and
`stopped` are recoverable through explicit `resume` unless the job itself is already completed.

## Candidate branches

Every job creates:

```text
yolo/<run-id>/integration
```

and each task creates a temporary child branch beneath the job ID. Successful task branches/worktrees are
removed when cleanup is enabled; the integration branch remains as the audit/release artifact.

## Troubleshooting

Start with:

```bash
yolo doctor
yolo status
yolo events <job-id>
yolo logs <job-id>
```

If an agent CLI was upgraded and flags changed, edit the corresponding `[agents.NAME].command` array. The
orchestrator deliberately keeps CLI command lines in configuration so upstream churn does not require changing
the scheduling/state engine.

If the Omarchy panel is missing, verify the plugin and rescan:

```bash
omarchy plugin validate ~/.config/omarchy/plugins/dev.aether.yolo
omarchy-shell -q shell rescanPlugins
omarchy plugin enable dev.aether.yolo
yolo ui
```
