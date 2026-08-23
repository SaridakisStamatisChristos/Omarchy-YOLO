from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .git import GitRepo
from .util import YoloError

MAX_CHANGED_FILE_LIST_BYTES = 2_000_000
MAX_SINGLE_FILE_DIFF_BYTES = 4_000_000
MAX_TOTAL_REVIEW_DIFF_BYTES = 32_000_000
MAX_BINARY_METADATA_BYTES = 32_000


@dataclass(slots=True, frozen=True)
class ReviewChunk:
    index: int
    files: tuple[str, ...]
    text: str


def _git_bytes(repo: GitRepo, cwd: Path, args: list[str], max_bytes: int) -> bytes:
    repo.assert_worktree(cwd)
    out = repo.run_bytes(
        *args,
        cwd=cwd,
        check=False,
        max_stdout_bytes=max_bytes,
        max_stderr_bytes=64_000,
    )
    if out.timed_out:
        raise YoloError(
            f"Git review query timed out after {repo.command_timeout_seconds} seconds"
        )
    if out.output_truncated:
        raise YoloError(
            f"candidate review data exceeds the safety ceiling ({max_bytes} bytes); "
            "split the change into smaller tasks"
        )
    if out.returncode != 0:
        raise YoloError(
            f"Git review query failed with exit {out.returncode}: {' '.join(args[:4])}"
        )
    return out.stdout


def _git_text(repo: GitRepo, cwd: Path, args: list[str], max_bytes: int) -> str:
    return _git_bytes(repo, cwd, args, max_bytes).decode("utf-8", errors="replace")


_PLAIN_PROMPT_PATH_RE = re.compile(
    r"^[A-Za-z0-9._/@+\-](?:[A-Za-z0-9._/@+ \-]*[A-Za-z0-9._/@+\-])?$"
)
_ENCODED_PATH_PREFIX = "json:"


def _display_path(path: str) -> str:
    """Return an injective, single-line representation safe for model prompts.

    Ordinary project paths remain readable and backward compatible. Exotic paths
    use an explicitly tagged JSON string; reserving the tag for encoded values
    makes the representation collision-free.
    """
    if _PLAIN_PROMPT_PATH_RE.fullmatch(path) and not path.startswith(_ENCODED_PATH_PREFIX):
        return path
    return _ENCODED_PATH_PREFIX + json.dumps(path, ensure_ascii=True)


def changed_files(repo: GitRepo, cwd: Path, base: str, *, max_files: int) -> list[str]:
    raw = _git_bytes(
        repo,
        cwd,
        ["diff", "--name-only", "-z", f"{base}..HEAD"],
        MAX_CHANGED_FILE_LIST_BYTES,
    )
    files = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    if len(files) > max_files:
        raise YoloError(
            f"candidate changes {len(files)} files; configured final-review maximum is {max_files}"
        )
    return files


def _is_binary_change(repo: GitRepo, cwd: Path, base: str, path: str) -> bool:
    raw = _git_bytes(
        repo,
        cwd,
        ["diff", "--numstat", "-z", f"{base}..HEAD", "--", path],
        MAX_BINARY_METADATA_BYTES,
    )
    first_record = raw.split(b"\0", 1)[0]
    fields = first_record.split(b"\t", 2)
    return len(fields) >= 2 and fields[0] == b"-" and fields[1] == b"-"


def _binary_metadata(repo: GitRepo, cwd: Path, base: str, path: str) -> str:
    raw = _git_text(
        repo,
        cwd,
        ["diff", "--raw", "--full-index", f"{base}..HEAD", "--", path],
        MAX_BINARY_METADATA_BYTES,
    )
    summary = _git_text(
        repo,
        cwd,
        ["diff", "--summary", f"{base}..HEAD", "--", path],
        MAX_BINARY_METADATA_BYTES,
    )
    return (
        "[Binary content review explicitly allowed; content is not interpreted. "
        "Review object hashes/modes/provenance metadata only.]\n"
        f"RAW:\n{raw}\nSUMMARY:\n{summary}"
    )


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
    allow_binary: bool = False,
) -> tuple[list[str], list[ReviewChunk]]:
    if chunk_files < 1:
        raise ValueError("chunk_files must be positive")
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
        unique_files = tuple(dict.fromkeys(current_files))
        if len(unique_files) > 1:
            raise YoloError("internal review invariant violated: a shard spans multiple files")
        chunks.append(
            ReviewChunk(
                index=len(chunks) + 1,
                files=unique_files,
                text="".join(current_parts),
            )
        )
        current_parts = []
        current_files = []
        current_bytes = 0

    for path in actual_files:
        # Keep every raw shard file-local. This lets the next hierarchy stage synthesize
        # all pieces of one file before global cross-file reasoning.
        flush()
        display_path = _display_path(path)
        binary = _is_binary_change(repo, cwd, base, path)
        if binary and not allow_binary:
            raise YoloError(
                f"binary change cannot be semantically reviewed: {display_path}; "
                "set engine.final_review_allow_binary=true only when metadata-only review is acceptable"
            )
        diff = _binary_metadata(repo, cwd, base, path) if binary else _file_diff(repo, cwd, base, path)
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
            if current_parts and current_bytes + payload_bytes > chunk_bytes:
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
