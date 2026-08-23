from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import stat
import time
import uuid
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO, ParamSpec, TypeVar


class YoloError(RuntimeError):
    """Base operational error surfaced to the CLI/RPC boundary."""


MAX_STRUCTURED_OUTPUT_CHARS = 262_144
MAX_JSON_DECODE_ATTEMPTS = 512
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{12}$")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_P = ParamSpec("_P")
_R = TypeVar("_R")


def current_uid() -> int:
    return os.geteuid()


def utc_ts() -> float:
    return time.time()


async def atomic_to_thread(
    function: Callable[_P, _R],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _R:
    """Finish a blocking side effect before allowing cancellation to escape.

    ``asyncio.to_thread`` cannot stop its worker thread. Awaiting it directly under a
    repository lock lets cancellation release that lock while Git is still mutating
    shared state. This helper defers propagation of any cancellation until the thread
    has terminated; the blocking operation itself must still enforce a finite timeout.
    """

    return await finish_before_cancel(asyncio.to_thread(function, *args, **kwargs))


async def finish_before_cancel(awaitable: Awaitable[_R]) -> _R:
    """Let a bounded critical awaitable settle before propagating cancellation."""
    inner = asyncio.ensure_future(awaitable)
    cancelled = False
    while not inner.done():
        try:
            await asyncio.shield(inner)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        result = inner.result()
    except BaseException as exc:
        if cancelled:
            raise asyncio.CancelledError from exc
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_runtime_dir() -> Path:
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if raw:
        return Path(raw)
    return Path(f"/tmp/omarchy-yolo-{os.getuid()}")


def ensure_private_dir(path: Path) -> Path:
    """Create/validate a private directory without following a final-component symlink."""
    path = path.expanduser()
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        info = path.lstat()

    if stat.S_ISLNK(info.st_mode):
        raise YoloError(f"refusing symlink for private directory: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise YoloError(f"private path is not a directory: {path}")
    if info.st_uid != current_uid():
        raise YoloError(
            f"private directory is owned by uid {info.st_uid}, expected uid {current_uid()}: {path}"
        )
    os.chmod(path, 0o700, follow_symlinks=False)
    return path


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def shell_join(argv: list[str]) -> str:
    return shlex.join(argv)


def slug(value: str, *, max_len: int = 48) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._") or "task"
    return value[:max_len].rstrip("-._") or "task"


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the last valid JSON object from bounded mixed CLI output.

    Scanning from the end avoids retaining every candidate and using ``raw_decode`` with an
    index avoids repeatedly allocating large suffix strings. Only the tail is considered because
    supported agent CLIs emit their final answer last.
    """
    window = text[-MAX_STRUCTURED_OUTPUT_CHARS:]
    decoder = json.JSONDecoder()
    attempts = 0
    for index in range(len(window) - 1, -1, -1):
        if window[index] != "{":
            continue
        attempts += 1
        if attempts > MAX_JSON_DECODE_ATTEMPTS:
            break
        try:
            value, _ = decoder.raw_decode(window, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise YoloError("agent output did not contain a valid JSON object")


def redact_env_key(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH"))


def terminal_safe(value: object, *, single_line: bool = False, max_chars: int = 20_000) -> str:
    """Strip terminal control sequences from untrusted status/error text."""
    text = str(value)
    text = _ANSI_RE.sub("", text)
    cleaned: list[str] = []
    for char in text:
        code = ord(char)
        if char == "\n" and not single_line:
            cleaned.append(char)
        elif char == "\t" and not single_line:
            cleaned.append(char)
        elif code >= 0x20 and code != 0x7F:
            cleaned.append(char)
        elif single_line and char in {"\n", "\r", "\t"}:
            cleaned.append(" ")
    result = "".join(cleaned)
    if single_line:
        result = " ".join(result.splitlines())
    return result[:max_chars]


def validate_job_id(value: str) -> str:
    if not _JOB_ID_RE.fullmatch(value):
        raise YoloError("invalid job id")
    return value


def truncate_utf8(value: str, max_bytes: int, *, marker: str = "\n...[truncated]...\n") -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker_b = marker.encode("utf-8")
    budget = max(0, max_bytes - len(marker_b))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


def open_private_binary(path: Path, *, append: bool = False) -> BinaryIO:
    """Open a private log/state artifact without following the final path component."""
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if append else os.O_TRUNC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise YoloError(f"cannot safely open private file: {path}: {exc}") from exc
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab" if append else "wb")


def read_text_bounded(path: Path, *, max_bytes: int, errors: str = "strict") -> str:
    """Read at most ``max_bytes`` from a regular non-symlink file."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(info.st_mode):
        raise YoloError(f"refusing non-regular inspection file: {path}")
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise YoloError(f"file exceeds {max_bytes} byte inspection limit: {path}")
    return data.decode("utf-8", errors=errors)


def read_tail_bounded(path: Path, *, max_bytes: int, errors: str = "replace") -> str:
    """Read a bounded file tail without following a final-component symlink."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise YoloError(f"cannot safely read file: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise YoloError(f"refusing non-regular file: {path}")
        os.lseek(fd, max(0, info.st_size - max_bytes), os.SEEK_SET)
        data = os.read(fd, max_bytes)
    finally:
        os.close(fd)
    return data.decode("utf-8", errors=errors)
