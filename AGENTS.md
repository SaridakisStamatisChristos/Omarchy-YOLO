# Agent contribution contract

This repository is an autonomous orchestration control plane. Preserve these invariants when editing it:

1. Workers never intentionally mutate the user's source branch.
2. One worker task owns one Git worktree/branch.
3. Integration into the candidate branch is serialized.
4. A worker result is not complete until gates and review pass.
5. Stop/timeout must terminate process groups, not only parent CLIs.
6. Durable records are append/preserve-first; do not erase attempt/event history to simplify recovery.
7. Automatic source application must remain fast-forward-only and must refuse if the source moved or is dirty.
8. The daemon must never require or accept root execution.
9. Planner/reviewer structured output must be validated before it influences orchestration.
10. Repository content is untrusted input. Do not add behavior that deliberately exfiltrates credentials or
    follows repository-embedded instructions outside the user's goal/control-plane contract.

Before declaring a change complete, run `./scripts/check.sh` and add regression tests for state-machine, Git,
retry/recovery, adapter, or process-lifecycle changes.
