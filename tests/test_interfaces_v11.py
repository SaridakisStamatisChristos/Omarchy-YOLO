from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omarchy_yolo import __version__, cli
from omarchy_yolo.agents import AgentRegistry
from omarchy_yolo.agents.base import CommandAgent, MAX_PROMPT_ARG_BYTES
from omarchy_yolo.config import AgentConfig, Config, SandboxConfig
from omarchy_yolo.daemon import YoloDaemon
from omarchy_yolo.model import AgentResult, PlannedTask, ReviewResult
from omarchy_yolo.reviewer import Reviewer
from omarchy_yolo.rpc import RpcError, RpcRemoteError, RpcUnavailable, rpc_call
from omarchy_yolo.sandbox import Sandbox
from omarchy_yolo.util import YoloError


class CapturingRunner:
    def __init__(self, stdout: str = "ok", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.prompt = ""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
        env: dict[str, str] | None = None,
        prompt_arg: str | None = None,
    ) -> AgentResult:
        self.argv = list(argv)
        self.env = dict(env or {})
        self.prompt = prompt_arg or ""
        return AgentResult(self.returncode, self.stdout, "", 0.01, tuple(argv))


class ReviewAgent:
    name = "reviewer"

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def supports_profile(self, execution_profile: str) -> bool:
        return execution_profile == "review"

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
        execution_profile: str,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        self.prompts.append(prompt)
        return AgentResult(0, self.stdout, "", 0.01, ("reviewer",))


def make_config(tmp_path: Path, **agents: AgentConfig) -> Config:
    return Config(
        state_dir=tmp_path / "state",
        config_path=tmp_path / "config.toml",
        agents=dict(agents),
    )


async def _start_rpc_server(path: Path, mode: str = "ok") -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        request = json.loads(raw)
        if mode == "close":
            writer.close()
            await writer.wait_closed()
            return
        if mode == "invalid-json":
            writer.write(b"not-json\n")
        elif mode == "mismatch":
            writer.write(json.dumps({"id": "wrong", "ok": True, "result": 1}).encode() + b"\n")
        elif mode == "remote-error":
            writer.write(
                json.dumps({"id": request["id"], "ok": False, "error": "boom"}).encode()
                + b"\n"
            )
        else:
            writer.write(
                json.dumps({"id": request["id"], "ok": True, "result": request["params"]}).encode()
                + b"\n"
            )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_rpc_round_trip_and_transport_errors(tmp_path: Path) -> None:
    socket = tmp_path / "rpc.sock"
    server = await _start_rpc_server(socket)
    async with server:
        assert await rpc_call(socket, "echo", {"x": 1}) == {"x": 1}
    server.close()
    await server.wait_closed()

    for mode, error in (
        ("remote-error", RpcRemoteError),
        ("invalid-json", RpcError),
        ("mismatch", RpcError),
        ("close", RpcUnavailable),
    ):
        with pytest.raises(error):
            server = await _start_rpc_server(socket, mode)
            try:
                await rpc_call(socket, "echo", {})
            finally:
                server.close()
                await server.wait_closed()

    with pytest.raises(RpcUnavailable):
        await rpc_call(tmp_path / "missing.sock", "ping")


async def test_rpc_rejects_oversized_request(tmp_path: Path) -> None:
    socket = tmp_path / "large.sock"
    server = await _start_rpc_server(socket)
    async with server:
        with pytest.raises(RpcError, match="request exceeds"):
            await rpc_call(socket, "echo", {"x": "z" * 1_100_000})
    server.close()
    await server.wait_closed()


def test_sandbox_native_unknown_and_missing_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = ["python", "-V"]
    assert Sandbox(SandboxConfig(backend="native")).wrap(
        command, tmp_path, execution_profile="yolo-worktree"
    ) == command
    with pytest.raises(YoloError, match="unknown sandbox"):
        Sandbox(SandboxConfig(backend="wat")).wrap(
            command, tmp_path, execution_profile="yolo-worktree"
        )
    monkeypatch.setattr("omarchy_yolo.sandbox.shutil.which", lambda _: None)
    with pytest.raises(YoloError, match="bubblewrap"):
        Sandbox(SandboxConfig(backend="bwrap")).wrap(
            command, tmp_path, execution_profile="yolo-worktree"
        )


def test_bwrap_review_is_read_only_and_worker_policy_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omarchy_yolo.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    home = Path.home().resolve()
    sandbox = Sandbox(SandboxConfig(backend="bwrap", network=False, read_only_home=False))
    worker = sandbox.wrap(["agent"], tmp_path, execution_profile="yolo-worktree")
    review = sandbox.wrap(["agent"], tmp_path, execution_profile="review")
    assert ["--bind", str(home), str(home)] == worker[
        worker.index(str(home)) - 1 : worker.index(str(home)) + 2
    ]
    assert str(home) not in review
    assert "--unshare-net" in review
    env = sandbox.environment()
    assert env["CI"]
    assert env["OMARCHY_YOLO"] == "1"


def test_bwrap_rejects_writable_home_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omarchy_yolo.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    sandbox = Sandbox(
        SandboxConfig(
            backend="bwrap",
            read_only_home=True,
            writable_home_paths=("../../escape",),
        )
    )
    with pytest.raises(YoloError, match="escapes HOME"):
        sandbox.wrap(["agent"], tmp_path, execution_profile="yolo-worktree")


def test_custom_agent_requires_declared_review_command(tmp_path: Path) -> None:
    agent = CommandAgent(
        "custom",
        AgentConfig(command=("python", "-V")),
        make_config(tmp_path),
    )
    assert agent.supports_profile("yolo-worktree")
    assert not agent.supports_profile("review")
    with pytest.raises(YoloError, match="review capability"):
        agent.command_for_profile("review")

    declared = CommandAgent(
        "custom",
        AgentConfig(command=("python", "worker"), review_command=("python", "review")),
        make_config(tmp_path),
    )
    assert declared.supports_profile("review")
    assert declared.command_for_profile("review") == ["python", "review"]


async def test_agent_run_enforces_policy_and_prompt_bound(tmp_path: Path) -> None:
    runner = CapturingRunner(stdout='{"text":"answer"}\n')
    agent = CommandAgent(
        "opencode",
        AgentConfig(command=("python",), review_command=("python",)),
        make_config(tmp_path),
        runner=runner,
    )
    result = await agent.run(
        "x" * (MAX_PROMPT_ARG_BYTES * 2),
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=tmp_path / "agent.log",
        execution_profile="review",
        extra_env={"OPENCODE_CONFIG_CONTENT": "unsafe", "EXTRA": "1"},
    )
    assert result.stdout == "answer"
    assert len(runner.prompt.encode()) <= MAX_PROMPT_ARG_BYTES
    assert runner.env["EXTRA"] == "1"
    assert runner.env["OPENCODE_CONFIG_CONTENT"] != "unsafe"
    assert '"edit":"deny"' in runner.env["OPENCODE_CONFIG_CONTENT"]


async def test_agent_unavailable_and_stdout_normalization(tmp_path: Path) -> None:
    missing = CommandAgent(
        "missing",
        AgentConfig(command=("definitely-not-installed-yolo-test",)),
        make_config(tmp_path),
        runner=CapturingRunner(),
    )
    with pytest.raises(YoloError, match="not installed"):
        await missing.run(
            "p",
            cwd=tmp_path,
            timeout_seconds=1,
            log_path=tmp_path / "x.log",
            execution_profile="yolo-worktree",
        )

    claude = CommandAgent("claude", AgentConfig(command=("python",)), make_config(tmp_path))
    assert claude._normalize_stdout('{"result":"clean"}') == "clean"
    assert claude._normalize_stdout("not-json") == "not-json"

    opencode = CommandAgent("opencode", AgentConfig(command=("python",)), make_config(tmp_path))
    nested = json.dumps({"data": {"content": "one"}}) + "\n" + json.dumps({"text": "two"})
    normalized = opencode._normalize_stdout(nested)
    assert "one" in normalized and "two" in normalized


def test_registry_filters_review_capabilities(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path,
        custom=AgentConfig(command=("python",)),
        safe=AgentConfig(command=("python",), review_command=("python", "-V")),
    )
    registry = AgentRegistry(cfg)
    assert "custom" in registry.available()
    assert "custom" not in registry.available("review")
    assert registry.choose_role(
        "custom", ("safe",), execution_profile="review"
    ) == "safe"
    with pytest.raises(YoloError, match="unknown agent"):
        registry.get("unknown")


async def test_reviewer_parses_results_and_rejects_invalid_contracts(tmp_path: Path) -> None:
    good = ReviewAgent('{"verdict":"pass","summary":"clean","findings":[]}')
    registry = AgentRegistry(make_config(tmp_path), overrides={"reviewer": good})
    reviewer = Reviewer(registry)
    result = await reviewer.review_final_synthesis(
        goal="goal",
        gates=[],
        manifest=["a.py", "b.py"],
        chunk_reviews=[ReviewResult("pass", "a"), ReviewResult("pass", "b")],
        cwd=tmp_path,
        agent_name="reviewer",
        timeout_seconds=10,
        log_path=tmp_path / "review.log",
    )
    assert result.passed
    assert "a.py" in good.prompts[-1]

    bad = ReviewAgent('{"verdict":"pass","summary":"bad","findings":["material"]}')
    reviewer_bad = Reviewer(AgentRegistry(make_config(tmp_path), overrides={"reviewer": bad}))
    with pytest.raises(YoloError, match="pass with material findings"):
        await reviewer_bad.review_final(
            goal="goal",
            diff="diff",
            gates=[],
            cwd=tmp_path,
            agent_name="reviewer",
            timeout_seconds=10,
            log_path=tmp_path / "bad.log",
        )


async def test_reviewer_rejects_oversized_synthesis_input(tmp_path: Path) -> None:
    agent = ReviewAgent('{"verdict":"pass","summary":"ok","findings":[]}')
    reviewer = Reviewer(AgentRegistry(make_config(tmp_path), overrides={"reviewer": agent}))
    with pytest.raises(YoloError, match="manifest is too large"):
        await reviewer.review_final_synthesis(
            goal="goal",
            gates=[],
            manifest=["x" * 81_000],
            chunk_reviews=[],
            cwd=tmp_path,
            agent_name="reviewer",
            timeout_seconds=1,
            log_path=tmp_path / "review.log",
        )


def make_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, agents: dict[str, AgentConfig] | None = None
) -> YoloDaemon:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("omarchy_yolo.daemon.current_uid", lambda: 1000)
    return YoloDaemon(
        Config(
            state_dir=tmp_path / "state",
            config_path=tmp_path / "config.toml",
            agents=agents or {},
        )
    )


async def test_daemon_actual_socket_ping_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon(tmp_path, monkeypatch)
    await daemon.start()
    result = await rpc_call(daemon.socket_path, "ping")
    assert result["version"] == __version__
    assert daemon.socket_path.stat().st_mode & 0o777 == 0o600
    await daemon.shutdown()
    assert not daemon.socket_path.exists()


async def test_daemon_dispatch_status_events_agents_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon(
        tmp_path,
        monkeypatch,
        agents={"custom": AgentConfig(enabled=False, command=("python",))},
    )
    job = daemon.db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/test/integration",
        auto_apply=False,
    )
    daemon.db.add_tasks(job.id, [PlannedTask("T1", "Title", "Description")])
    assert (await daemon.dispatch("ping", {}))["version"] == __version__
    assert (await daemon.dispatch("runtime", {}))["worker_capacity"] >= 1
    assert (await daemon.dispatch("status", {"job_id": job.id}))["tasks"][0]["logical_id"] == "T1"
    assert len(await daemon.dispatch("jobs", {"limit": "999999"})) == 1
    events = await daemon.dispatch("events", {"job_id": job.id, "after_id": 0, "limit": 999999})
    assert events
    agents = await daemon.dispatch("agents", {})
    assert "custom" in agents["configured"]
    with pytest.raises(YoloError, match="unknown RPC method"):
        await daemon.dispatch("nope", {})
    with pytest.raises(YoloError, match="integer"):
        daemon._bounded_int(True, default=1, minimum=0, maximum=2, name="n")
    daemon.db.close()


async def test_daemon_submit_real_git_repo(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = make_daemon(tmp_path, monkeypatch)
    spawned: list[str] = []
    monkeypatch.setattr(daemon, "_spawn", lambda job_id: spawned.append(job_id))
    status = await daemon.dispatch(
        "submit", {"goal": "do useful work", "repo": str(git_repo), "auto_apply": False}
    )
    assert status["job"]["base_branch"] == "main"
    assert status["job"]["state"] == "queued"
    assert spawned == [status["job"]["id"]]
    with pytest.raises(YoloError, match="auto_apply"):
        await daemon.dispatch(
            "submit", {"goal": "x", "repo": str(git_repo), "auto_apply": "yes"}
        )
    daemon.db.close()


async def test_cli_command_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    responses: dict[str, Any] = {
        "jobs": [{"id": "job_1", "state": "completed", "repo": "/tmp/repo", "goal": "done"}],
        "events": [{"id": 1, "kind": "job.created", "task_id": None, "payload": {"x": 1}}],
        "status": {
            "job": {
                "id": "job_1",
                "state": "completed",
                "repo": "/tmp/repo",
                "goal": "done",
                "integration_branch": "yolo/x/integration",
                "final_summary": "ok",
                "error": "",
            },
            "tasks": [],
            "counts": {},
        },
        "agents": {
            "available": ["python"],
            "configured": {"python": {"enabled": True, "command": ["python"]}},
        },
    }

    async def fake_rpc(method: str, params: dict[str, Any] | None = None, *, ensure: bool = True) -> Any:
        return responses[method]

    monkeypatch.setattr(cli, "_rpc", fake_rpc)
    assert await cli.cmd_jobs(argparse.Namespace(limit=50, json=False)) == 0
    assert await cli.cmd_events(argparse.Namespace(job_id="job_1", after=0, limit=10, json=False)) == 0
    assert await cli.cmd_status(argparse.Namespace(job_id="job_1", json=False)) == 0
    assert await cli.cmd_agents(argparse.Namespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "job_1" in out and "job.created" in out and "python" in out


def test_cli_logs_parser_and_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    job_id = "job_0123456789ab"
    log = cfg.logs_dir / job_id / "task" / "x.log"
    log.parent.mkdir(parents=True)
    log.write_text("hello\n")
    assert cli.cmd_logs(argparse.Namespace(job_id=job_id, latest=True, bytes=100, raw=False)) == 0
    assert "hello" in capsys.readouterr().out

    parser = cli.build_parser()
    parsed = parser.parse_args(["status", job_id, "--json"])
    assert parsed.command == "status" and parsed.json

    monkeypatch.setattr(cli, "toggle_ui", lambda: True)
    assert asyncio.run(cli.cmd_ui(argparse.Namespace())) == 0
    monkeypatch.setattr(cli, "toggle_ui", lambda: False)
    assert asyncio.run(cli.cmd_ui(argparse.Namespace())) == 1


async def test_cli_doctor_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    cfg = make_config(
        tmp_path,
        python=AgentConfig(command=("python",), review_command=("python",)),
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    async def fake_rpc(method: str, params: dict[str, Any] | None = None, *, ensure: bool = True) -> Any:
        assert method == "ping"
        return {"pid": 1, "version": "1.1.0"}

    monkeypatch.setattr(cli, "_rpc", fake_rpc)
    assert await cli.cmd_doctor(argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "agent-ready" in output and "daemon" in output
