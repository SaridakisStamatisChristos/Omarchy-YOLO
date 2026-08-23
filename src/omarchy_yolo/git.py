from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import GitConfig
from .util import YoloError, ensure_private_dir


@dataclass(slots=True, frozen=True)
class CommandOutput:
    returncode: int
    stdout: str
    stderr: str


class GitError(YoloError):
    pass


class MergeConflict(GitError):
    pass


class GitRepo:
    def __init__(self, root: Path):
        self.root = root.resolve()

    @classmethod
    def discover(cls, path: Path) -> "GitRepo":
        out = cls._run_static(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        if out.returncode != 0:
            raise GitError(f"not a Git repository: {path}: {out.stderr.strip()}")
        return cls(Path(out.stdout.strip()))

    @staticmethod
    def _run_static(argv: list[str], *, env: dict[str, str] | None = None) -> CommandOutput:
        proc = subprocess.run(argv, text=True, capture_output=True, env=env, check=False)
        return CommandOutput(proc.returncode, proc.stdout, proc.stderr)

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> CommandOutput:
        directory = cwd or self.root
        effective_env = os.environ.copy() if env is None else dict(env)
        effective_env.setdefault("GIT_PAGER", "cat")
        effective_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        out = self._run_static(
            [
                "git",
                "-C",
                str(directory),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "diff.external=",
                *args,
            ],
            env=effective_env,
        )
        if check and out.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
        return out

    def _run_stdout_bounded(
        self,
        args: list[str],
        *,
        cwd: Path,
        max_bytes: int,
    ) -> tuple[str, bool]:
        argv = [
            "git",
            "-C",
            str(cwd),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            *args,
        ]
        env = os.environ.copy()
        env.setdefault("GIT_PAGER", "cat")
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        assert proc.stdout is not None
        data = proc.stdout.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        if truncated and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        return data[:max_bytes].decode("utf-8", errors="replace"), truncated

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
        out = self._run_static(["git", "-C", str(resolved), "rev-parse", "--show-toplevel"])
        if out.returncode != 0 or Path(out.stdout.strip()).resolve() != resolved:
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
        return out.stdout.strip() if out.returncode == 0 else "HEAD"

    def is_clean(self, cwd: Path | None = None) -> bool:
        return not self.run("status", "--porcelain=v1", cwd=cwd).stdout.strip()

    def preflight(self, *, require_clean: bool) -> tuple[str, str]:
        if require_clean and not self.is_clean():
            raise GitError("repository has uncommitted changes; commit/stash them or disable require_clean_repo")
        return self.branch(), self.head()

    def branch_exists(self, branch: str) -> bool:
        self._validate_branch_name(branch)
        return self.run(
            "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        ).returncode == 0

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
        self.run("worktree", "prune", check=False)
        if not self.branch_exists(branch):
            self.run("branch", "--", branch, ref)
        self.run("worktree", "add", str(path), branch)
        self.assert_worktree(path, branch)

    def remove_worktree(self, path: Path, *, force: bool = True) -> None:
        if not path.exists() and not path.is_symlink():
            self.run("worktree", "prune", check=False)
            return
        if path.is_symlink():
            raise GitError(f"refusing symlink at worktree path: {path}")
        self.assert_worktree(path)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.extend(["--", str(path)])
        out = self.run(*args, check=False)
        if out.returncode != 0:
            # Only fall back after proving this directory belongs to this repository.
            self.assert_worktree(path)
            shutil.rmtree(path)
            self.run("worktree", "prune", check=False)

    def delete_branch(self, branch: str) -> None:
        self._validate_branch_name(branch)
        if self.branch_exists(branch):
            self.run("branch", "-D", "--", branch, check=False)

    def commit_all(self, cwd: Path, message: str, cfg: GitConfig) -> str:
        self.assert_worktree(cwd)
        if self.is_clean(cwd):
            return self.head(cwd)
        self.run("add", "-A", cwd=cwd)
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": cfg.commit_name,
                "GIT_AUTHOR_EMAIL": cfg.commit_email,
                "GIT_COMMITTER_NAME": cfg.commit_name,
                "GIT_COMMITTER_EMAIL": cfg.commit_email,
            }
        )
        self.run(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
            cwd=cwd,
            env=env,
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
        out = self.run("diff", "--name-only", f"{base}..HEAD", cwd=cwd, check=False).stdout
        return [line for line in out.splitlines() if line.strip()]

    def merge(self, integration_cwd: Path, branch: str) -> None:
        self.assert_worktree(integration_cwd)
        self._validate_branch_name(branch)
        out = self.run(
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--no-ff",
            "--no-edit",
            "--",
            branch,
            cwd=integration_cwd,
            check=False,
        )
        if out.returncode != 0:
            unresolved = self.unresolved_files(integration_cwd)
            if unresolved:
                raise MergeConflict("merge conflict: " + ", ".join(unresolved))
            raise GitError(f"merge failed: {out.stderr.strip()}")

    def unresolved_files(self, cwd: Path) -> list[str]:
        self.assert_worktree(cwd)
        out = self.run("diff", "--name-only", "--diff-filter=U", cwd=cwd, check=False).stdout
        return [line for line in out.splitlines() if line.strip()]

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
            env = os.environ.copy()
            env.update(
                {
                    "GIT_AUTHOR_NAME": cfg.commit_name,
                    "GIT_AUTHOR_EMAIL": cfg.commit_email,
                    "GIT_COMMITTER_NAME": cfg.commit_name,
                    "GIT_COMMITTER_EMAIL": cfg.commit_email,
                }
            )
            self.run(
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--no-gpg-sign",
                "--no-edit",
                cwd=cwd,
                env=env,
            )
        elif not self.is_clean(cwd):
            self.commit_all(cwd, "yolo: resolve integration", cfg)

    def abort_merge(self, cwd: Path) -> None:
        self.assert_worktree(cwd)
        if self.merge_in_progress(cwd):
            self.run("merge", "--abort", cwd=cwd, check=False)

    def reset_hard(self, cwd: Path, commit: str) -> None:
        self.assert_worktree(cwd)
        self.run("reset", "--hard", commit, cwd=cwd)
        self.run("clean", "-ffdx", cwd=cwd, check=False)

    def can_fast_forward_source(self, base_branch: str, base_commit: str) -> tuple[bool, str]:
        if base_branch == "HEAD":
            return False, "source checkout was detached"
        if self.branch() != base_branch:
            return False, f"source worktree is now on {self.branch()}, not {base_branch}"
        if not self.is_clean():
            return False, "source worktree is dirty"
        if self.head() != base_commit:
            return False, "source branch moved since job creation"
        return True, "ok"

    def fast_forward_source(self, integration_branch: str, base_branch: str, base_commit: str) -> None:
        self._validate_branch_name(integration_branch)
        ok, reason = self.can_fast_forward_source(base_branch, base_commit)
        if not ok:
            raise GitError(f"automatic apply refused: {reason}")
        self.run(
            "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "--", integration_branch
        )
