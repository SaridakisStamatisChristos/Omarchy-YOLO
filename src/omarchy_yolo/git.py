from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import GitConfig
from .util import YoloError, ensure_private_dir


@dataclass(slots=True, frozen=True)
class CommandOutput:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False


@dataclass(slots=True, frozen=True)
class BinaryCommandOutput:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False


class GitError(YoloError):
    pass


class MergeConflict(GitError):
    pass


DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS = 120
MAX_GIT_STDOUT_BYTES = 2_000_000
MAX_GIT_STDERR_BYTES = 256_000
_GIT_TERMINATION_GRACE_SECONDS = 2.0


class GitRepo:
    def __init__(
        self,
        root: Path,
        *,
        command_timeout_seconds: int = DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS,
        allow_repository_commands: bool = True,
    ):
        self.root = root.resolve()
        self.command_timeout_seconds = max(1, command_timeout_seconds)
        self.allow_repository_commands = allow_repository_commands

    @classmethod
    def discover(
        cls,
        path: Path,
        *,
        command_timeout_seconds: int = DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS,
        allow_repository_commands: bool = True,
    ) -> "GitRepo":
        out = cls._run_static(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            timeout_seconds=command_timeout_seconds,
        )
        if out.timed_out:
            raise GitError(f"Git repository discovery timed out after {command_timeout_seconds}s")
        if out.output_truncated:
            raise GitError("Git repository discovery produced oversized output")
        if out.returncode != 0:
            raise GitError(f"not a Git repository: {path}: {out.stderr.strip()}")
        return cls(
            Path(out.stdout.strip()),
            command_timeout_seconds=command_timeout_seconds,
            allow_repository_commands=allow_repository_commands,
        )

    @staticmethod
    def _base_environment(env: dict[str, str] | None = None) -> dict[str, str]:
        effective = os.environ.copy() if env is None else dict(env)
        identity = {
            key: value
            for key, value in effective.items()
            if key
            in {
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
            }
        }
        for key in tuple(effective):
            if key.startswith("GIT_"):
                effective.pop(key, None)
        effective.update(identity)
        effective["GIT_PAGER"] = "cat"
        effective["GIT_TERMINAL_PROMPT"] = "0"
        effective["GIT_ASKPASS"] = "/bin/false"
        effective["SSH_ASKPASS"] = "/bin/false"
        effective["GIT_CONFIG_NOSYSTEM"] = "1"
        effective["GIT_CONFIG_GLOBAL"] = os.devnull
        return effective

    @staticmethod
    def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _GIT_TERMINATION_GRACE_SECONDS
        if proc.poll() is None:
            try:
                proc.wait(timeout=_GIT_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        if proc.poll() is None:
            proc.wait()

    @classmethod
    def _run_static_bytes(
        cls,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float = DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS,
        max_stdout_bytes: int = MAX_GIT_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_GIT_STDERR_BYTES,
    ) -> BinaryCommandOutput:
        if timeout_seconds <= 0:
            raise ValueError("Git command timeout must be positive")
        if max_stdout_bytes < 1 or max_stderr_bytes < 1:
            raise ValueError("Git output limits must be positive")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cls._base_environment(env),
            start_new_session=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        output_truncated = threading.Event()
        buffer_lock = threading.Lock()
        pump_errors: list[BaseException] = []

        def pump(stream: BinaryIO, target: bytearray, limit: int) -> None:
            try:
                while True:
                    chunk = stream.read(65_536)
                    if not chunk:
                        return
                    with buffer_lock:
                        remaining = max(0, limit - len(target))
                        target.extend(chunk[:remaining])
                        overflow = len(chunk) > remaining
                    if overflow:
                        output_truncated.set()
                        cls._terminate_process_group(proc)
                        return
            except BaseException as exc:
                with buffer_lock:
                    pump_errors.append(exc)
                cls._terminate_process_group(proc)

        stdout_thread = threading.Thread(
            target=pump,
            args=(proc.stdout, stdout, max_stdout_bytes),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=pump,
            args=(proc.stderr, stderr, max_stderr_bytes),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            cls._terminate_process_group(proc)
        finally:
            stdout_thread.join(timeout=_GIT_TERMINATION_GRACE_SECONDS)
            stderr_thread.join(timeout=_GIT_TERMINATION_GRACE_SECONDS)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                cls._terminate_process_group(proc)
                proc.stdout.close()
                proc.stderr.close()
                stdout_thread.join(timeout=_GIT_TERMINATION_GRACE_SECONDS)
                stderr_thread.join(timeout=_GIT_TERMINATION_GRACE_SECONDS)

        if pump_errors and not timed_out:
            raise GitError(
                f"cannot capture bounded Git output: {type(pump_errors[0]).__name__}"
            )

        return BinaryCommandOutput(
            proc.returncode if proc.returncode is not None else 124,
            bytes(stdout),
            bytes(stderr),
            timed_out,
            output_truncated.is_set(),
        )

    @classmethod
    def _run_static(
        cls,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float = DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS,
        max_stdout_bytes: int = MAX_GIT_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_GIT_STDERR_BYTES,
    ) -> CommandOutput:
        out = cls._run_static_bytes(
            argv,
            env=env,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        return CommandOutput(
            out.returncode,
            out.stdout.decode("utf-8", errors="replace"),
            out.stderr.decode("utf-8", errors="replace"),
            out.timed_out,
            out.output_truncated,
        )

    def _repository_command_overrides(self, directory: Path) -> list[str]:
        if self.allow_repository_commands:
            return []
        pattern = r"^(filter\..*\.(clean|smudge|process|required)|merge\..*\.driver)$"
        out = self._run_static_bytes(
            [
                "git",
                "-C",
                str(directory),
                "config",
                "--includes",
                "--name-only",
                "--null",
                "--get-regexp",
                pattern,
            ],
            timeout_seconds=self.command_timeout_seconds,
            max_stdout_bytes=256_000,
            max_stderr_bytes=64_000,
        )
        if out.timed_out:
            raise GitError("timed out while inspecting repository command configuration")
        if out.output_truncated:
            raise GitError("repository command configuration exceeds the safety ceiling")
        if out.returncode not in {0, 1}:
            detail = out.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"cannot inspect repository command configuration: {detail}")
        overrides: list[str] = []
        for raw_key in out.stdout.split(b"\0"):
            if not raw_key:
                continue
            key = os.fsdecode(raw_key)
            lower = key.lower()
            if lower.startswith("filter.") and lower.endswith((".clean", ".smudge")):
                value = "/usr/bin/cat"
            elif lower.startswith("filter.") and lower.endswith(".process"):
                value = ""
            elif lower.startswith("filter.") and lower.endswith(".required"):
                value = "false"
            elif lower.startswith("merge.") and lower.endswith(".driver"):
                value = "/usr/bin/false"
            else:
                continue
            overrides.extend(["-c", f"{key}={value}"])
        return overrides

    def run_bytes(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
        max_stdout_bytes: int = MAX_GIT_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_GIT_STDERR_BYTES,
    ) -> BinaryCommandOutput:
        directory = (cwd or self.root).resolve()
        argv = [
            "git",
            "-C",
            str(directory),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-c",
            "interactive.diffFilter=",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.quotePath=true",
            *self._repository_command_overrides(directory),
            *args,
        ]
        out = self._run_static_bytes(
            argv,
            env=env,
            timeout_seconds=self.command_timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        command = " ".join(args[:8])
        if check and out.timed_out:
            raise GitError(
                f"git {command} timed out after {self.command_timeout_seconds} seconds"
            )
        if check and out.output_truncated:
            raise GitError(f"git {command} exceeded the bounded output limit")
        if check and out.returncode != 0:
            detail = out.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {command} failed: {detail}")
        return out

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> CommandOutput:
        out = self.run_bytes(
            *args,
            cwd=cwd,
            check=check,
            env=env,
        )
        return CommandOutput(
            out.returncode,
            out.stdout.decode("utf-8", errors="replace"),
            out.stderr.decode("utf-8", errors="replace"),
            out.timed_out,
            out.output_truncated,
        )

    def _run_stdout_bounded(
        self,
        args: list[str],
        *,
        cwd: Path,
        max_bytes: int,
    ) -> tuple[str, bool]:
        out = self.run_bytes(
            *args,
            cwd=cwd,
            check=False,
            max_stdout_bytes=max_bytes,
            max_stderr_bytes=64_000,
        )
        command = " ".join(args[:8])
        if out.timed_out:
            raise GitError(
                f"git {command} timed out after {self.command_timeout_seconds} seconds"
            )
        if out.returncode != 0 and not out.output_truncated:
            detail = out.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {command} failed: {detail}")
        return out.stdout.decode("utf-8", errors="replace"), out.output_truncated

    def _common_dir(self, cwd: Path) -> Path:
        out = self.run("rev-parse", "--git-common-dir", cwd=cwd)
        value = Path(out.stdout.strip())
        if not value.is_absolute():
            value = cwd / value
        return value.resolve()

    def _root_common_dir(self) -> Path:
        return self._common_dir(self.root)

    def assert_worktree(self, path: Path, expected_branch: str | None = None) -> None:
        resolved = path.resolve()
        out = self._run_static(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            timeout_seconds=self.command_timeout_seconds,
        )
        if (
            out.timed_out
            or out.output_truncated
            or out.returncode != 0
            or Path(out.stdout.strip()).resolve() != resolved
        ):
            raise GitError(f"refusing path that is not a Git worktree root: {path}")
        if self._common_dir(resolved) != self._root_common_dir():
            raise GitError(f"refusing worktree belonging to a different repository: {path}")
        if expected_branch is not None and self.branch(resolved) != expected_branch:
            raise GitError(
                f"worktree branch mismatch at {path}: expected {expected_branch}, got {self.branch(resolved)}"
            )

    def _validate_branch_name(self, branch: str) -> None:
        out = self.run("check-ref-format", "--branch", branch, check=False)
        if out.returncode != 0:
            raise GitError(f"invalid Git branch name: {branch}")

    def head(self, cwd: Path | None = None) -> str:
        return self.run("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def branch(self, cwd: Path | None = None) -> str:
        out = self.run("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd, check=False)
        if out.timed_out or out.output_truncated:
            raise GitError("cannot determine current branch within bounded Git limits")
        if out.returncode == 0:
            return out.stdout.strip()
        if out.returncode == 1:
            return "HEAD"
        raise GitError(f"cannot determine current branch: {out.stderr.strip()}")

    def is_clean(self, cwd: Path | None = None) -> bool:
        return not self.run("status", "--porcelain=v1", cwd=cwd).stdout.strip()

    def preflight(self, *, require_clean: bool) -> tuple[str, str]:
        if require_clean and not self.is_clean():
            raise GitError("repository has uncommitted changes; commit/stash them or disable require_clean_repo")
        return self.branch(), self.head()

    def branch_exists(self, branch: str) -> bool:
        self._validate_branch_name(branch)
        out = self.run(
            "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        )
        if out.timed_out or out.output_truncated:
            raise GitError("cannot determine branch existence within bounded Git limits")
        if out.returncode not in {0, 1}:
            raise GitError(f"cannot inspect branch {branch}: {out.stderr.strip()}")
        return out.returncode == 0

    def ensure_worktree(self, path: Path, branch: str, ref: str) -> None:
        self._validate_branch_name(branch)
        ensure_private_dir(path.parent)
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise GitError(f"refusing symlink at worktree path: {path}")
            self.assert_worktree(path)
            self.remove_worktree(path, force=True)
        if self.branch_exists(branch):
            raise GitError(f"refusing to overwrite existing branch: {branch}")
        self.run("worktree", "add", "-b", branch, str(path), ref)
        self.assert_worktree(path, branch)

    def ensure_existing_branch_worktree(self, path: Path, branch: str, ref: str) -> None:
        self._validate_branch_name(branch)
        ensure_private_dir(path.parent)
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise GitError(f"refusing symlink at worktree path: {path}")
            if (path / ".git").exists():
                self.assert_worktree(path, branch)
                return
            raise GitError(f"refusing to delete unrelated directory at worktree path: {path}")
        self.run("worktree", "prune")
        if not self.branch_exists(branch):
            self.run("branch", "--", branch, ref)
        self.run("worktree", "add", str(path), branch)
        self.assert_worktree(path, branch)

    def remove_worktree(self, path: Path, *, force: bool = True) -> None:
        if not path.exists() and not path.is_symlink():
            self.run("worktree", "prune")
            return
        if path.is_symlink():
            raise GitError(f"refusing symlink at worktree path: {path}")
        self.assert_worktree(path)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.extend(["--", str(path)])
        out = self.run(*args, check=False)
        if out.timed_out:
            raise GitError(
                f"worktree removal timed out after {self.command_timeout_seconds} seconds"
            )
        if out.output_truncated:
            raise GitError("worktree removal produced oversized output")
        if out.returncode != 0:
            self.assert_worktree(path)
            shutil.rmtree(path)
            self.run("worktree", "prune")

    def delete_branch(self, branch: str) -> None:
        self._validate_branch_name(branch)
        if self.branch_exists(branch):
            self.run("branch", "-D", "--", branch)

    @staticmethod
    def _identity_environment(cfg: GitConfig) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": cfg.commit_name,
                "GIT_AUTHOR_EMAIL": cfg.commit_email,
                "GIT_COMMITTER_NAME": cfg.commit_name,
                "GIT_COMMITTER_EMAIL": cfg.commit_email,
            }
        )
        return env

    def commit_all(self, cwd: Path, message: str, cfg: GitConfig) -> str:
        self.assert_worktree(cwd)
        if self.is_clean(cwd):
            return self.head(cwd)
        self.run("add", "-A", cwd=cwd)
        self.run(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
            cwd=cwd,
            env=self._identity_environment(cfg),
        )
        return self.head(cwd)

    def diff(self, cwd: Path, base: str, *, max_bytes: int = 70_000) -> str:
        self.assert_worktree(cwd)
        max_bytes = min(80_000, max(1_000, max_bytes))
        stat, stat_truncated = self._run_stdout_bounded(
            ["diff", "--stat", "--stat-count=100", f"{base}..HEAD"],
            cwd=cwd,
            max_bytes=min(10_000, max_bytes // 5),
        )
        remaining = max(1_000, max_bytes - len(stat.encode("utf-8")) - 32)
        patch, patch_truncated = self._run_stdout_bounded(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                "--find-copies",
                f"{base}..HEAD",
            ],
            cwd=cwd,
            max_bytes=remaining,
        )
        combined = f"DIFF STAT\n{stat}\nPATCH\n{patch}"
        if stat_truncated or patch_truncated:
            combined += "\n...[diff truncated]...\n"
        return combined

    def changed_files(self, cwd: Path, base: str) -> list[str]:
        self.assert_worktree(cwd)
        out = self.run_bytes("diff", "--name-only", "-z", f"{base}..HEAD", cwd=cwd).stdout
        return [os.fsdecode(item) for item in out.split(b"\0") if item]

    def merge(
        self,
        integration_cwd: Path,
        branch: str,
        cfg: GitConfig | None = None,
    ) -> None:
        self.assert_worktree(integration_cwd)
        self._validate_branch_name(branch)
        out = self.run(
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--no-ff",
            "--no-edit",
            "--no-gpg-sign",
            "--",
            branch,
            cwd=integration_cwd,
            check=False,
            env=self._identity_environment(cfg or GitConfig()),
        )
        if out.timed_out:
            raise GitError(
                f"integration merge timed out after {self.command_timeout_seconds} seconds"
            )
        if out.output_truncated:
            raise GitError("integration merge produced oversized output")
        if out.returncode != 0:
            unresolved = self.unresolved_files(integration_cwd)
            if unresolved:
                raise MergeConflict("merge conflict: " + ", ".join(unresolved))
            raise GitError(f"merge failed: {out.stderr.strip()}")

    def unresolved_files(self, cwd: Path) -> list[str]:
        self.assert_worktree(cwd)
        out = self.run_bytes(
            "diff", "--name-only", "--diff-filter=U", "-z", cwd=cwd
        ).stdout
        return [os.fsdecode(item) for item in out.split(b"\0") if item]

    def merge_in_progress(self, cwd: Path) -> bool:
        self.assert_worktree(cwd)
        raw = self.run("rev-parse", "--git-path", "MERGE_HEAD", cwd=cwd).stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = cwd / path
        return path.exists()

    def finish_merge(self, cwd: Path, cfg: GitConfig) -> None:
        self.assert_worktree(cwd)
        unresolved = self.unresolved_files(cwd)
        if unresolved:
            raise MergeConflict("unresolved merge files remain: " + ", ".join(unresolved))
        self.run("add", "-A", cwd=cwd)
        if self.merge_in_progress(cwd):
            self.run(
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--no-gpg-sign",
                "--no-edit",
                cwd=cwd,
                env=self._identity_environment(cfg),
            )
        elif not self.is_clean(cwd):
            self.commit_all(cwd, "yolo: resolve integration", cfg)

    def abort_merge(self, cwd: Path) -> None:
        self.assert_worktree(cwd)
        if self.merge_in_progress(cwd):
            self.run("merge", "--abort", cwd=cwd)

    def reset_hard(self, cwd: Path, commit: str) -> None:
        self.assert_worktree(cwd)
        self.run("reset", "--hard", commit, cwd=cwd)
        self.run("clean", "-ffdx", cwd=cwd)

    def resolve_branch_commit(self, branch: str) -> str:
        self._validate_branch_name(branch)
        out = self.run("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
        commit = out.stdout.strip()
        if not commit:
            raise GitError(f"cannot resolve branch commit: {branch}")
        return commit

    def can_fast_forward_source(self, base_branch: str, base_commit: str) -> tuple[bool, str]:
        if base_branch == "HEAD":
            return False, "source checkout was detached"
        current_branch = self.branch()
        if current_branch != base_branch:
            return False, f"source worktree is now on {current_branch}, not {base_branch}"
        if not self.is_clean():
            return False, "source worktree is dirty"
        if self.head() != base_commit:
            return False, "source branch moved since job creation"
        return True, "ok"

    def reconcile_fast_forward_source(
        self,
        integration_branch: str,
        base_branch: str,
        base_commit: str,
        accepted_commit: str,
    ) -> tuple[str, str]:
        """Idempotently reconcile a journaled source-apply operation.

        This deliberately distinguishes policy skips from Git execution failures.
        A hard crash can occur after the ff-only merge but before SQLite publication;
        seeing the source already at ``accepted_commit`` is therefore success, not a
        moved-branch refusal.
        """
        integration_commit = self.resolve_branch_commit(integration_branch)
        if integration_commit != accepted_commit:
            raise GitError(
                "accepted integration branch moved after acceptance: "
                f"expected {accepted_commit}, got {integration_commit}"
            )
        if base_branch == "HEAD":
            return "skipped", "source checkout was detached"
        current_branch = self.branch()
        if current_branch != base_branch:
            return "skipped", f"source worktree is now on {current_branch}, not {base_branch}"
        if not self.is_clean():
            return "skipped", "source worktree is dirty"

        current_head = self.head()
        if current_head == accepted_commit:
            return "applied", "source already at accepted commit"
        if current_head != base_commit:
            return "skipped", "source branch moved since job creation"

        self.run(
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--ff-only",
            "--",
            integration_branch,
        )
        final_head = self.head()
        if final_head != accepted_commit:
            raise GitError(
                "source fast-forward completed at unexpected commit: "
                f"expected {accepted_commit}, got {final_head}"
            )
        return "applied", "source fast-forwarded to accepted commit"

    def fast_forward_source(self, integration_branch: str, base_branch: str, base_commit: str) -> None:
        accepted_commit = self.resolve_branch_commit(integration_branch)
        outcome, reason = self.reconcile_fast_forward_source(
            integration_branch,
            base_branch,
            base_commit,
            accepted_commit,
        )
        if outcome != "applied":
            raise GitError(f"automatic apply refused: {reason}")
