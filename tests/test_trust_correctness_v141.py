from __future__ import annotations

import argparse
import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omarchy_yolo import cli, dossier_cli
from omarchy_yolo.config import AgentConfig, Config
from omarchy_yolo.db import CURRENT_SCHEMA_VERSION, Database
from omarchy_yolo.model import JobState, PlannedTask, TaskState
from omarchy_yolo.resources import ResourcePolicy
from omarchy_yolo.state_machine import StateTransitionError
from omarchy_yolo.util import YoloError


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _running_job_with_task(db: Database, root: Path, *, suffix: str = "one") -> tuple[str, str]:
    job = db.create_job(
        repo=str(root / f"repo-{suffix}"),
        goal=f"trust correctness {suffix}",
        base_branch="main",
        base_commit="a" * 40,
        integration_branch=f"yolo/{suffix}/integration",
        auto_apply=False,
    )
    task = db.add_tasks(
        job.id,
        [PlannedTask("T1", "Trust boundary", "Exercise the durable contract")],
    )[0]
    db.update_job(job.id, state=JobState.RUNNING)
    return job.id, task.id


def _complete_task(db: Database, task_id: str) -> None:
    db.update_task(task_id, state=TaskState.RUNNING)
    db.update_task(task_id, state=TaskState.REVIEWING)
    db.update_task(task_id, state=TaskState.INTEGRATING)
    db.update_task(task_id, state=TaskState.COMPLETED, result_summary="accepted")


def _stage(db: Database, job_id: str, content: str = '{"accepted":true}') -> str:
    digest = _digest(content)
    db.stage_dossier(job_id, schema_version=1, sha256=digest, content=content)
    return digest


def test_staged_dossier_is_invisible_until_atomic_completion(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, task_id = _running_job_with_task(db, tmp_path)
        _complete_task(db, task_id)
        digest = _stage(db, job_id)

        staged = db.get_staged_dossier(job_id)
        assert staged is not None
        assert staged["sha256"] == digest
        assert staged["published"] is False
        assert db.get_dossier(job_id) is None
        assert db.get_job(job_id).state == JobState.RUNNING

        db.publish_completed_job(
            job_id,
            dossier_schema_version=1,
            dossier_sha256=digest,
            final_summary="release accepted",
            source_apply_outcome="not-requested",
        )

        job = db.get_job(job_id)
        assert job.state == JobState.COMPLETED
        assert not job.stop_requested
        published = db.get_dossier(job_id)
        assert published is not None
        assert published["sha256"] == digest
        kinds = [event["kind"] for event in db.events(job_id, limit=100)]
        assert kinds[-2:] == ["job.dossier_created", "job.completed"]
    finally:
        db.close()


def test_publication_transaction_rolls_back_every_visible_change(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, task_id = _running_job_with_task(db, tmp_path)
        _complete_task(db, task_id)
        digest = _stage(db, job_id)
        before_events = db.last_event_id(job_id)

        with pytest.raises(ValueError, match="invalid source apply outcome"):
            db.publish_completed_job(
                job_id,
                dossier_schema_version=1,
                dossier_sha256=digest,
                final_summary="must roll back",
                source_apply_outcome="impossible",
            )

        assert db.get_job(job_id).state == JobState.RUNNING
        assert db.get_dossier(job_id) is None
        staged = db.get_staged_dossier(job_id)
        assert staged is not None and staged["published"] is False
        assert db.last_event_id(job_id) == before_events
    finally:
        db.close()


def test_completion_requires_publication_and_complete_cross_record_snapshot(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, task_id = _running_job_with_task(db, tmp_path)
        with pytest.raises(StateTransitionError, match="publish_completed_job"):
            db.update_job(job_id, state=JobState.COMPLETED)
        assert db.get_job(job_id).state == JobState.RUNNING

        digest = _stage(db, job_id)
        with pytest.raises(StateTransitionError, match="non-completed"):
            db.publish_completed_job(
                job_id,
                dossier_schema_version=1,
                dossier_sha256=digest,
                final_summary="not accepted",
                source_apply_outcome="not-requested",
            )
        assert db.get_job(job_id).state == JobState.RUNNING
        assert db.get_task(task_id).state == TaskState.PENDING
        assert db.get_dossier(job_id) is None
    finally:
        db.close()


def test_cross_record_snapshot_rejects_failed_job_with_inflight_task(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, task_id = _running_job_with_task(db, tmp_path)
        db.update_task(task_id, state=TaskState.RUNNING)
        with pytest.raises(StateTransitionError, match="in-flight"):
            db.update_job(job_id, state=JobState.FAILED, error="synthetic")
        assert db.get_job(job_id).state == JobState.RUNNING
        assert db.get_task(task_id).state == TaskState.RUNNING
    finally:
        db.close()


def test_completed_job_is_absorbing_across_resume_boundary(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, task_id = _running_job_with_task(db, tmp_path)
        _complete_task(db, task_id)
        digest = _stage(db, job_id)
        db.publish_completed_job(
            job_id,
            dossier_schema_version=1,
            dossier_sha256=digest,
            final_summary="accepted",
            source_apply_outcome="not-requested",
        )
        with pytest.raises(StateTransitionError, match="stopped or failed"):
            db.prepare_resume(job_id)
        assert db.get_job(job_id).state == JobState.COMPLETED
        assert db.get_dossier(job_id) is not None
    finally:
        db.close()


def test_attempt_must_reference_task_owned_by_same_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_a, _ = _running_job_with_task(db, tmp_path, suffix="a")
        _job_b, task_b = _running_job_with_task(db, tmp_path, suffix="b")
        with pytest.raises(YoloError, match="ownership mismatch"):
            db.start_attempt(
                job_id=job_a,
                task_id=task_b,
                number=1,
                agent="fake",
                worktree="/tmp/foreign",
                branch="yolo/foreign",
                log_path="/tmp/foreign.log",
            )
        assert db.attempt_metrics(job_a)["attempts_total"] == 0
    finally:
        db.close()


def test_schema_contract_rejects_malformed_database_before_normal_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL);
            PRAGMA user_version = {CURRENT_SCHEMA_VERSION};
            """
        )
    finally:
        conn.close()

    with pytest.raises(YoloError, match="schema contract mismatch for jobs"):
        Database(path)


def test_schema_v3_composite_foreign_key_rejects_cross_job_attempt_directly(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_a, _ = _running_job_with_task(db, tmp_path, suffix="a")
        _job_b, task_b = _running_job_with_task(db, tmp_path, suffix="b")
        with pytest.raises(sqlite3.IntegrityError):
            db._execute(
                """
                INSERT INTO attempts(
                  id, job_id, task_id, number, agent, state, worktree, branch,
                  log_path, started_at
                ) VALUES ('attempt_crossjob', ?, ?, 1, 'fake', 'running', '', '', '', 0)
                """,
                (job_a, task_b),
            )
    finally:
        db.close()


def test_dossier_cli_refuses_staged_state_then_verifies_published_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml")
    db = Database(cfg.db_path)
    try:
        job_id, task_id = _running_job_with_task(db, tmp_path)
        _complete_task(db, task_id)
        content = '{"release":"accepted"}'
        digest = _stage(db, job_id, content)
    finally:
        db.close()

    monkeypatch.setattr(dossier_cli, "load_config", lambda: cfg)
    args = argparse.Namespace(job_id=job_id, output=None, verify_only=True)
    with pytest.raises(YoloError, match="durable job completion"):
        dossier_cli.run(args)

    db = Database(cfg.db_path)
    try:
        db.publish_completed_job(
            job_id,
            dossier_schema_version=1,
            dossier_sha256=digest,
            final_summary="accepted",
            source_apply_outcome="not-requested",
        )
    finally:
        db.close()

    assert dossier_cli.run(args) == 0
    assert capsys.readouterr().out.strip() == digest

    output = tmp_path / "exports" / "dossier.json"
    assert dossier_cli.run(
        argparse.Namespace(job_id=job_id, output=str(output), verify_only=False)
    ) == 0
    assert output.read_text(encoding="utf-8") == content + "\n"
    assert output.stat().st_mode & 0o777 == 0o600

    db = Database(cfg.db_path)
    try:
        db._execute("UPDATE dossiers SET sha256 = ? WHERE job_id = ?", ("0" * 64, job_id))
    finally:
        db.close()
    with pytest.raises(YoloError, match="SHA-256 verification"):
        dossier_cli.run(args)


def test_dossier_stage_rejects_digest_mismatch(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, _ = _running_job_with_task(db, tmp_path)
        with pytest.raises(StateTransitionError, match="SHA-256"):
            db.stage_dossier(
                job_id,
                schema_version=1,
                sha256="0" * 64,
                content="different",
            )
        assert db.get_staged_dossier(job_id) is None
    finally:
        db.close()


def test_resource_policy_live_probe_reports_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"systemd-run", "true"} else None

    monkeypatch.setattr("omarchy_yolo.resources.shutil.which", which)
    monkeypatch.setattr(
        "omarchy_yolo.resources.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    policy = ResourcePolicy(backend="systemd", memory_max_mib=1024)
    assert policy.probe() == (True, "/usr/bin/systemd-run")

    monkeypatch.setattr(
        "omarchy_yolo.resources.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "no user manager"),
    )
    ok, detail = policy.probe()
    assert not ok
    assert "no user manager" in detail


def test_resource_policy_probe_fails_when_systemd_run_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omarchy_yolo.resources.shutil.which", lambda _name: None)
    ok, detail = ResourcePolicy(backend="systemd").probe()
    assert not ok
    assert "requires systemd-run" in detail


async def test_doctor_makes_configured_resource_backend_a_readiness_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(
        state_dir=tmp_path / "state",
        config_path=tmp_path / "config.toml",
        resources=ResourcePolicy(backend="systemd", memory_max_mib=1024),
        agents={
            "agent": AgentConfig(
                command=("agent",),
                review_command=("agent", "--read-only"),
                roles=("worker", "planner", "reviewer", "integrator"),
            )
        },
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"git", "agent"} else None)
    monkeypatch.setattr(
        "omarchy_yolo.agents.base.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "agent" else None,
    )
    monkeypatch.setattr(ResourcePolicy, "probe", lambda self: (False, "user scope unavailable"))

    async def fake_rpc(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"pid": 1234, "version": "1.4.1"}

    monkeypatch.setattr(cli, "_rpc", fake_rpc)
    assert await cli.cmd_doctor(argparse.Namespace()) == 1
    rendered = capsys.readouterr().out
    assert "[!!] resources" in rendered
    assert "user scope unavailable" in rendered
