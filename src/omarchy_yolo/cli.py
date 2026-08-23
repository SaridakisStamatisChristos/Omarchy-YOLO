from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .daemon import run_daemon
from .omarchy import toggle_ui
from .rpc import RpcError, RpcUnavailable, rpc_call
from .util import YoloError, ensure_private_dir, terminal_safe, validate_job_id, xdg_runtime_dir


TERMINAL_STATES = {"completed", "failed", "stopped"}


def socket_path() -> Path:
    return xdg_runtime_dir() / "omarchy-yolo.sock"


async def _rpc(method: str, params: dict[str, Any] | None = None, *, ensure: bool = True) -> Any:
    path = socket_path()
    try:
        return await rpc_call(path, method, params)
    except RpcUnavailable:
        if not ensure:
            raise
    await _ensure_daemon()
    return await rpc_call(path, method, params)


async def _ensure_daemon() -> None:
    config = load_config()
    ensure_private_dir(config.state_dir)
    systemctl = shutil.which("systemctl")
    if systemctl:
        await asyncio.to_thread(
            subprocess.run,
            [systemctl, "--user", "start", "omarchy-yolo.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(8):
            try:
                await rpc_call(socket_path(), "ping")
                return
            except RpcError:
                await asyncio.sleep(0.1)

    log_path = config.logs_dir / "daemon-bootstrap.log"
    ensure_private_dir(log_path.parent)
    with log_path.open("ab") as log:
        await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-m", "omarchy_yolo.cli", "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    for _ in range(20):
        try:
            await rpc_call(socket_path(), "ping")
            return
        except RpcError:
            await asyncio.sleep(0.1)
    raise RpcError(f"unable to start daemon; inspect {log_path}")


def _print_status(status: dict[str, Any]) -> None:
    job = status.get("job")
    if not job:
        print("No YOLO jobs yet.")
        return
    print(f"{terminal_safe(job['id'], single_line=True)}  {terminal_safe(job['state'], single_line=True)}  {terminal_safe(Path(job['repo']).name, single_line=True)}")
    print(f"goal: {terminal_safe(job['goal'])}")
    print(f"candidate: {terminal_safe(job['integration_branch'], single_line=True)}")
    counts = status.get("counts", {})
    if counts:
        print("tasks: " + "  ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    for task in status.get("tasks", []):
        agent = task.get("preferred_agent") or "auto"
        print(
            f"  {terminal_safe(task['logical_id'], single_line=True):<8} {terminal_safe(task['state'], single_line=True):<12} "
            f"attempts={task['attempts']} agent={terminal_safe(agent, single_line=True)}  {terminal_safe(task['title'], single_line=True)}"
        )
    if job.get("final_summary"):
        print(f"summary: {terminal_safe(job['final_summary'])}")
    if job.get("error"):
        print(f"error: {terminal_safe(job['error'])}", file=sys.stderr)


async def cmd_run(args: argparse.Namespace) -> int:
    status = await _rpc(
        "submit",
        {"goal": args.goal, "repo": str(Path(args.repo).resolve()), "auto_apply": args.apply},
    )
    _print_status(status)
    job_id = status["job"]["id"]
    if args.watch:
        return await _watch(job_id)
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    status = await _rpc("status", {"job_id": args.job_id} if args.job_id else {})
    if args.json:
        print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    else:
        _print_status(status)
    return 0


async def cmd_jobs(args: argparse.Namespace) -> int:
    jobs = await _rpc("jobs", {"limit": args.limit})
    if args.json:
        print(json.dumps(jobs, ensure_ascii=False, separators=(",", ":")))
    else:
        for job in jobs:
            print(
                f"{terminal_safe(job['id'], single_line=True)}  {terminal_safe(job['state'], single_line=True):<10}  "
                f"{terminal_safe(Path(job['repo']).name, single_line=True):<24}  "
                f"{terminal_safe(job['goal'], single_line=True, max_chars=80)}"
            )
    return 0


async def cmd_events(args: argparse.Namespace) -> int:
    events = await _rpc(
        "events", {"job_id": args.job_id, "after_id": args.after, "limit": args.limit}
    )
    if args.json:
        print(json.dumps(events, ensure_ascii=False, separators=(",", ":")))
    else:
        for event in events:
            payload = terminal_safe(
                json.dumps(event["payload"], ensure_ascii=False), single_line=True
            )
            task = (
                f" task={terminal_safe(event['task_id'], single_line=True)}"
                if event.get("task_id")
                else ""
            )
            print(
                f"{event['id']:>6}  {terminal_safe(event['kind'], single_line=True)}{task}  {payload}"
            )
    return 0


async def _watch(job_id: str) -> int:
    after = 0
    while True:
        events = await _rpc("events", {"job_id": job_id, "after_id": after, "limit": 200})
        for event in events:
            after = max(after, int(event["id"]))
            payload = event.get("payload", {})
            suffix = (
                " " + terminal_safe(json.dumps(payload, ensure_ascii=False), single_line=True)
                if payload
                else ""
            )
            print(
                f"[{event['id']:05d}] {terminal_safe(event['kind'], single_line=True)}{suffix}",
                flush=True,
            )
        status = await _rpc("status", {"job_id": job_id})
        state = status["job"]["state"]
        if state in TERMINAL_STATES:
            _print_status(status)
            return 0 if state == "completed" else 1
        await asyncio.sleep(1.0)


async def cmd_watch(args: argparse.Namespace) -> int:
    return await _watch(args.job_id)


async def cmd_stop(args: argparse.Namespace) -> int:
    status = await _rpc("stop", {"job_id": args.job_id})
    _print_status(status)
    return 0


async def cmd_resume(args: argparse.Namespace) -> int:
    status = await _rpc("resume", {"job_id": args.job_id})
    _print_status(status)
    return await _watch(args.job_id) if args.watch else 0


async def cmd_agents(args: argparse.Namespace) -> int:
    data = await _rpc("agents")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    else:
        available = set(data["available"])
        for name, cfg in data["configured"].items():
            state = "ready" if name in available else ("disabled" if not cfg["enabled"] else "missing")
            display_name = terminal_safe(name, single_line=True)
            command = terminal_safe(" ".join(cfg["command"]), single_line=True)
            print(f"{display_name:<12} {state:<9} {command}")
    return 0


async def cmd_doctor(_: argparse.Namespace) -> int:
    cfg = load_config()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 12), sys.version.split()[0]))
    checks.append(("linux", sys.platform.startswith("linux"), sys.platform))
    checks.append(("non-root", os.geteuid() != 0, f"uid={os.geteuid()}"))
    checks.append(("git", shutil.which("git") is not None, shutil.which("git") or "missing"))
    checks.append(("config", True, str(cfg.config_path)))
    checks.append(("state", True, str(cfg.state_dir)))
    if cfg.sandbox.backend == "bwrap":
        checks.append(("bubblewrap", shutil.which("bwrap") is not None, shutil.which("bwrap") or "missing"))
    checks.append(("omarchy-shell", shutil.which("omarchy-shell") is not None, shutil.which("omarchy-shell") or "not on PATH"))
    plugin = Path.home() / ".config/omarchy/plugins/dev.aether.yolo/manifest.json"
    checks.append(("shell-plugin", plugin.exists(), str(plugin)))

    try:
        ping = await _rpc("ping", ensure=False)
        checks.append(("daemon", True, f"pid={ping['pid']} version={ping['version']}"))
    except RpcError:
        checks.append(("daemon", False, "not running"))

    ready = True
    available_agents = 0
    for name, cfg_agent in cfg.agents.items():
        if not cfg_agent.enabled:
            continue
        executable = cfg_agent.command[0] if cfg_agent.command else ""
        resolved = shutil.which(executable) if executable else None
        exists = resolved is not None
        available_agents += int(exists)
        checks.append((f"agent:{name}", exists, resolved or "missing"))
    checks.append(("agent-ready", available_agents > 0, f"{available_agents} usable agent(s)"))

    for name, ok, detail in checks:
        marker = "OK" if ok else "!!"
        print(f"[{marker}] {name:<16} {terminal_safe(detail, single_line=True)}")
        if name in {"python", "linux", "non-root", "git", "agent-ready"} and not ok:
            ready = False
    return 0 if ready else 1


async def cmd_ui(_: argparse.Namespace) -> int:
    if not toggle_ui():
        print("YOLO Omarchy panel is not loaded. Run ./install.sh or enable dev.aether.yolo.", file=sys.stderr)
        return 1
    return 0


async def cmd_daemon(_: argparse.Namespace) -> int:
    await run_daemon()
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    cfg = load_config()
    job_id = validate_job_id(args.job_id)
    root = cfg.logs_dir / job_id
    if not root.exists():
        print(f"No logs for {job_id}", file=sys.stderr)
        return 1
    files = sorted(root.rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if args.latest and files:
        count = min(2_000_000, max(1, args.bytes))
        text = files[0].read_text(errors="replace")[-count:]
        print(text if args.raw else terminal_safe(text, max_chars=2_000_000))
        return 0
    for path in files:
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yolo", description="Omarchy YOLO multi-agent orchestrator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="submit an autonomous engineering goal")
    run.add_argument("goal")
    run.add_argument("--repo", default=".")
    apply_group = run.add_mutually_exclusive_group()
    apply_group.add_argument("--apply", dest="apply", action="store_true", help="fast-forward source branch when clean and unchanged")
    apply_group.add_argument("--no-apply", dest="apply", action="store_false")
    run.set_defaults(apply=None, handler=cmd_run)
    run.add_argument("--watch", action="store_true")

    status = sub.add_parser("status", help="show a job (latest by default)")
    status.add_argument("job_id", nargs="?")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)

    jobs = sub.add_parser("jobs", help="list jobs")
    jobs.add_argument("--limit", type=int, default=50)
    jobs.add_argument("--json", action="store_true")
    jobs.set_defaults(handler=cmd_jobs)

    events = sub.add_parser("events", help="show the durable event stream")
    events.add_argument("job_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=200)
    events.add_argument("--json", action="store_true")
    events.set_defaults(handler=cmd_events)

    watch = sub.add_parser("watch", help="follow a job until it finishes")
    watch.add_argument("job_id")
    watch.set_defaults(handler=cmd_watch)

    stop = sub.add_parser("stop", help="stop a running job and its workers")
    stop.add_argument("job_id")
    stop.set_defaults(handler=cmd_stop)

    resume = sub.add_parser("resume", help="resume/retry a stopped or failed job")
    resume.add_argument("job_id")
    resume.add_argument("--watch", action="store_true")
    resume.set_defaults(handler=cmd_resume)

    agents = sub.add_parser("agents", help="show configured agent CLIs")
    agents.add_argument("--json", action="store_true")
    agents.set_defaults(handler=cmd_agents)

    doctor = sub.add_parser("doctor", help="validate local integration")
    doctor.set_defaults(handler=cmd_doctor)

    ui = sub.add_parser("ui", help="toggle the Omarchy YOLO panel")
    ui.set_defaults(handler=cmd_ui)

    daemon = sub.add_parser("daemon", help="run the persistent daemon (normally systemd-managed)")
    daemon.set_defaults(handler=cmd_daemon)

    logs = sub.add_parser("logs", help="list or tail job logs")
    logs.add_argument("job_id")
    logs.add_argument("--latest", action="store_true")
    logs.add_argument("--bytes", type=int, default=30000)
    logs.add_argument("--raw", action="store_true", help="do not strip terminal control sequences")
    logs.set_defaults(sync_handler=cmd_logs)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if hasattr(args, "sync_handler"):
            code = int(args.sync_handler(args))
        else:
            code = int(asyncio.run(args.handler(args)))
    except KeyboardInterrupt:
        code = 130
    except (YoloError, RpcError, KeyError) as exc:
        print(f"yolo: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
