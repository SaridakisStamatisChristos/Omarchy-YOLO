from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .git import GitRepo
from .util import YoloError

MAX_CHANGED_FILE_LIST_BYTES = 2_000_000
MAX_SINGLE_FILE_DIFF_BYTES = 4_000_000
MAX_TOTAL_REVIEW_DIFF_BYTES = 32_000_000


@dataclass(slots=True, frozen=True)
class ReviewChunk:
    index: int
    files: tuple[str, ...]
    text: str


def _git_bytes(repo: GitRepo, cwd: Path, args: list[str], max_bytes: int) -> bytes:
    repo.assert_worktree(cwd)
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
    data = cast(bytes, proc.stdout.read(max_bytes + 1))
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
    if truncated:
        raise YoloError(
            f"candidate review data exceeds the safety ceiling ({max_bytes} bytes); "
            "split the change into smaller tasks"
        )
    if proc.returncode != 0:
        raise YoloError(
            f"Git review query failed with exit {proc.returncode}: {' '.join(args[:4])}"
        )
    return data


def _git_text(repo: GitRepo, cwd: Path, args: list[str], max_bytes: int) -> str:
    return _git_bytes(repo, cwd, args, max_bytes).decode("utf-8", errors="replace")


def _display_path(path: str) -> str:
    """Make an arbitrary POSIX filename safe for UTF-8 prompts without losing identity."""
    return path.encode("utf-8", errors="backslashreplace").decode("utf-8")


def changed_files(repo: GitRepo, cwd: Path, base: str, *, max_files: int) -> list[str]:
    raw = _git_bytes(
        repo,
        cwd,
        ["diff", "--name-only", "-z", f"{base}..HEAD"],
        MAX_CHANGED_FILE_LIST_BYTES,
    )
    # os.fsdecode uses the platform's surrogateescape strategy on Linux, allowing the
    # resulting str to be passed back to subprocess and recover the original path bytes.
    files = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    if len(files) > max_files:
        raise YoloError(
            f"candidate changes {len(files)} files; configured final-review maximum is {max_files}"
        )
    return files


def _file_diff(repo: GitRepo, cwd: Path, base: str, path: str) -> str:
    return _git_text(
        repo,
        cwd,
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--find-copies",
            f"{base}..HEAD",
            "--",
            path,
        ],
        MAX_SINGLE_FILE_DIFF_BYTES,
    )


def _split_text(text: str, max_bytes: int) -> list[str]:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    pieces: list[str] = []
    current: list[str] = []
    current_bytes = 0

    def flush() -> None:
        nonlocal current, current_bytes
        if current:
            pieces.append("".join(current))
            current = []
            current_bytes = 0

    for line in text.splitlines(keepends=True):
        encoded = line.encode("utf-8")
        if len(encoded) <= max_bytes:
            if current and current_bytes + len(encoded) > max_bytes:
                flush()
            current.append(line)
            current_bytes += len(encoded)
            continue

        flush()
        fragment: list[str] = []
        fragment_bytes = 0
        for char in line:
            size = len(char.encode("utf-8"))
            if fragment and fragment_bytes + size > max_bytes:
                pieces.append("".join(fragment))
                fragment = []
                fragment_bytes = 0
            fragment.append(char)
            fragment_bytes += size
        if fragment:
            pieces.append("".join(fragment))
    flush()
    return pieces or [""]


def build_review_chunks(
    repo: GitRepo,
    cwd: Path,
    base: str,
    *,
    max_files: int,
    chunk_bytes: int,
    chunk_files: int,
) -> tuple[list[str], list[ReviewChunk]]:
    actual_files = changed_files(repo, cwd, base, max_files=max_files)
    if not actual_files:
        return [], [ReviewChunk(index=1, files=(), text="No changed files.")]

    manifest = [_display_path(path) for path in actual_files]
    chunks: list[ReviewChunk] = []
    current_parts: list[str] = []
    current_files: list[str] = []
    current_bytes = 0
    total_bytes = 0

    def flush() -> None:
        nonlocal current_parts, current_files, current_bytes
        if not current_parts:
            return
        chunks.append(
            ReviewChunk(
                index=len(chunks) + 1,
                files=tuple(dict.fromkeys(current_files)),
                text="".join(current_parts),
            )
        )
        current_parts = []
        current_files = []
        current_bytes = 0

    for path in actual_files:
        display_path = _display_path(path)
        diff = _file_diff(repo, cwd, base, path)
        diff_bytes = len(diff.encode("utf-8"))
        total_bytes += diff_bytes
        if total_bytes > MAX_TOTAL_REVIEW_DIFF_BYTES:
            raise YoloError(
                "candidate diff exceeds the 32 MB hierarchical-review safety ceiling; "
                "split the release into smaller changes"
            )
        header_probe = f"\n===== FILE {display_path} PART 999999/999999 =====\n"
        payload_budget = chunk_bytes - len(header_probe.encode("utf-8"))
        if payload_budget < 1024:
            raise YoloError(f"review chunk budget is too small for file path {display_path!r}")
        pieces = _split_text(
            diff or f"[No textual diff for {display_path}]\n",
            payload_budget,
        )
        for part_number, piece in enumerate(pieces, start=1):
            header = (
                f"\n===== FILE {display_path} PART {part_number}/{len(pieces)} =====\n"
            )
            payload = header + piece
            payload_bytes = len(payload.encode("utf-8"))
            if (
                current_parts
                and (
                    current_bytes + payload_bytes > chunk_bytes
                    or len(set(current_files + [display_path])) > chunk_files
                )
            ):
                flush()
            if payload_bytes > chunk_bytes:
                raise YoloError(
                    f"review chunk construction exceeded byte limit for {display_path}"
                )
            current_parts.append(payload)
            current_files.append(display_path)
            current_bytes += payload_bytes
    flush()
    return manifest, chunks
