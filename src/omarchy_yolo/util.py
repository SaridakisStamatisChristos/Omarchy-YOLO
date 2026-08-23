from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Any


class YoloError(RuntimeError):
    """Base operational error surfaced to the CLI/RPC boundary."""


def utc_ts() -> float:
    return time.time()


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
    # The fallback is intentionally user-specific and private by convention.
    return Path(f"/tmp/omarchy-yolo-{os.getuid()}")


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except PermissionError:
        pass
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
    """Extract the last valid JSON object from mixed CLI output.

    Agent CLIs may prepend status lines or wrap a JSON answer in markdown. We scan every
    opening brace with JSONDecoder.raw_decode and return the last object that parses.
    """

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        raise YoloError("agent output did not contain a valid JSON object")
    return candidates[-1]


def redact_env_key(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH"))
