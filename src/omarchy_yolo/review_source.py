from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


def _git_text(repo: GitRepo, cwd: Path, args: list[str], max_bytes: int) -> str:
    repo.assert_worktree(cwd)
    text, truncated = repo._run_stdout_bounded(args, cwd=cwd, max_bytes=max_bytes)
    if truncated:
        raise YoloError(
            f"candidate review data exceeds the safety ceiling ({max_bytes} bytes); "
            "split the change into smaller tasks"
        )
    return text


def changed_files(repo: GitRepo, cwd: Path, base: str, *, max_files: int) -> list[str]:
    raw = _git_text(
        repo,
        cwd,
        ["diff", "--name-only", "-z", f"{base}..HEAD"],
        MAX_CHANGED_FILE_LIST_BYTES,
    )
    files = [item for item in raw.split("\0") if item]
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
    files = changed_files(repo, cwd, base, max_files=max_files)
    if not files:
        return [], [ReviewChunk(index=1, files=(), text="No changed files.")]

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

    for path in files:
        diff = _file_diff(repo, cwd, base, path)
        diff_bytes = len(diff.encode("utf-8"))
        total_bytes += diff_bytes
        if total_bytes > MAX_TOTAL_REVIEW_DIFF_BYTES:
            raise YoloError(
                "candidate diff exceeds the 32 MB hierarchical-review safety ceiling; "
                "split the release into smaller changes"
            )
        pieces = _split_text(diff or f"[No textual diff for {path}]\n", chunk_bytes)
        for part_number, piece in enumerate(pieces, start=1):
            header = f"\n===== FILE {path} PART {part_number}/{len(pieces)} =====\n"
            payload = header + piece
            payload_bytes = len(payload.encode("utf-8"))
            if (
                current_parts
                and (
                    current_bytes + payload_bytes > chunk_bytes
                    or len(set(current_files + [path])) > chunk_files
                )
            ):
                flush()
            if payload_bytes > chunk_bytes:
                raise YoloError(f"review chunk construction exceeded byte limit for {path}")
            current_parts.append(payload)
            current_files.append(path)
            current_bytes += payload_bytes
    flush()
    return files, chunks
