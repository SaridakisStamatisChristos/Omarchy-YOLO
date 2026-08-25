from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omarchy_yolo.agents import AgentRegistry
from omarchy_yolo.config import Config
from omarchy_yolo.db import CURRENT_SCHEMA_VERSION, Database
from omarchy_yolo.model import JobState, PlannedTask, TaskState
from omarchy_yolo.provenance import DOSSIER_SCHEMA_VERSION, build_dossier, verify_dossier
from omarchy_yolo.util import YoloError


def _legacy_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY,
              repo TEXT NOT NULL,
              goal TEXT NOT NULL,
              state TEXT NOT NULL,
              base_branch TEXT NOT NULL,
              base_commit TEXT NOT NULL,
              integration_branch TEXT NOT NULL,
              integration_path TEXT NOT NULL DEFAULT '',
              auto_apply INTEGER NOT NULL DEFAULT 0,
              stop_requested INTEGER NOT NULL DEFAULT 0,
              final_summary TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            PRAGMA user_version = 0;
            """
        )
    finally:
        conn.close()


def test_legacy_database_migrates_with_private_backup(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _legacy_database(path)
    db = Database(path)
    try:
        assert db.schema_version() == CURRENT_SCHEMA_VERSION
        assert db.last_migration_backup is not None
        backup = db.last_migration_backup
        assert backup.exists()
        assert backup.stat().st_mode & 0o777 == 0o600
        backup_conn = sqlite3.connect(backup)
        try:
            assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            backup_conn.close()
        row = db._execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dossiers'"
        ).fetchone()
        assert row is not None
        columns = {
            str(row["name"])
            for row in db._execute("PRAGMA table_info(dossiers)").fetchall()
        }
        assert "published" in columns
        event_table = db._execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        assert event_table is not None
    finally:
        db.close()


def test_future_database_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _legacy_database(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    finally:
        conn.close()
    with pytest.raises(YoloError, match="newer than supported"):
        Database(path)


def test_dossier_is_deterministic_verifiable_and_boundary_stable(tmp_path: Path) -> None:
    cfg = Config(
        state_dir=tmp_path / "state",
        config_path=tmp_path / "config.toml",
    )
    db = Database(cfg.db_path)
    try:
        job = db.create_job(
            repo=str(tmp_path / "repo"),
            goal="implement deterministic provenance",
            base_branch="main",
            base_commit="a" * 40,
            integration_branch="yolo/test/integration",
            auto_apply=False,
        )
        task = db.add_tasks(
            job.id,
            [PlannedTask("T1", "Implement", "Do the work", acceptance=("passes",))],
        )[0]
        db.update_job(job.id, state=JobState.RUNNING)
        db.update_task(task.id, state=TaskState.RUNNING, attempts=1)
        attempt = db.start_attempt(
            job_id=job.id,
            task_id=task.id,
            number=1,
            agent="fake",
            worktree="/tmp/worktree",
            branch="yolo/test/T1",
            log_path="/tmp/attempt.log",
        )
        db.update_task(task.id, state=TaskState.REVIEWING)
        db.update_task(task.id, state=TaskState.INTEGRATING)
        db.finish_attempt(attempt, state="passed", returncode=0, summary="verified")
        db.update_task(
            task.id,
            state=TaskState.COMPLETED,
            result_summary="verified",
        )
        db.event(job.id, "test.provenance", {"ok": True}, task_id=task.id)

        registry = AgentRegistry(cfg)
        first_content, first_hash = build_dossier(
            db=db,
            config=cfg,
            registry=registry,
            job_id=job.id,
            final_commit="b" * 40,
            final_summary="release accepted",
            source_apply_intent="not-requested",
        )
        second_content, second_hash = build_dossier(
            db=db,
            config=cfg,
            registry=registry,
            job_id=job.id,
            final_commit="b" * 40,
            final_summary="release accepted",
            source_apply_intent="not-requested",
        )
        assert first_content == second_content
        assert first_hash == second_hash
        assert verify_dossier(first_content, first_hash)
        payload = json.loads(first_content)
        assert payload["dossier_schema_version"] == DOSSIER_SCHEMA_VERSION
        assert payload["job"]["final_commit"] == "b" * 40
        assert payload["job"]["source_apply_intent"] == "not-requested"
        assert payload["event_ledger"]["count"] >= 2

        db.stage_dossier(
            job.id,
            schema_version=DOSSIER_SCHEMA_VERSION,
            sha256=first_hash,
            content=first_content,
        )
        assert db.get_dossier(job.id) is None
        staged = db.get_staged_dossier(job.id)
        assert staged is not None and staged["published"] is False

        db.prepare_accepted_job(
            job.id,
            accepted_commit="b" * 40,
            final_summary="release accepted",
            source_apply_intent="not-requested",
            dossier_schema_version=DOSSIER_SCHEMA_VERSION,
            dossier_sha256=first_hash,
            dossier_content=first_content,
        )
        db.publish_completed_job(
            job.id,
            dossier_schema_version=DOSSIER_SCHEMA_VERSION,
            dossier_sha256=first_hash,
            final_summary="release accepted",
            source_apply_outcome="not-requested",
        )
        db.event(job.id, "job.cleanup_failed", {"synthetic": True})

        regenerated, regenerated_hash = build_dossier(
            db=db,
            config=cfg,
            registry=registry,
            job_id=job.id,
            final_commit="b" * 40,
            final_summary="release accepted",
            source_apply_intent="not-requested",
        )
        assert regenerated == first_content
        assert regenerated_hash == first_hash

        stored = db.get_dossier(job.id)
        assert stored is not None
        assert stored["sha256"] == first_hash
        assert stored["content"] == first_content
    finally:
        db.close()
