from __future__ import annotations

import shutil
import subprocess


def notify(title: str, body: str) -> None:
    executable = shutil.which("notify-send")
    if not executable:
        return
    try:
        subprocess.run(
            [executable, title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        return


def toggle_ui() -> bool:
    executable = shutil.which("omarchy-shell")
    if not executable:
        return False
    try:
        proc = subprocess.run(
            [executable, "dev.aether.yolo", "toggle"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0
