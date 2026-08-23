from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import __version__
from .agents import AgentRegistry
from .config import Config
from .db import Database
from .model import JobState, TaskState
from .state_machine import validate_job_snapshot
from .util import YoloError, truncate_utf8

DOSSIER_SCHEMA_VERSION = 1
MAX_DOSSIER_BYTES = 512_000


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_dossier(content: str, expected_sha256: str) -> bool:
    return sha256_text(content) == expected_sha256


def _event_digest(events: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    digest = hashlib.sha256()
    kinds: Counter[str] = Counter()
    for event in events:
        kinds[str(event["kind"])] += 1
        digest.update(canonical_json(event).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest(), dict(sorted(kinds.items()))


def _summary_fingerprint(value: str) -> dict[str, object]:
    bounded = truncate_utf8(value, 512, marker="...[truncated]...")
    return {
        "sha256": sha256_text(value),
        "preview": bounded,
    }


def _effective_config(config: Config) -> dict[str, object]:
    return {
        "engine": {
            "max_parallel": config.engine.max_parallel,
            "max_global_workers": config.engine.max_global_workers,
            "max_attempts": config.engine.max_attempts,
            "max_final_cycles": config.engine.max_final_cycles,
            "max_tasks": config.engine.max_tasks,
            "agent_timeout_seconds": config.engine.agent_timeout_seconds,
            "gate_timeout_seconds": config.engine.gate_timeout_seconds,
            "planner_agent": config.engine.planner_agent,
            "reviewer_agent": config.engine.reviewer_agent,
            "integrator_agent": config.engine.integrator_agent,
            "worker_agents": list(config.engine.worker_agents),
            "auto_apply": config.engine.auto_apply,
            "cleanup_worktrees": config.engine.cleanup_worktrees,
            "execution_profile": config.engine.execution_profile,
            "final_review_chunk_bytes": config.engine.final_review_chunk_bytes,
            "final_review_chunk_files": config.engine.final_review_chunk_files,
            "final_review_max_files": config.engine.final_review_max_files,
            "final_review_allow_binary": config.engine.final_review_allow_binary,
        },
        "git": {
            "require_clean_repo": config.git.require_clean_repo,
            "branch_prefix": config.git.branch_prefix,
            "command_timeout_seconds": config.git.command_timeout_seconds,
            "allow_repository_commands": config.git.allow_repository_commands,
        },
        "gates": {
            "commands": list(config.gates.commands),
            "final_commands": list(config.gates.final_commands),
        },
        "sandbox": {
            "backend": config.sandbox.backend,
            "network": config.sandbox.network,
            "read_only_home": config.sandbox.read_only_home,
            "writable_home_paths": list(config.sandbox.writable_home_paths),
            "hostile_repo_mode": config.sandbox.hostile_repo_mode,
            "gate_env_allowlist": list(config.sandbox.gate_env_allowlist),
            "agent_env_allowlist": list(config.sandbox.agent_env_allowlist),
        },
        "resources": config.resources.contract(),
    }


def build_dossier(
    *,
    db: Database,
    config: Config,
    registry: AgentRegistry,
    job_id: str,
    final_commit: str,
    final_summary: str,
    source_apply_outcome: str,
) -> tuple[str, str]:
    job = db.get_job(job_id)
    snapshot = db.provenance_snapshot(job_id)
    tasks = list(snapshot["tasks"])
    attempts = list(snapshot["attempts"])
    events = list(snapshot["events"])

    validate_job_snapshot(
        JobState.COMPLETED,
        (TaskState(str(task["state"])) for task in tasks),
        stop_requested=False,
    )
    event_sha256, event_kinds = _event_digest(events)

    compact_tasks = [
        {
            "id": task["id"],
            "seq": task["seq"],
            "logical_id": task["logical_id"],
            "title": task["title"],
            "state": task["state"],
            "dependencies": task["dependencies"],
            "acceptance": task["acceptance"],
            "preferred_agent": task["preferred_agent"],
            "attempts": task["attempts"],
            "branch": task["branch"],
            "base_commit": task["base_commit"],
            "result_summary": _summary_fingerprint(str(task["result_summary"])),
        }
        for task in tasks
    ]
    compact_attempts = [
        {
            "id": attempt["id"],
            "task_id": attempt["task_id"],
            "number": attempt["number"],
            "agent": attempt["agent"],
            "state": attempt["state"],
            "branch": attempt["branch"],
            "started_at": attempt["started_at"],
            "finished_at": attempt["finished_at"],
            "returncode": attempt["returncode"],
            "summary": _summary_fingerprint(str(attempt["summary"])),
        }
        for attempt in attempts
    ]

    agent_config = {
        name: {
            "command_sha256": sha256_text(canonical_json(list(agent.command))),
            "review_command_sha256": sha256_text(canonical_json(list(agent.review_command))),
            "roles": list(agent.roles),
        }
        for name, agent in sorted(config.agents.items())
    }

    payload: dict[str, object] = {
        "dossier_schema_version": DOSSIER_SCHEMA_VERSION,
        "omarchy_yolo_version": __version__,
        "job": {
            "id": job.id,
            "repo": str(Path(job.repo).resolve()),
            "goal_sha256": sha256_text(job.goal),
            "goal_preview": truncate_utf8(job.goal, 2_000, marker="...[truncated]..."),
            "base_branch": job.base_branch,
            "base_commit": job.base_commit,
            "integration_branch": job.integration_branch,
            "final_commit": final_commit,
            "auto_apply": job.auto_apply,
            "source_apply_outcome": source_apply_outcome,
            "accepted_state": JobState.COMPLETED.value,
            "created_at": job.created_at,
            "accepted_summary": _summary_fingerprint(final_summary),
        },
        "effective_config": _effective_config(config),
        "agent_contracts": registry.contracts(),
        "agent_config_fingerprints": agent_config,
        "tasks": compact_tasks,
        "attempts": compact_attempts,
        "event_ledger": {
            "count": len(events),
            "sha256": event_sha256,
            "kinds": event_kinds,
            "last_event_id": int(events[-1]["id"]) if events else 0,
        },
    }
    content = canonical_json(payload)
    if len(content.encode("utf-8")) > MAX_DOSSIER_BYTES:
        raise YoloError("execution dossier exceeds the bounded serialization limit")
    return content, sha256_text(content)
