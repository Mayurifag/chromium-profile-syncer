from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

_LOG = logging.getLogger(__name__)

_PROGRESS_RE = re.compile(r"Transferred:\s+[\d.]+\s*\w+\s*/\s*[\d.]+\s*\w+,\s*(\d+)%")


def _fallback_paths() -> list[Path]:
    if sys.platform == "darwin":
        return [Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")]
    if sys.platform == "win32":
        return [
            Path("C:/Program Files/rclone/rclone.exe"),
            Path("C:/Program Files (x86)/rclone/rclone.exe"),
            Path.home() / "AppData" / "Local" / "rclone" / "rclone.exe",
        ]
    return [Path("/usr/bin/rclone"), Path("/usr/local/bin/rclone")]


_FALLBACK_PATHS = _fallback_paths()


@lru_cache(maxsize=1)
def find_rclone() -> Path | None:
    which = shutil.which("rclone")
    if which:
        return Path(which)
    for p in _FALLBACK_PATHS:
        if p.exists():
            return p
    return None


def run(cmd: list[str], description: str, report: Callable[[str], None]) -> None:
    report(f"{description} (starting...)" if description else "Starting sync...")
    _LOG.debug("Executing: %s", " ".join(cmd))
    output_lines: list[str] = []
    try:
        from src._winproc import hidden_popen_kwargs
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            **hidden_popen_kwargs(),
        )
        for line in iter(process.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            output_lines.append(line)
            m = _PROGRESS_RE.search(line)
            if m:
                pct = m.group(1)
                report(f"{description} ({pct}%)" if description else f"Syncing ({pct}%)")
            elif "Transferred:" in line:
                report(f"{description}..." if description else "Syncing...")
        process.wait()
        if process.returncode != 0:
            error_output = "\n".join(output_lines) if output_lines else "No output"
            _LOG.error("rclone failed:\n%s", error_output)
            raise subprocess.CalledProcessError(process.returncode, cmd, output=error_output)
        _LOG.debug("rclone complete: %s", description or cmd[1])
    except subprocess.CalledProcessError as exc:
        raise OSError(
            f"rclone sync failed: {exc.output}" if exc.output else f"rclone sync failed: {exc}"
        ) from exc
    except FileNotFoundError as exc:
        raise OSError(f"rclone not found: {exc}") from exc
