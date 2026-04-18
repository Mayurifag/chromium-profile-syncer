from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

_CHEATSHEET = """<h3>After restoring to a new machine / fresh profile</h3>

<b>Extensions — Chrome / Thorium</b><br>
Go to <code>chrome://extensions</code> and enable each extension manually (one by one).
<br><br>

<b>Extensions — Helium</b><br>
Install each extension from the Web Store manually (one by one).<br>
The app generates install stubs, but Helium needs a user-initiated install per extension.
<br><br>

<b>Search shortcuts</b><br>
1. Open Settings → <i>Edit Search Shortcuts</i>.<br>
2. Check <i>Default</i> on your preferred shortcut.<br>
3. Remove any bundled search engine you don't want.<br>
4. Save.
<br><br>

<b>Favicons</b><br>
Favicons are not synced. Visit your bookmarked and shortcut sites to restore them."""


class HelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Usage notes")
        self.setMinimumWidth(480)

        label = QLabel(_CHEATSHEET)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(buttons)
