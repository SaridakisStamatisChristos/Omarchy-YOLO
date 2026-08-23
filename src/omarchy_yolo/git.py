from __future__ import annotations

import os
import shutil
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

    def run(self, *args: str, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None) -> CommandOutput:
        directory = cwd or self.root
        out = self._run_static(["git", "-C", str(directory), *args], env=env)
        if check and out.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
        return out

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
        return self.run("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

    def ensure_worktree(self, path: Path, branch: str, ref: str) -> None:
        ensure_private_dir(path.parent)
        if path.exists(): self.remove_worktree(path, force=True)
        if self.branch_exists(branch): self.run("branch", "-D", branch)
        self.run("worktree", "add", "-b", branch, str(path), ref)

    def ensure_existing_branch_worktree(self, path: Path, branch: str, ref: str) -> None:
        ensure_private_dir(path.parent)
        if path.exists() and (path / ".git").exists(): return
        if path.exists(): shutil.rmtree(path)
        self.run("worktree", "prune", check=False)
        if not self.branch_exists(branch): self.run("branch", branch, ref)
        self.run("worktree", "add", str(path), branch)

    def remove_worktree(self, path: Path, *, force: bool = True) -> None:
        if not path.exists():
            self.run("worktree", "prune", check=False); return
        args = ["worktree", "remove"]
        if force: args.append("--force")
        args.append(str(path))
        out = self.run(*args, check=False)
        if out.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            self.run("worktree", "prune", check=False)

    def delete_branch(self, branch: str) -> None:
        if self.branch_exists(branch): self.run("branch", "-D", branch, check=False)

    def commit_all(self, cwd: Path, message: str, cfg: GitConfig) -> str:
        if self.is_clean(cwd): return self.head(cwd)
        self.run("add", "-A", cwd=cwd)
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_NAME": cfg.commit_name, "GIT_AUTHOR_EMAIL": cfg.commit_email, "GIT_COMMITTER_NAME": cfg.commit_name, "GIT_COMMITTER_EMAIL": cfg.commit_email})
        self.run("-c", "core.hooksPath=/dev/null", "commit", "--no-gpg-sign", "-m", message, cwd=cwd, env=env)
        return self.head(cwd)

    def diff(self, cwd: Path, base: str, *, max_bytes: int = 250_000) -> str:
        stat = self.run("diff", "--stat", f"{base}..HEAD", cwd=cwd, check=False).stdout
        patch = self.run("diff", "--no-ext-diff", "--find-renames", "--find-copies", f"{base}..HEAD", cwd=cwd, check=False).stdout
        combined = f"DIFF STAT\n{stat}\nPATCH\n{patch}"
        if len(combined.encode("utf-8")) <= max_bytes: return combined
        encoded = combined.encode("utf-8")[:max_bytes]
        return encoded.decode("utf-8", errors="ignore") + "\n...[diff truncated]...\n"

    def changed_files(self, cwd: Path, base: str) -> list[str]:
        out = self.run("diff", "--name-only", f"{base}..HEAD", cwd=cwd, check=False).stdout
        return [line for line in out.splitlines() if line.strip()]

    def merge(self, integration_cwd: Path, branch: str) -> None:
        out = self.run("-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "--no-edit", branch, cwd=integration_cwd, check=False)
        if out.returncode != 0:
            unresolved = self.unresolved_files(integration_cwd)
            if unresolved: raise MergeConflict("merge conflict: " + ", ".join(unresolved))
            raise GitError(f"merge failed: {out.stderr.strip()}")

    def unresolved_files(self, cwd: Path) -> list[str]:
        out = self.run("diff", "--name-only", "--diff-filter=U", cwd=cwd, check=False).stdout
        return [line for line in out.splitlines() if line.strip()]

    def merge_in_progress(self, cwd: Path) -> bool:
        raw = self.run("rev-parse", "--git-path", "MERGE_HEAD", cwd=cwd).stdout.strip()
        path = Path(raw)
        if not path.is_absolute(): path = cwd / path
        return path.exists()

    def finish_merge(self, cwd: Path, cfg: GitConfig) -> None:
        unresolved = self.unresolved_files(cwd)
        if unresolved: raise MergeConflict("unresolved merge files remain: " + ", ".join(unresolved))
        self.run("add", "-A", cwd=cwd)
        if self.merge_in_progress(cwd):
            env = os.environ.copy()
            env.update({"GIT_AUTHOR_NAME": cfg.commit_name, "GIT_AUTHOR_EMAIL": cfg.commit_email, "GIT_COMMITTER_NAME": cfg.commit_name, "GIT_COMMITTER_EMAIL": cfg.commit_email})
            self.run("-c", "core.hooksPath=/dev/null", "commit", "--no-gpg-sign", "--no-edit", cwd=cwd, env=env)
        elif not self.is_clean(cwd): self.commit_all(cwd, "yolo: resolve integration", cfg)

    def abort_merge(self, cwd: Path) -> None:
        if self.merge_in_progress(cwd): self.run("merge", "--abort", cwd=cwd, check=False)

    def reset_hard(self, cwd: Path, commit: str) -> None:
        self.run("reset", "--hard", commit, cwd=cwd)
        self.run("clean", "-fd", cwd=cwd, check=False)

    def can_fast_forward_source(self, base_branch: str, base_commit: str) -> tuple[bool, str]:
        if base_branch == "HEAD": return False, "source checkout was detached"
        if self.branch() != base_branch: return False, f"source worktree is now on {self.branch()}, not {base_branch}"
        if not self.is_clean(): return False, "source worktree is dirty"
        if self.head() != base_commit: return False, "source branch moved since job creation"
        return True, "ok"

    def fast_forward_source(self, integration_branch: str, base_branch: str, base_commit: str) -> None:
        ok, reason = self.can_fast_forward_source(base_branch, base_commit)
        if not ok: raise GitError(f"automatic apply refused: {reason}")
        self.run("-c", "core.hooksPath=/dev/null", "merge", "--ff-only", integration_branch)
