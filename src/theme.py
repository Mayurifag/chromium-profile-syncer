"""Minimal Dracula theme - just colors, no layout changes."""

DRACULA_STYLESHEET = """
/* Dracula colors with minimal styling */
QWidget {
    background-color: #282a36;
    color: #f8f8f2;
}

QDialog {
    background-color: #282a36;
}

QGroupBox {
    color: #f8f8f2;
    border: 1px solid #44475a;
    margin-top: 0.5em;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
}

QLabel {
    color: #f8f8f2;
}

QLineEdit {
    background-color: #44475a;
    color: #f8f8f2;
    border: 1px solid #6272a4;
    selection-background-color: #bd93f9;
    padding: 4px 8px;
    min-height: 20px;
}

QLineEdit:read-only {
    background-color: #3a3c4e;
    color: #6272a4;
}

QLineEdit:focus {
    border: 1px solid #8be9fd;
}

QPushButton {
    background-color: #44475a;
    color: #f8f8f2;
    border: none;
    padding: 4px 12px;
    min-height: 20px;
}

QPushButton:hover {
    background-color: #6272a4;
}

QPushButton:pressed {
    background-color: #282a36;
}

QPushButton:disabled {
    background-color: #3a3c4e;
    color: #6272a4;
}

QComboBox {
    background-color: #44475a;
    color: #f8f8f2;
    border: 1px solid #6272a4;
    padding: 4px 8px;
    min-height: 20px;
}

QComboBox QAbstractItemView {
    background-color: #44475a;
    color: #f8f8f2;
    selection-background-color: #bd93f9;
}

QProgressBar {
    border: 1px solid #44475a;
    background-color: #44475a;
    color: #f8f8f2;
}

QProgressBar::chunk {
    background-color: #50fa7b;
}

QTextEdit {
    background-color: #282a36;
    color: #f8f8f2;
    border: 1px solid #44475a;
}

QMenu {
    background-color: #282a36;
    color: #f8f8f2;
}

QMenu::item:selected {
    background-color: #44475a;
}

QMenu::item:disabled {
    color: #6272a4;
}

QMenu::separator {
    height: 1px;
    background-color: #44475a;
}
"""
