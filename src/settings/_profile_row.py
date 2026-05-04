from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import src.config as config_module
from src.dracula import PROFILE_ROW_STYLE, SMALL_MUTED
from src.settings._helpers import _CLOSE_BROWSER_HINT, _sync_folder_has_profile

_LOG = logging.getLogger(__name__)


@dataclass
class RowContext:
    parent: QWidget
    profile_states: dict[str, dict[str, bool]]
    profile_progress: dict[tuple[str, str], tuple[QProgressBar, QLabel]]
    apply_backup_buttons: dict[tuple[str, str], QPushButton]
    sync_toggle_buttons: dict[tuple[str, str], QPushButton]
    remove_profile_buttons: dict[tuple[str, str], QPushButton]
    is_syncing: Callable[[], bool]
    set_syncing: Callable[[bool], None]
    save_profiles_config: Callable[[], None]
    refresh_apply_backup_enabled: Callable[[], None]
    emit_apply_backup: Callable[[str, str], None]
    remove_profile: Callable[[str, str, Path], None]


def add_profile_row(
    ctx: RowContext,
    layout: QVBoxLayout,
    browser_name: str,
    profile_name: str,
    folder: Path | None,
    is_running: bool,
    is_enabled: bool,
    prefix_widgets: list,
    profile_path: Path | None = None,
) -> None:
    row = QWidget()
    row.setObjectName("profile_row")
    row.setStyleSheet(PROFILE_ROW_STYLE)
    row_layout = QVBoxLayout(row)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setSpacing(0)

    top_row = QWidget()
    top_layout = QHBoxLayout(top_row)
    top_layout.setContentsMargins(0, 0, 0, 0)
    top_layout.setSpacing(4)

    for w in prefix_widgets:
        top_layout.addWidget(w)

    if browser_name == "Thorium" and profile_path is not None:
        _add_remove_button(ctx, top_layout, browser_name, profile_name, is_running, profile_path)

    top_layout.addStretch()

    sync_toggle_btn = _add_sync_toggle_button(
        ctx, top_layout, browser_name, profile_name, is_enabled
    )
    _add_apply_backup_button(
        ctx, top_layout, browser_name, profile_name, is_running, sync_toggle_btn, folder
    )

    progress_bar = QProgressBar()
    progress_bar.setMaximumHeight(8)
    progress_bar.setTextVisible(False)
    progress_bar.setVisible(False)

    info_label = QLabel()
    info_label.setStyleSheet(SMALL_MUTED)
    info_label.setVisible(False)

    ctx.profile_progress[(browser_name, profile_name)] = (progress_bar, info_label)

    row_layout.addWidget(top_row)
    row_layout.addWidget(progress_bar)
    row_layout.addWidget(info_label)
    layout.addWidget(row)


def _add_remove_button(
    ctx: RowContext,
    top_layout: QHBoxLayout,
    browser_name: str,
    profile_name: str,
    is_running: bool,
    profile_path: Path,
) -> None:
    btn = QPushButton("Remove Profile")
    btn.setFixedWidth(110)
    btn.setEnabled(not is_running)
    if is_running:
        btn.setToolTip(_CLOSE_BROWSER_HINT)
    ctx.remove_profile_buttons[(browser_name, profile_name)] = btn
    top_layout.addWidget(btn)
    btn.clicked.connect(lambda: ctx.remove_profile(browser_name, profile_name, profile_path))


def _add_sync_toggle_button(
    ctx: RowContext,
    top_layout: QHBoxLayout,
    browser_name: str,
    profile_name: str,
    is_enabled: bool,
) -> QPushButton:
    btn = QPushButton()
    btn.setFixedWidth(110)
    sync_enabled = config_module.is_profile_sync_enabled(browser_name, profile_name)
    btn.setText("Auto-sync: ON" if sync_enabled else "Auto-sync: OFF")
    btn.setVisible(is_enabled)
    ctx.sync_toggle_buttons[(browser_name, profile_name)] = btn
    top_layout.addWidget(btn)
    btn.clicked.connect(lambda: _toggle_sync(browser_name, profile_name, btn))
    return btn


def _toggle_sync(browser_name: str, profile_name: str, btn: QPushButton) -> None:
    enabled = config_module.is_profile_sync_enabled(browser_name, profile_name)
    config_module.set_profile_sync_enabled(browser_name, profile_name, not enabled)
    btn.setText("Auto-sync: ON" if not enabled else "Auto-sync: OFF")


def _add_apply_backup_button(
    ctx: RowContext,
    top_layout: QHBoxLayout,
    browser_name: str,
    profile_name: str,
    is_running: bool,
    sync_toggle_btn: QPushButton,
    folder: Path | None,
) -> None:
    btn = QPushButton("Apply Backup")
    btn.setFixedWidth(100)
    btn.setEnabled(not is_running and not ctx.is_syncing())
    if is_running:
        btn.setToolTip(_CLOSE_BROWSER_HINT)
    ctx.apply_backup_buttons[(browser_name, profile_name)] = btn
    top_layout.addWidget(btn)
    btn.clicked.connect(
        lambda: _on_apply_clicked(ctx, browser_name, profile_name, sync_toggle_btn, folder)
    )


def _on_apply_clicked(
    ctx: RowContext,
    browser_name: str,
    profile_name: str,
    sync_toggle_btn: QPushButton,
    folder: Path | None,
) -> None:
    _LOG.debug("Apply Backup clicked: %s/%s", browser_name, profile_name)
    try:
        currently = ctx.profile_states[browser_name][profile_name]
    except KeyError:
        _LOG.error("Apply Backup: profile state missing for %s/%s", browser_name, profile_name)
        return

    if not currently:
        ctx.profile_states[browser_name][profile_name] = True
        sync_toggle_btn.setVisible(True)
        if folder is not None and _sync_folder_has_profile(folder):
            config_module.mark_profile_for_restore(browser_name, profile_name)
            _LOG.info("Profile %s/%s marked for initial restore", browser_name, profile_name)
        ctx.save_profiles_config()
        ctx.set_syncing(True)
        ctx.refresh_apply_backup_enabled()
        ctx.emit_apply_backup(browser_name, profile_name)
        return

    reply = QMessageBox.question(
        ctx.parent,
        "Apply Backup",
        f"Overwrite local {browser_name} profile with backup?\n\n"
        "This will replace local data with the synced backup.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if reply == QMessageBox.StandardButton.Yes:
        ctx.set_syncing(True)
        ctx.refresh_apply_backup_enabled()
        ctx.emit_apply_backup(browser_name, profile_name)
