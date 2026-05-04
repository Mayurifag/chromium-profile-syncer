from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src import config as _config
from src.browsers import ALL_BROWSERS
from src.sync.flags import get_local_flags, load_sync_flags, remove_flags

_LOG = logging.getLogger(__name__)


def _collect_all_flags(sync_root: Path) -> dict[str, set[str]]:
    """Return {flag: {sources}} where sources is set of browser names + 'sync'."""
    flag_sources: dict[str, set[str]] = {}

    sync_data = load_sync_flags(sync_root)
    for browser_name, entry in sync_data.items():
        for f in entry.get("enabled_labs_experiments", []):
            flag_sources.setdefault(f, set()).add(f"sync:{browser_name}")

    for browser in ALL_BROWSERS:
        if not browser.is_installed():
            continue
        local_state = browser.local_state_path()
        if local_state is None or not local_state.exists():
            continue
        for f in get_local_flags(local_state):
            flag_sources.setdefault(f, set()).add(f"local:{browser.name}")

    return flag_sources


class FlagsManagerDialog(QDialog):
    def __init__(self, parent, sync_folder: Path):
        super().__init__(parent)
        self.setWindowTitle("Flags")
        self.setMinimumWidth(680)
        self.setMinimumHeight(400)
        self._sync_folder = sync_folder
        self._ignore_checks: dict[str, QCheckBox] = {}
        self._remove_checks: dict[str, QCheckBox] = {}
        self._build_ui()

    def _sync_root(self) -> Path:
        from src.sync.sync_dir import SYNC_DIR_NAME
        return self._sync_folder / SYNC_DIR_NAME

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        flag_sources = _collect_all_flags(self._sync_root())
        ignore_set = set(_config.get_flags_ignore())

        if not flag_sources:
            root.addWidget(QLabel(
                "No flags found.\n\n"
                "Enable some flags in chrome://flags or helium://flags, "
                "close the browser, and run a sync."
            ))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            for btn in buttons.buttons():
                btn.setIcon(QIcon())
            root.addWidget(buttons)
            return

        info = QLabel(
            f"{len(flag_sources)} flag(s) tracked. "
            "Ignore = keep in sync but skip on this machine. "
            "Remove = delete from sync and all local browsers."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        table = QTableWidget(len(flag_sources), 4)
        table.setHorizontalHeaderLabels(["Flag", "Source", "Ignore", "Remove"])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)

        from PySide6.QtWidgets import QWidget

        def _wrap(check: QCheckBox) -> QWidget:
            cell = QHBoxLayout()
            cell.addWidget(check)
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setContentsMargins(0, 0, 0, 0)
            wrap = QWidget()
            wrap.setLayout(cell)
            return wrap

        for row, (flag, sources) in enumerate(sorted(flag_sources.items())):
            flag_item = QTableWidgetItem(flag)
            flag_item.setToolTip(flag)
            table.setItem(row, 0, flag_item)

            source_str = ", ".join(sorted(sources))
            source_item = QTableWidgetItem(source_str)
            source_item.setToolTip(source_str)
            table.setItem(row, 1, source_item)

            ignore_chk = QCheckBox()
            ignore_chk.setChecked(flag in ignore_set)
            self._ignore_checks[flag] = ignore_chk
            table.setCellWidget(row, 2, _wrap(ignore_chk))

            remove_chk = QCheckBox()
            self._remove_checks[flag] = remove_chk
            remove_chk.toggled.connect(
                lambda checked, ic=ignore_chk: ic.setEnabled(not checked)
            )
            table.setCellWidget(row, 3, _wrap(remove_chk))

        root.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        for btn in buttons.buttons():
            btn.setIcon(QIcon())
        root.addWidget(buttons)

    def _save(self) -> None:
        to_remove = {f for f, chk in self._remove_checks.items() if chk.isChecked()}
        if to_remove:
            browser_states = [
                (b.name, ls)
                for b in ALL_BROWSERS
                if b.is_installed() and (ls := b.local_state_path()) is not None
            ]
            remove_flags(self._sync_root(), to_remove, browser_states)
            _LOG.info("Removed %d flag(s) from sync and local browsers", len(to_remove))

        ignore = [
            f for f, chk in self._ignore_checks.items()
            if chk.isChecked() and f not in to_remove
        ]
        _config.set_flags_ignore(ignore)
        self.accept()
