from __future__ import annotations

import json
import os
from pathlib import Path

from src.sync.flags import (
    FLAGS_FILE,
    FLAGS_KEY,
    clear_local_flags,
    get_local_flags,
    load_sync_flags,
    prune_local_flags,
    remove_flags,
    sync_flags,
)


def _write_local_state(path: Path, flags: list[str], mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"browser": {FLAGS_KEY: flags}, "other": "x"}),
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


def _write_sync(sync_root: Path, browser: str, flags: list[str], mtime: float) -> None:
    sync_root.mkdir(parents=True, exist_ok=True)
    (sync_root / FLAGS_FILE).write_text(
        json.dumps({browser: {FLAGS_KEY: flags, "mtime": mtime}}),
        encoding="utf-8",
    )


def test_get_local_flags_empty(tmp_path: Path) -> None:
    p = tmp_path / "Local State"
    p.write_text(json.dumps({"browser": {}}), encoding="utf-8")
    assert get_local_flags(p) == []


def test_get_local_flags_returns_array(tmp_path: Path) -> None:
    p = tmp_path / "Local State"
    p.write_text(
        json.dumps({"browser": {FLAGS_KEY: ["a@1", "b@2"]}}), encoding="utf-8"
    )
    assert get_local_flags(p) == ["a@1", "b@2"]


def test_get_local_flags_missing_file(tmp_path: Path) -> None:
    assert get_local_flags(tmp_path / "missing") == []


def test_sync_flags_push_local_to_empty_sync(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, ["flag-a@1", "flag-b"], 2000.0)
    sync_root = tmp_path / "sync"

    s, sk = sync_flags("Helium", local, sync_root, ignore=[])
    assert (s, sk) == (1, 0)

    data = load_sync_flags(sync_root)
    assert data["Helium"][FLAGS_KEY] == ["flag-a@1", "flag-b"]
    assert data["Helium"]["mtime"] == 2000.0


def test_sync_flags_pull_sync_to_empty_local(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, [], 1000.0)
    sync_root = tmp_path / "sync"
    _write_sync(sync_root, "Helium", ["pulled@1"], 2000.0)

    s, sk = sync_flags("Helium", local, sync_root, ignore=[])
    assert (s, sk) == (1, 0)
    assert get_local_flags(local) == ["pulled@1"]


def test_sync_flags_pull_filters_ignored(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, [], 1000.0)
    sync_root = tmp_path / "sync"
    _write_sync(sync_root, "Helium", ["wanted@1", "ignored@1"], 2000.0)

    s, sk = sync_flags("Helium", local, sync_root, ignore=["ignored@1"])
    assert (s, sk) == (1, 0)
    assert get_local_flags(local) == ["wanted@1"]


def test_sync_flags_push_preserves_ignored_in_sync(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, ["local-only@1"], 3000.0)
    sync_root = tmp_path / "sync"
    _write_sync(sync_root, "Helium", ["ignored@1", "stale@2"], 1000.0)

    s, sk = sync_flags("Helium", local, sync_root, ignore=["ignored@1"])
    assert (s, sk) == (1, 0)

    data = load_sync_flags(sync_root)
    assert "ignored@1" in data["Helium"][FLAGS_KEY]
    assert "local-only@1" in data["Helium"][FLAGS_KEY]
    assert "stale@2" not in data["Helium"][FLAGS_KEY]


def test_sync_flags_noop_when_unchanged(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, ["a@1"], 1000.0)
    sync_root = tmp_path / "sync"
    _write_sync(sync_root, "Helium", ["a@1"], 2000.0)

    s, sk = sync_flags("Helium", local, sync_root, ignore=[])
    assert (s, sk) == (0, 1)


def test_sync_flags_missing_local_state(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    s, sk = sync_flags("Helium", tmp_path / "missing", sync_root, ignore=[])
    assert (s, sk) == (0, 1)


def test_sync_flags_other_local_state_keys_preserved(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, [], 1000.0)
    sync_root = tmp_path / "sync"
    _write_sync(sync_root, "Helium", ["new@1"], 2000.0)

    sync_flags("Helium", local, sync_root, ignore=[])
    data = json.loads(local.read_text(encoding="utf-8"))
    assert data["other"] == "x"
    assert data["browser"][FLAGS_KEY] == ["new@1"]


def test_sync_flags_multi_browser_isolation(tmp_path: Path) -> None:
    local_a = tmp_path / "a" / "Local State"
    local_b = tmp_path / "b" / "Local State"
    _write_local_state(local_a, ["a-flag@1"], 2000.0)
    _write_local_state(local_b, ["b-flag@1"], 2000.0)
    sync_root = tmp_path / "sync"

    sync_flags("BrowserA", local_a, sync_root, ignore=[])
    sync_flags("BrowserB", local_b, sync_root, ignore=[])

    data = load_sync_flags(sync_root)
    assert data["BrowserA"][FLAGS_KEY] == ["a-flag@1"]
    assert data["BrowserB"][FLAGS_KEY] == ["b-flag@1"]


def test_clear_local_flags_wipes_array(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, ["a@1", "b@2"], 1000.0)

    assert clear_local_flags(local) is True
    assert get_local_flags(local) == []
    data = json.loads(local.read_text(encoding="utf-8"))
    assert data["other"] == "x"


def test_clear_local_flags_noop_when_already_empty(tmp_path: Path) -> None:
    local = tmp_path / "Local State"
    _write_local_state(local, [], 1000.0)
    assert clear_local_flags(local) is False


def test_prune_local_flags_clears_absent_browsers(tmp_path: Path) -> None:
    local_a = tmp_path / "a" / "Local State"
    local_b = tmp_path / "b" / "Local State"
    _write_local_state(local_a, ["a@1"], 1000.0)
    _write_local_state(local_b, ["b@1"], 1000.0)

    pruned = prune_local_flags(
        [("A", local_a), ("B", local_b)], keep_browsers={"A"}
    )
    assert pruned == ["B"]
    assert get_local_flags(local_a) == ["a@1"]
    assert get_local_flags(local_b) == []


def test_prune_local_flags_skips_missing_files(tmp_path: Path) -> None:
    pruned = prune_local_flags([("A", tmp_path / "missing")], keep_browsers=set())
    assert pruned == []


def test_remove_flags_strips_from_sync_and_local(tmp_path: Path) -> None:
    local_a = tmp_path / "a" / "Local State"
    local_b = tmp_path / "b" / "Local State"
    _write_local_state(local_a, ["keep@1", "drop@1"], 1000.0)
    _write_local_state(local_b, ["drop@1", "other@2"], 1000.0)
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / FLAGS_FILE).write_text(
        json.dumps({
            "A": {FLAGS_KEY: ["keep@1", "drop@1"], "mtime": 1.0},
            "B": {FLAGS_KEY: ["drop@1"], "mtime": 1.0},
        }),
        encoding="utf-8",
    )

    remove_flags(
        sync_root,
        {"drop@1"},
        [("A", local_a), ("B", local_b)],
    )

    data = load_sync_flags(sync_root)
    assert data["A"][FLAGS_KEY] == ["keep@1"]
    assert data["B"][FLAGS_KEY] == []
    assert get_local_flags(local_a) == ["keep@1"]
    assert get_local_flags(local_b) == ["other@2"]


def test_remove_flags_empty_set_noop(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    _write_sync(sync_root, "A", ["x@1"], 1.0)
    remove_flags(sync_root, set(), [])
    data = load_sync_flags(sync_root)
    assert data["A"][FLAGS_KEY] == ["x@1"]
