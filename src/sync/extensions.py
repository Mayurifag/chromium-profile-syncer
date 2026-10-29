from __future__ import annotations

import json
import logging
from pathlib import Path

from src.operations import operation_lock
from src.sync import write_text_if_changed
from src.sync.sync_dir import edit_snapshot

_LOG = logging.getLogger(__name__)
LIBRARY_FILE = "extension_library.json"
MANIFEST_FILE = "webstore_extensions.json"


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _parse_version(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _dir_version(directory: Path) -> tuple[int, ...]:
    try:
        manifest = _read_object(directory / "manifest.json")
        return _parse_version(manifest.get("version", directory.name.split("_")[0]))
    except (OSError, ValueError):
        return _parse_version(directory.name.split("_")[0])


def _best_version_dir(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    return max((p for p in directory.iterdir() if p.is_dir()), key=_dir_version, default=None)


def _is_webstore_extension(directory: Path) -> bool:
    if (directory / "_metadata" / "verified_contents.json").exists():
        return True
    try:
        return bool(_read_object(directory / "manifest.json").get("update_url"))
    except (OSError, ValueError):
        return False


def _extension_name(directory: Path) -> str:
    try:
        manifest = _read_object(directory / "manifest.json")
        name = manifest.get("name", "")
        if not isinstance(name, str):
            return directory.parent.name
        if not name.startswith("__MSG_"):
            return name or directory.parent.name
        key = name.removeprefix("__MSG_").removesuffix("__").casefold()
        locales = directory / "_locales"
        preferred = [locales / manifest.get("default_locale", "en"), locales / "en"]
        for locale in dict.fromkeys([*preferred, *sorted(locales.glob("*"))]):
            messages = _read_object(locale / "messages.json")
            for message_key, message in messages.items():
                if message_key.casefold() == key and isinstance(message, dict):
                    return message.get("message", "") or directory.parent.name
    except (OSError, ValueError):
        pass
    return directory.parent.name


def read_manifest(sync_dir: Path) -> dict[str, str]:
    path = sync_dir / MANIFEST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if isinstance(data, list) and all(isinstance(value, str) for value in data):
        return dict.fromkeys(data, "")
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError(f"Invalid extension manifest: {path}")
    return data


def read_library(sync_dir: Path) -> dict[str, dict]:
    library = _read_object(sync_dir / LIBRARY_FILE)
    for ext_id, entry in library.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("archived"), bool):
            raise ValueError(f"Invalid saved extension: {ext_id}")
        if not isinstance(entry.get("name", ""), str):
            raise ValueError(f"Invalid extension name: {ext_id}")
    for ext_id, name in read_manifest(sync_dir).items():
        library.setdefault(ext_id, {"name": name, "archived": False})
    return library


def _save_library(sync_dir: Path, library: dict[str, dict]) -> bool:
    sync_dir.mkdir(parents=True, exist_ok=True)
    changed = write_text_if_changed(sync_dir / LIBRARY_FILE, json.dumps(library, sort_keys=True))
    active = {
        ext_id: entry.get("name", "") for ext_id, entry in library.items() if not entry["archived"]
    }
    return (
        write_text_if_changed(sync_dir / MANIFEST_FILE, json.dumps(active, sort_keys=True))
        or changed
    )


def set_archived(sync_dir: Path, ext_ids: set[str], archived: bool) -> None:
    update_archive_states(sync_dir, dict.fromkeys(ext_ids, archived))


def update_archive_states(sync_dir: Path, states: dict[str, bool]) -> None:
    with operation_lock(), edit_snapshot(sync_dir) as work:
        library = read_library(work)
        for ext_id, archived in states.items():
            library.setdefault(ext_id, {"name": ""})["archived"] = archived
        _save_library(work, library)


def archived_ids(sync_dir: Path, aliases: dict[str, str] | None = None) -> set[str]:
    return _with_alias_ids(
        {ext_id for ext_id, entry in read_library(sync_dir).items() if entry["archived"]},
        aliases,
    )


def collect_webstore_extensions(profile_dir: Path) -> dict[str, str]:
    result = {}
    for ext_id in _extension_dir_ids(profile_dir / "Extensions"):
        best = _best_version_dir(profile_dir / "Extensions" / ext_id)
        if best and _is_webstore_extension(best):
            result[ext_id] = _extension_name(best)
    return result


def installed_extension_ids(profile_dir: Path) -> set[str]:
    # Secure Preferences is authoritative on browsers that protect extension state.
    # A disabled extension is still installed; state 2 records an external uninstall.
    for filename in ("Secure Preferences", "Preferences"):
        prefs = _read_object(profile_dir / filename)
        settings = prefs.get("extensions", {}).get("settings")
        if settings is not None:
            if not isinstance(settings, dict):
                raise ValueError(f"Invalid extension settings in {profile_dir / filename}")
            return {
                ext_id
                for ext_id, entry in settings.items()
                if isinstance(entry, dict) and entry.get("state") != 2
            }
    return _extension_dir_ids(profile_dir / "Extensions")


def update_webstore_manifest(
    profile_dir: Path,
    sync_dir: Path,
    aliases: dict[str, str] | None = None,
    *,
    ignore_removals: set[str] | None = None,
) -> None:
    from src import config

    aliases = aliases or {}
    installed = installed_extension_ids(profile_dir)
    canonical = {aliases.get(ext_id, ext_id) for ext_id in installed}
    observed = config.get_extension_observation(profile_dir)
    library = read_library(sync_dir)
    if observed is not None:
        for ext_id in set(observed) - canonical - (ignore_removals or set()):
            if ext_id in library:
                library[ext_id]["archived"] = True
                _LOG.info("Extension removed in browser; saved for restore: %s", ext_id)

    discovered = collect_webstore_extensions(profile_dir)
    for alias, ext_id in aliases.items():
        if alias in _profile_extension_data_ids(profile_dir):
            discovered.setdefault(alias, library.get(ext_id, {}).get("name", ""))
    for ext_id, name in discovered.items():
        canonical_id = aliases.get(ext_id, ext_id)
        entry = library.setdefault(canonical_id, {"name": name, "archived": False})
        if name and name != ext_id:
            entry["name"] = name
    _save_library(sync_dir, library)


def record_observation(profile_dir: Path, aliases: dict[str, str] | None = None) -> None:
    from src import config

    aliases = aliases or {}
    ids = {aliases.get(ext_id, ext_id) for ext_id in installed_extension_ids(profile_dir)}
    config.set_extension_observation(profile_dir, sorted(ids))


def _extension_dir_ids(base: Path) -> set[str]:
    return {entry.name for entry in base.iterdir() if entry.is_dir()} if base.is_dir() else set()


def _with_alias_ids(ext_ids: set[str], aliases: dict[str, str] | None) -> set[str]:
    expanded = set(ext_ids)
    for alias, canonical in (aliases or {}).items():
        if alias in ext_ids or canonical in ext_ids:
            expanded.update((alias, canonical))
    return expanded


def extension_install_ids(
    sync_dir: Path,
    profile_dir: Path | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    ids = {ext_id for ext_id, entry in read_library(sync_dir).items() if not entry["archived"]}
    ids.update(_extension_dir_ids(sync_dir / "Extensions"))
    if profile_dir is not None:
        ids.update(_extension_dir_ids(profile_dir / "Extensions"))
    return _with_alias_ids(ids, aliases) - archived_ids(sync_dir, aliases)


def indexeddb_extension_id(name: str) -> str:
    return (
        name.removeprefix("chrome-extension_").split("_")[0]
        if name.startswith("chrome-extension_")
        else ""
    )


def _profile_extension_data_ids(profile_dir: Path) -> set[str]:
    ids: set[str] = set()
    for subdir in (
        "Local Extension Settings",
        "Sync Extension Settings",
        "Managed Extension Settings",
    ):
        ids.update(_extension_dir_ids(profile_dir / subdir))
    indexeddb = profile_dir / "IndexedDB"
    if indexeddb.is_dir():
        ids.update(
            filter(None, (indexeddb_extension_id(entry.name) for entry in indexeddb.iterdir()))
        )
    return ids


def preserve_orphan_extension_data(
    sync_dir: Path, keep_ext_ids: set[str], aliases: dict[str, str] | None = None
) -> set[str]:
    library = read_library(sync_dir)
    known = _with_alias_ids(set(library), aliases)
    orphan_ids = _profile_extension_data_ids(sync_dir) - keep_ext_ids - known
    for ext_id in orphan_ids:
        library[ext_id] = {"name": "", "archived": True}
    if orphan_ids:
        _save_library(sync_dir, library)
    return orphan_ids
