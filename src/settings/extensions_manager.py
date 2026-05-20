from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import escape, unescape
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src import config as _config
from src.browsers import ALL_BROWSERS, BrowserBase, get_browser

_LOG = logging.getLogger(__name__)
_NAME_CACHE_FILE = "extension_names.json"
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class ExtensionInfo:
    name: str = ""
    sources: set[str] = field(default_factory=set)


def _webstore_url(ext_id: str) -> str:
    return f"https://chromewebstore.google.com/detail/{ext_id}"


def extension_backup_count(sync_folder: Path) -> int:
    from src.sync.extensions import extension_install_ids
    from src.sync.sync_dir import SYNC_DIR_NAME

    work_dir = sync_folder / SYNC_DIR_NAME
    if not work_dir.is_dir():
        work_dir = sync_folder
    if not work_dir.is_dir():
        return 0
    return len(extension_install_ids(work_dir))


def _canonical_ext_id(ext_id: str) -> str:
    for browser in ALL_BROWSERS:
        if ext_id in browser.ext_id_aliases:
            return browser.ext_id_aliases[ext_id]
    return ext_id


def _all_ext_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for browser in ALL_BROWSERS:
        aliases.update(browser.ext_id_aliases)
    return aliases


def _all_id_candidates(ext_id: str) -> set[str]:
    candidates = {ext_id}
    for browser in ALL_BROWSERS:
        for alias_id, canonical_id in browser.ext_id_aliases.items():
            if canonical_id == ext_id:
                candidates.add(alias_id)
            elif alias_id == ext_id:
                candidates.add(canonical_id)
    return candidates


def _resolve_target_browser() -> BrowserBase | None:
    name = _config.get_last_restored_browser()
    if name:
        b = get_browser(name)
        if b and b.launch_command():
            return b
    for b in ALL_BROWSERS:
        if b.is_installed() and b.launch_command():
            return b
    return None


def _open_urls(urls: list[str]) -> None:
    if not urls:
        return
    browser = _resolve_target_browser()
    if browser is None:
        for u in urls:
            QDesktopServices.openUrl(QUrl(u))
        return
    cmd = browser.launch_command()
    assert cmd
    try:
        subprocess.Popen([*cmd, *urls], close_fds=True)
    except OSError as exc:
        _LOG.warning(
            "Failed to launch %s (%s) — falling back to default browser", browser.name, exc
        )
        for u in urls:
            QDesktopServices.openUrl(QUrl(u))


def _dir_size(d: Path) -> int:
    total = 0
    for f in d.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _fmt_size(size: int) -> str:
    if size == 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024**2:.1f} MB"


def _settings_size(work_dir: Path, ext_id: str) -> int:
    dirs: list[Path] = []
    candidates = _all_id_candidates(ext_id)
    for candidate in candidates:
        dirs += [
            work_dir / "Local Extension Settings" / candidate,
            work_dir / "Sync Extension Settings" / candidate,
            work_dir / "Managed Extension Settings" / candidate,
        ]
    idb = work_dir / "IndexedDB"
    if idb.exists():
        prefixes = tuple(f"chrome-extension_{candidate}_" for candidate in candidates)
        dirs += [d for d in idb.iterdir() if d.name.startswith(prefixes)]
    return sum(_dir_size(d) for d in dirs if d.exists())


def _info(ext_map: dict[str, ExtensionInfo], ext_id: str) -> ExtensionInfo:
    return ext_map.setdefault(ext_id, ExtensionInfo())


def _display_name(info: ExtensionInfo) -> str:
    if info.name:
        return info.name
    sources = info.sources
    if "Extension files" in sources:
        return "Extension files"
    if "Web Store" in sources:
        return "Web Store extension"
    if any(s.endswith(" internal") for s in sources):
        return "Internal extension"
    if "Settings" in sources:
        return "Extension data without install record"
    if "Pinned" in sources:
        return "Pinned extension"
    return "Extension"


def _source_text(info: ExtensionInfo) -> str:
    sources = info.sources
    labels: list[str] = []
    if "Web Store" in sources:
        labels.append("Web Store")
    labels += sorted(s for s in sources if s.endswith(" internal"))
    if "Extension files" in sources:
        labels.append("Extension files")
    if "Settings" in sources:
        labels.append("Saved extension settings" if labels else "No install record")
    if "Pinned" in sources:
        labels.append("Toolbar pin" if labels else "Toolbar pin only")
    return ", ".join(labels) or "No source metadata"


def _source_badge(info: ExtensionInfo) -> tuple[str, str]:
    sources = info.sources
    internal = sorted(s.removesuffix(" internal") for s in sources if s.endswith(" internal"))
    if "Helium" in internal:
        return "He", "#bd93f9"
    if internal:
        return internal[0][:2].title(), "#bd93f9"
    if "Web Store" in sources:
        return "WS", "#8be9fd"
    if "Pinned" in sources and len(sources) == 1:
        return "P", "#ffb86c"
    if sources:
        return "S", "#6272a4"
    return "?", "#44475a"


def _source_icon(info: ExtensionInfo) -> QIcon:
    label, color = _source_badge(info)
    pix = QPixmap(28, 18)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 1, 28, 16, 5, 5)
    painter.setPen(QColor("#282a36"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(10)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, label)
    painter.end()

    return QIcon(pix)


def _id_candidates(browser: BrowserBase, ext_id: str) -> list[str]:
    candidates = [ext_id]
    for alias_id, canonical_id in browser.ext_id_aliases.items():
        if canonical_id == ext_id:
            candidates.append(alias_id)
        elif alias_id == ext_id:
            candidates.append(canonical_id)
    return candidates


def _secure_pref_info(
    browser: BrowserBase, profile: Path, ext_ids: set[str]
) -> dict[str, ExtensionInfo]:
    prefs_path = profile / "Secure Preferences"
    if not prefs_path.exists():
        return {}
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    settings = prefs.get("extensions", {}).get("settings", {})
    if not isinstance(settings, dict):
        return {}
    result: dict[str, ExtensionInfo] = {}
    for ext_id in ext_ids:
        for candidate in _id_candidates(browser, ext_id):
            if candidate not in settings:
                continue
            entry = settings[candidate]
            if not isinstance(entry, dict):
                continue
            info = result.setdefault(ext_id, ExtensionInfo())
            if candidate != ext_id:
                info.sources.add(f"{browser.name} internal")
            manifest = entry.get("manifest", {}) if isinstance(entry, dict) else {}
            name = manifest.get("name", "") if isinstance(manifest, dict) else ""
            if name and not name.startswith("__MSG_"):
                info.name = name
            update_url = manifest.get("update_url", "") if isinstance(manifest, dict) else ""
            if entry.get("from_webstore") or update_url:
                info.sources.add("Web Store")
            break
    return result


def _installed_extension_info(ext_ids: set[str]) -> dict[str, ExtensionInfo]:
    from src.sync.extensions import _best_version_dir, _extension_name, _is_webstore_extension

    result: dict[str, ExtensionInfo] = {}
    for browser in ALL_BROWSERS:
        if not browser.is_installed():
            continue
        for profile in browser.discover_profiles():
            for ext_id, info in _secure_pref_info(browser, profile, ext_ids).items():
                current = result.setdefault(ext_id, ExtensionInfo())
                if info.name and not current.name:
                    current.name = info.name
                current.sources.update(info.sources)
            ext_dir = profile / "Extensions"
            if not ext_dir.exists():
                continue
            for ext_id in ext_ids:
                current = result.setdefault(ext_id, ExtensionInfo())
                for candidate in _id_candidates(browser, ext_id):
                    best = _best_version_dir(ext_dir / candidate)
                    if best is None:
                        continue
                    name = _extension_name(best)
                    if name and name != candidate and not current.name:
                        current.name = name
                    if candidate != ext_id:
                        current.sources.add(f"{browser.name} internal")
                    elif _is_webstore_extension(best):
                        current.sources.add("Web Store")
                    break
    return result


def _read_name_cache(work_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((work_dir / _NAME_CACHE_FILE).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def _write_name_cache(work_dir: Path, names: dict[str, str]) -> None:
    try:
        (work_dir / _NAME_CACHE_FILE).write_text(
            json.dumps(names, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


def _webstore_name_from_html(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    title = " ".join(unescape(match.group(1)).split())
    title = title.removesuffix(" - Chrome Web Store")
    return "" if title == "Chrome Web Store" else title


def _webstore_extension_name(ext_id: str) -> str:
    request = Request(
        _webstore_url(ext_id),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            html = response.read(1_500_000).decode("utf-8", "ignore")
    except (OSError, ValueError, URLError):
        return ""
    return _webstore_name_from_html(html)


def _webstore_extension_names(ext_ids: set[str]) -> dict[str, str]:
    if not ext_ids:
        return {}
    names: dict[str, str] = {}
    workers = min(8, len(ext_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_webstore_extension_name, ext_id): ext_id for ext_id in ext_ids}
        for future in as_completed(futures):
            try:
                name = future.result()
            except Exception:
                continue
            if name:
                names[futures[future]] = name
    return names


def _resolve_missing_names(work_dir: Path, ext_map: dict[str, ExtensionInfo]) -> None:
    cache = _read_name_cache(work_dir)
    changed = False
    for ext_id, info in ext_map.items():
        name = cache.get(ext_id, "")
        if name and not info.name:
            info.name = name

    for ext_id, info in _installed_extension_info(set(ext_map)).items():
        current = ext_map[ext_id]
        if info.name and not current.name:
            current.name = info.name
            cache[ext_id] = info.name
            changed = True
        current.sources.update(info.sources)

    missing = {ext_id for ext_id, info in ext_map.items() if not info.name}
    if not missing:
        if changed:
            _write_name_cache(work_dir, cache)
        return

    for ext_id, name in _webstore_extension_names(missing).items():
        ext_map[ext_id].name = name
        cache[ext_id] = name
        changed = True

    if changed:
        _write_name_cache(work_dir, cache)


def _generate_html(ext_map: dict[str, ExtensionInfo]) -> str:
    rows = ""
    for ext_id, info in sorted(ext_map.items(), key=lambda x: (_display_name(x[1]).lower(), x[0])):
        display = escape(_display_name(info))
        source = _source_text(info)
        badge, color = _source_badge(info)
        url = _webstore_url(ext_id)
        rows += (
            f'<tr><td><a href="{url}" target="_blank" rel="noopener">{display}</a></td>'
            f'<td class="source"><span class="badge" title="{escape(source)}" '
            f'style="background:{color}">{escape(badge)}</span></td>'
            f'<td class="id">{ext_id}</td></tr>\n'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Extensions</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; padding: 24px;
          background: #282a36; color: #f8f8f2; }}
  h2 {{ color: #bd93f9; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ text-align: left; padding: 8px; color: #ff79c6;
        border-bottom: 2px solid #44475a; }}
  td {{ padding: 8px; border-bottom: 1px solid #44475a; }}
  td.source {{ text-align: center; width: 48px; }}
  .badge {{ display: inline-block; color: #282a36; border-radius: 5px;
            min-width: 28px; padding: 2px 0; font-weight: 700; font-size: 0.78em; }}
  td.id {{ color: #6272a4; font-size: 0.85em;
           font-family: ui-monospace, "JetBrains Mono", monospace; }}
  a {{ color: #8be9fd; text-decoration: none; }}
  a:hover {{ color: #50fa7b; text-decoration: underline; }}
  tr:hover td {{ background: #313340; }}
</style>
</head>
<body>
<h2>Extensions ({len(ext_map)})</h2>
<table>
<tr><th>Name</th><th>Source</th><th>ID</th></tr>
{rows}</table>
</body>
</html>"""


class ExtensionsManagerDialog(QDialog):
    def __init__(self, parent, sync_folder: Path):
        super().__init__(parent)
        self.setWindowTitle("Extensions")
        self.setMinimumWidth(560)
        self._sync_folder = sync_folder
        self._work_dir: Path | None = None
        self._deleted: set[str] = set()

        self._setup_work_dir()
        self._build_ui()

    def _setup_work_dir(self) -> None:
        from src.sync.sync_dir import SYNC_DIR_NAME

        self._work_dir = self._sync_folder / SYNC_DIR_NAME
        stale = self._work_dir / "extensions.html"
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass

    def _read_extensions(self) -> dict[str, ExtensionInfo]:
        from src.sync import extensions as sync_extensions

        assert self._work_dir
        keep_ext_ids = sync_extensions.extension_install_ids(
            self._work_dir,
            aliases=_all_ext_aliases(),
        )
        pruned = sync_extensions.prune_orphan_extension_data(self._work_dir, keep_ext_ids)
        if pruned:
            _LOG.info("Pruned orphan extension data: %s", ", ".join(sorted(pruned)))

        ext_map: dict[str, ExtensionInfo] = {}
        manifest = self._work_dir / "webstore_extensions.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for ext_id, name in data.items():
                        if isinstance(ext_id, str):
                            info = _info(ext_map, _canonical_ext_id(ext_id))
                            if isinstance(name, str) and name:
                                info.name = name
                            info.sources.add("Web Store")
                else:
                    for ext_id in data:
                        if isinstance(ext_id, str):
                            _info(ext_map, _canonical_ext_id(ext_id)).sources.add("Web Store")
            except (json.JSONDecodeError, OSError):
                pass

        for subdir in (
            "Extensions",
            "Local Extension Settings",
            "Sync Extension Settings",
            "Managed Extension Settings",
        ):
            base = self._work_dir / subdir
            if base.exists():
                for d in base.iterdir():
                    if d.is_dir():
                        info = _info(ext_map, _canonical_ext_id(d.name))
                        info.sources.add(
                            "Extension files" if subdir == "Extensions" else "Settings"
                        )

        idb = self._work_dir / "IndexedDB"
        if idb.exists():
            for d in idb.iterdir():
                if d.is_dir() and d.name.startswith("chrome-extension_"):
                    ext_id = d.name.removeprefix("chrome-extension_").split("_")[0]
                    _info(ext_map, _canonical_ext_id(ext_id)).sources.add("Settings")

        prefs_path = self._work_dir / "preferences.json"
        if prefs_path.exists():
            try:
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                extensions = prefs.get("extensions", {})
                pinned = (
                    extensions.get("pinned_extensions", []) if isinstance(extensions, dict) else []
                )
                if isinstance(pinned, list):
                    for ext_id in pinned:
                        if isinstance(ext_id, str):
                            _info(ext_map, ext_id).sources.add("Pinned")
            except (json.JSONDecodeError, OSError):
                pass

        _resolve_missing_names(self._work_dir, ext_map)

        return ext_map

    def _build_ui(self) -> None:
        ext_map = self._read_extensions()

        root = QVBoxLayout(self)

        if not ext_map:
            root.addWidget(QLabel("No extensions found in backup."))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)
            return

        toolbar = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Search extensions")
        search.textChanged.connect(self._filter_rows)
        self._search_edit = search
        toolbar.addWidget(search)
        html_btn = QPushButton("Open in Browser")
        html_btn.clicked.connect(lambda: self._open_html(ext_map))
        toolbar.addWidget(html_btn)
        toolbar.addStretch()
        root.addLayout(toolbar)

        table = QTableWidget(len(ext_map), 4)
        table.setHorizontalHeaderLabels(["Name", "Source", "Settings", ""])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setIconSize(QSize(28, 18))
        table.verticalHeader().setVisible(False)
        self._table = table

        assert self._work_dir
        for row, (ext_id, info) in enumerate(
            sorted(ext_map.items(), key=lambda x: (_display_name(x[1]).lower(), x[0]))
        ):
            source = _source_text(info)
            name_item = QTableWidgetItem(_display_name(info))
            name_item.setToolTip(ext_id)
            name_item.setData(Qt.ItemDataRole.UserRole, ext_id)
            table.setItem(row, 0, name_item)

            source_item = QTableWidgetItem()
            source_item.setIcon(_source_icon(info))
            source_item.setToolTip(source)
            source_item.setData(Qt.ItemDataRole.UserRole, source)
            source_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 1, source_item)

            size = _settings_size(self._work_dir, ext_id)
            size_item = QTableWidgetItem(_fmt_size(size) or "—")
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 2, size_item)

            del_btn = QPushButton("Delete")
            del_btn.clicked.connect(
                lambda _checked, eid=ext_id, r=row: self._delete_extension(eid, r)
            )
            table.setCellWidget(row, 3, del_btn)

        root.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if len(ext_map) >= 6:
            self.adjustSize()
            self.resize(self.width(), self.height() * 2)

    def _open_html(self, ext_map: dict[str, ExtensionInfo]) -> None:
        html_path = Path(tempfile.gettempdir()) / "chromium_profile_syncer_extensions.html"
        html_path.write_text(_generate_html(ext_map), encoding="utf-8")
        _open_urls([QUrl.fromLocalFile(str(html_path)).toString()])

    def _filter_rows(self, text: str) -> None:
        query = text.casefold().strip()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None:
                continue
            ext_id = item.data(Qt.ItemDataRole.UserRole)
            source = self._table.item(row, 1)
            source_text = source.data(Qt.ItemDataRole.UserRole) if source else ""
            haystack = f"{item.text()} {source_text} {ext_id}".casefold()
            self._table.setRowHidden(
                row, ext_id in self._deleted or bool(query and query not in haystack)
            )

    def _delete_extension(self, ext_id: str, row: int) -> None:
        from PySide6.QtWidgets import QMessageBox

        reply = QMessageBox.question(
            self,
            "Delete Extension",
            f"Remove '{ext_id}' and all its settings from backup?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._deleted.add(ext_id)
        self._filter_rows(self._search_edit.text())

    def _apply_deletions(self) -> None:
        from src.sync.extensions import remove_extension_data

        assert self._work_dir
        remove_extension_data(self._work_dir, self._deleted, remove_install_records=True)

    def _save(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._deleted:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._apply_deletions()
            finally:
                QApplication.restoreOverrideCursor()

        self.accept()

    def done(self, result: int) -> None:
        self._work_dir = None
        super().done(result)
