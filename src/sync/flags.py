from __future__ import annotations

import json
import logging
from pathlib import Path

_LOG = logging.getLogger(__name__)

FLAGS_FILE = "browser_flags.json"
FLAGS_KEY = "enabled_labs_experiments"


def _load_local_state(local_state_path: Path) -> dict | None:
    try:
        return json.loads(local_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_local_state(local_state_path: Path, data: dict) -> None:
    tmp = local_state_path.with_suffix(local_state_path.suffix + ".cps-tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(local_state_path)


def get_local_flags(local_state_path: Path) -> list[str]:
    state = _load_local_state(local_state_path)
    if state is None:
        return []
    flags = state.get("browser", {}).get(FLAGS_KEY, [])
    return list(flags) if isinstance(flags, list) else []


def load_sync_flags(sync_root: Path) -> dict[str, dict]:
    f = sync_root / FLAGS_FILE
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_sync_flags(sync_root: Path, data: dict[str, dict]) -> None:
    sync_root.mkdir(parents=True, exist_ok=True)
    (sync_root / FLAGS_FILE).write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
    )


def sync_flags(
    browser_name: str,
    local_state_path: Path,
    sync_root: Path,
    ignore: list[str],
) -> tuple[int, int]:
    if not local_state_path.exists():
        _LOG.debug("%s: Local State missing — skipping flags sync", browser_name)
        return 0, 1

    local_mtime = local_state_path.stat().st_mtime
    sync_data = load_sync_flags(sync_root)
    entry = sync_data.get(browser_name, {})
    sync_mtime = float(entry.get("mtime", 0))
    sync_flags_list = list(entry.get(FLAGS_KEY, []))

    local_flags_list = get_local_flags(local_state_path)
    ignore_set = set(ignore)

    if local_mtime >= sync_mtime:
        merged = list(local_flags_list)
        for f in sync_flags_list:
            if f in ignore_set and f not in merged:
                merged.append(f)
        if merged == sync_flags_list and sync_mtime > 0:
            return 0, 1
        sync_data[browser_name] = {FLAGS_KEY: merged, "mtime": local_mtime}
        save_sync_flags(sync_root, sync_data)
        _LOG.info("%s: pushed %d flag(s) to sync", browser_name, len(merged))
        return 1, 0

    new_local = [f for f in sync_flags_list if f not in ignore_set]
    if new_local == local_flags_list:
        return 0, 1
    state = _load_local_state(local_state_path)
    if state is None:
        _LOG.warning("%s: Local State unreadable — skipping flags pull", browser_name)
        return 0, 1
    state.setdefault("browser", {})[FLAGS_KEY] = new_local
    _save_local_state(local_state_path, state)
    _LOG.info("%s: pulled %d flag(s) from sync", browser_name, len(new_local))
    return 1, 0
