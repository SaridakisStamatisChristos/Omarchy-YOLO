from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from omarchy_yolo.config import Config
from omarchy_yolo.daemon import YoloDaemon
from omarchy_yolo.model import JobState
from omarchy_yolo.util import YoloError


def make_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> YoloDaemon:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr("omarchy_yolo.daemon.current_uid", lambda: 1000)
    return YoloDaemon(Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml"))


def make_job(daemon: YoloDaemon):
    return daemon.db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/test/integration",
        auto_apply=False,
    )


async def test_stop_without_live_runner_becomes_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon(tmp_path, monkeypatch)
    job = make_job(daemon)
    status = await daemon.dispatch("stop", {"job_id": job.id})
    assert status["job"]["state"] == JobState.STOPPED.value
    assert daemon.db.get_job(job.id).stop_requested
    daemon.db.close()


async def test_completed_job_cannot_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon(tmp_path, monkeypatch)
    job = make_job(daemon)
    daemon.db.update_job(job.id, state=JobState.RUNNING)
    content = "{}"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    daemon.db.prepare_accepted_job(
        job.id,
        accepted_commit="b" * 40,
        final_summary="accepted",
        source_apply_intent="not-requested",
        dossier_schema_version=1,
        dossier_sha256=digest,
        dossier_content=content,
    )
    daemon.db.publish_completed_job(
        job.id,
        dossier_schema_version=1,
        dossier_sha256=digest,
        final_summary="accepted",
        source_apply_outcome="not-requested",
    )
    with pytest.raises(YoloError, match="immutable"):
        await daemon.dispatch("stop", {"job_id": job.id})
    daemon.db.close()


async def test_resume_waits_for_stopping_runner_before_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon(tmp_path, monkeypatch)
    job = make_job(daemon)
    daemon.db.update_job(job.id, state=JobState.STOPPING, stop_requested=True)

    async def finish_stop() -> None:
        await asyncio.sleep(0)
        daemon.db.update_job(job.id, state=JobState.STOPPED)

    runner = asyncio.create_task(finish_stop())
    daemon.runners[job.id] = runner
    spawned: list[str] = []
    monkeypatch.setattr(daemon, "_spawn", lambda job_id: spawned.append(job_id))

    status = await daemon.dispatch("resume", {"job_id": job.id})
    assert status["job"]["state"] == JobState.QUEUED.value
    assert spawned == [job.id]
    assert not daemon.db.get_job(job.id).stop_requested
    daemon.db.close()
