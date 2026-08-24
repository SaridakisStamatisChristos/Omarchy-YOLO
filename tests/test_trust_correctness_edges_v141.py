from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

from omarchy_yolo import dossier_cli
from omarchy_yolo.config import Config
from omarchy_yolo.db import Database
from omarchy_yolo.model import JobState, PlannedTask, TaskState
from omarchy_yolo.state_machine import StateTransitionError
from omarchy_yolo.util import YoloError


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _completed_task_job(
    db: Database,
    root: Path,
    *,
    suffix: str,
) -> tuple[str, str]:
    job = db.create_job(
        repo=str(root / f"repo-{suffix}"),
        goal=f"edge contract {suffix}",
        base_branch="main",
        base_commit="a" * 40,
        integration_branch=f"yolo/{suffix}/integration",
        auto_apply=False,
    )
    task = db.add_tasks(
        job.id,
        [PlannedTask("T1", "Edge", "Exercise trust-boundary behavior")],
    )[0]
    db.update_job(job.id, state=JobState.RUNNING)
    db.update_task(task.id, state=TaskState.RUNNING)
    db.update_task(task.id, state=TaskState.REVIEWING)
    db.update_task(task.id, state=TaskState.INTEGRATING)
    db.update_task(task.id, state=TaskState.COMPLETED, result_summary="accepted")
    return job.id, task.id


def _stage(db: Database, job_id: str, content: str = '{"ok":true}') -> str:
    digest = _digest(content)
    db.stage_dossier(job_id, schema_version=1, sha256=digest, content=content)
    return digest


def _prepare(
    db: Database,
    job_id: str,
    *,
    content: str,
    digest: str,
    summary: str,
    intent: str = "not-requested",
) -> None:
    accepted_commit = "b" * 40
    db.prepare_accepted_job(
        job_id,
        accepted_commit=accepted_commit,
        final_summary=summary,
        source_apply_intent=intent,
        dossier_schema_version=1,
        dossier_sha256=digest,
        dossier_content=content,
    )
    if intent == "requested":
        db.mark_apply_started(job_id, accepted_commit=accepted_commit)


def test_stage_validation_store_compatibility_and_idempotent_publication(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        job_id, _ = _completed_task_job(db, tmp_path, suffix="stage")
        content = '{"accepted":"yes"}'
        digest = _digest(content)

        with pytest.raises(ValueError, match="schema_version"):
            db.stage_dossier(
                job_id,
                schema_version=0,
                sha256=digest,
                content=content,
            )

        db.store_dossier(
            job_id,
            schema_version=1,
            sha256=digest,
            content=content,
        )
        _prepare(db, job_id, content=content, digest=digest, summary="accepted")
        db.publish_completed_job(
            job_id,
            dossier_schema_version=1,
            dossier_sha256=digest,
            final_summary="accepted",
            source_apply_outcome="not-requested",
        )
        before = db.last_event_id(job_id)
        db.publish_completed_job(
            job_id,
            dossier_schema_version=1,
            dossier_sha256=digest,
            final_summary="accepted",
            source_apply_outcome="not-requested",
        )
        assert db.last_event_id(job_id) == before

        with pytest.raises(StateTransitionError, match="completed job|acceptance begins"):
            db.stage_dossier(
                job_id,
                schema_version=1,
                sha256=digest,
                content=content,
            )
    finally:
        db.close()


def test_publication_rejects_missing_changed_and_tampered_dossiers(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        missing_job, _ = _completed_task_job(db, tmp_path, suffix="missing")
        with pytest.raises(StateTransitionError, match="without a staged dossier"):
            db.publish_completed_job(
                missing_job,
                dossier_schema_version=1,
                dossier_sha256="0" * 64,
                final_summary="no dossier",
                source_apply_outcome="not-requested",
            )

        changed_job, _ = _completed_task_job(db, tmp_path, suffix="changed")
        content = '{"stable":true}'
        digest = _stage(db, changed_job, content)
        with pytest.raises(StateTransitionError, match="schema version changed"):
            db.publish_completed_job(
                changed_job,
                dossier_schema_version=2,
                dossier_sha256=digest,
                final_summary="wrong schema",
                source_apply_outcome="not-requested",
            )
        with pytest.raises(StateTransitionError, match="digest changed"):
            db.publish_completed_job(
                changed_job,
                dossier_schema_version=1,
                dossier_sha256="f" * 64,
                final_summary="wrong digest",
                source_apply_outcome="not-requested",
            )

        db._execute(
            "UPDATE dossiers SET content = ? WHERE job_id = ?",
            ('{"stable":false}', changed_job),
        )
        with pytest.raises(StateTransitionError, match="content failed SHA-256"):
            db.publish_completed_job(
                changed_job,
                dossier_schema_version=1,
                dossier_sha256=digest,
                final_summary="tampered",
                source_apply_outcome="not-requested",
            )
    finally:
        db.close()


def test_publication_rejects_running_attempt_and_records_apply_outcomes(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state" / "state.sqlite3")
    try:
        running_job, running_task = _completed_task_job(db, tmp_path, suffix="running-attempt")
        running_content = '{"ok":true}'
        running_digest = _stage(db, running_job, running_content)
        # Seed an impossible historical/corrupt row directly. The guarded public
        # API correctly refuses to create a running attempt for a completed task;
        # publication must still fail closed if such a row exists on disk.
        db._execute(
            """
            INSERT INTO attempts(
              id, job_id, task_id, number, agent, state, worktree, branch,
              log_path, started_at
            ) VALUES ('attempt_corrupt', ?, ?, 1, 'synthetic', 'running',
                      '/tmp/worktree', 'yolo/synthetic', '/tmp/attempt.log', 0)
            """,
            (running_job, running_task),
        )
        with pytest.raises(StateTransitionError, match="running attempt|not in flight"):
            _prepare(
                db,
                running_job,
                content=running_content,
                digest=running_digest,
                summary="must fail",
            )

        applied_job, _ = _completed_task_job(db, tmp_path, suffix="applied")
        applied_content = '{"applied":true}'
        applied_digest = _stage(db, applied_job, applied_content)
        _prepare(
            db,
            applied_job,
            content=applied_content,
            digest=applied_digest,
            summary="applied",
            intent="requested",
        )
        db.publish_completed_job(
            applied_job,
            dossier_schema_version=1,
            dossier_sha256=applied_digest,
            final_summary="applied",
            source_apply_outcome="applied",
        )
        applied_kinds = [event["kind"] for event in db.events(applied_job, limit=100)]
        assert "job.applied" in applied_kinds

        skipped_job, _ = _completed_task_job(db, tmp_path, suffix="skipped")
        skipped_content = '{"skipped":true}'
        skipped_digest = _stage(db, skipped_job, skipped_content)
        _prepare(
            db,
            skipped_job,
            content=skipped_content,
            digest=skipped_digest,
            summary="skipped",
            intent="requested",
        )
        db.publish_completed_job(
            skipped_job,
            dossier_schema_version=1,
            dossier_sha256=skipped_digest,
            final_summary="skipped",
            source_apply_outcome="skipped",
            source_apply_reason="source branch moved",
        )
        skipped_events = db.events(skipped_job, limit=100)
        skipped = next(event for event in skipped_events if event["kind"] == "job.apply_skipped")
        assert skipped["payload"]["reason"] == "source branch moved"
    finally:
        db.close()


def test_dossier_cli_plain_output_missing_publication_symlink_and_main_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = dossier_cli.build_parser()
    parsed = parser.parse_args(["job_0123456789ab", "--verify-only"])
    assert parsed.job_id == "job_0123456789ab"
    assert parsed.verify_only

    cfg = Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml")
    db = Database(cfg.db_path)
    try:
        job_id, _ = _completed_task_job(db, tmp_path, suffix="cli")
        content = '{"cli":"plain"}'
        digest = _stage(db, job_id, content)
        _prepare(db, job_id, content=content, digest=digest, summary="accepted")
        db.publish_completed_job(
            job_id,
            dossier_schema_version=1,
            dossier_sha256=digest,
            final_summary="accepted",
            source_apply_outcome="not-requested",
        )

        missing = db.create_job(
            repo=str(tmp_path / "repo-missing-published"),
            goal="simulate historical completed row",
            base_branch="main",
            base_commit="b" * 40,
            integration_branch="yolo/missing-published/integration",
            auto_apply=False,
        )
        db._execute(
            "UPDATE jobs SET state = ?, stop_requested = 0 WHERE id = ?",
            (JobState.COMPLETED.value, missing.id),
        )
    finally:
        db.close()

    monkeypatch.setattr(dossier_cli, "load_config", lambda: cfg)
    assert dossier_cli.run(
        argparse.Namespace(job_id=job_id, output=None, verify_only=False)
    ) == 0
    assert capsys.readouterr().out.strip() == content

    with pytest.raises(YoloError, match="no published execution dossier"):
        dossier_cli.run(
            argparse.Namespace(job_id=missing.id, output=None, verify_only=False)
        )

    target = tmp_path / "real-output.json"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "dossier-link.json"
    link.symlink_to(target)
    with pytest.raises(YoloError, match="symlink dossier output"):
        dossier_cli.run(
            argparse.Namespace(job_id=job_id, output=str(link), verify_only=False)
        )
    assert target.read_text(encoding="utf-8") == "keep"

    monkeypatch.setattr(sys, "argv", ["yolo-dossier", "job_ffffffffffff"])
    with pytest.raises(SystemExit) as exc:
        dossier_cli.main()
    assert exc.value.code == 1
    assert "error:" in capsys.readouterr().err


def test_current_schema_repairs_missing_index_but_rejects_wrong_definition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    db.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX idx_jobs_created")
    finally:
        conn.close()

    repaired = Database(path)
    try:
        row = repaired._execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_jobs_created'"
        ).fetchone()
        assert row is not None
    finally:
        repaired.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX idx_jobs_created")
        conn.execute("CREATE INDEX idx_jobs_created ON jobs(state)")
    finally:
        conn.close()

    with pytest.raises(YoloError, match="schema contract mismatch for index idx_jobs_created"):
        Database(path)


def test_foreign_key_check_rejects_orphaned_durable_rows(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    db.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            ("job_missing0000", "orphan", "{}", 0.0),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(YoloError, match="foreign_key_check"):
        Database(path)
