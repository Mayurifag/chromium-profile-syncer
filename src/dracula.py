# Core Dracula colors
BACKGROUND = "#282a36"
BACKGROUND_DARK = "#1e1e1e"
CURRENT_LINE = "#44475a"
FOREGROUND = "#f8f8f2"
COMMENT = "#6272a4"
CYAN = "#8be9fd"
GREEN = "#50fa7b"
ORANGE = "#ffb86c"
PINK = "#ff79c6"
PURPLE = "#bd93f9"
RED = "#ff5555"
YELLOW = "#f1fa8c"

# Extended palette
BUTTON_HOVER = COMMENT
DISABLED_BG = "#3a3c4e"
DISABLED_FG = COMMENT

# Log level colors
LOG_COLORS = {
    "DEBUG": "#808080",
    "INFO": "#4ec9b0",
    "WARNING": "#dcdcaa",
    "ERROR": "#f48771",
    "CRITICAL": RED,
}
DEFAULT_LOG_COLOR = "#d4d4d4"

# Icon state colors
ICON_COLORS = {
    "idle": PURPLE,
    "syncing": CYAN,
    "waiting": YELLOW,
    "error": RED,
}

# Browser running indicator colors
RUNNING_GLOW = (80, 200, 120, 60)  # RGBA
RUNNING_DOT = (80, 200, 120)       # RGB
NOT_RUNNING_DOT = (98, 114, 164)   # RGB (#6272a4)

# Text styles (for setStyleSheet calls)
MUTED_TEXT = f"color: {COMMENT}; font-size: 10px;"
BOLD_HEADING = "font-weight: bold; font-size: 12pt;"
BOLD_LABEL = "font-weight: bold; margin-top: 8px;"
SMALL_MUTED = f"font-size: 10px; color: {COMMENT};"

# Widget-specific styles
LOG_VIEWER_TEXTEDIT = f"""
QTextEdit {{
    background-color: {BACKGROUND_DARK};
    color: {DEFAULT_LOG_COLOR};
    border: none;
}}
"""

PROFILE_ROW_STYLE = f"""
QWidget#profile_row {{
    background-color: {BACKGROUND};
}}
QPushButton {{
    background-color: {CURRENT_LINE};
}}
"""

# Main application stylesheet
APP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <circle cx="50" cy="50" r="48" fill="#282a36"/>
  <path d="M 18 50 A 32 32 0 0 1 82 50"
        fill="none" stroke="#bd93f9" stroke-width="9" stroke-linecap="round"/>
  <polygon points="82,58 76,44 88,44" fill="#bd93f9"/>
  <path d="M 82 50 A 32 32 0 0 1 18 50"
        fill="none" stroke="#8be9fd" stroke-width="9" stroke-linecap="round"/>
  <polygon points="18,42 12,56 24,56" fill="#8be9fd"/>
</svg>"""

DRACULA_STYLESHEET = f"""
QWidget {{
    background-color: {BACKGROUND};
    color: {FOREGROUND};
}}

QDialog {{
    background-color: {BACKGROUND};
}}

QGroupBox {{
    color: {FOREGROUND};
    border: 1px solid {CURRENT_LINE};
    margin-top: 0.5em;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
}}

QLabel {{
    color: {FOREGROUND};
}}

QLineEdit {{
    background-color: {CURRENT_LINE};
    color: {FOREGROUND};
    border: 1px solid {COMMENT};
    selection-background-color: {PURPLE};
    padding: 2px 6px;
    min-height: 16px;
}}

QLineEdit:read-only {{
    background-color: {DISABLED_BG};
    color: {COMMENT};
}}

QLineEdit:focus {{
    border: 1px solid {CYAN};
}}

QPushButton {{
    background-color: {CURRENT_LINE};
    color: {FOREGROUND};
    border: none;
    padding: 1px 10px;
    min-height: 14px;
}}

QPushButton:hover {{
    background-color: {BUTTON_HOVER};
}}

QPushButton:pressed {{
    background-color: {BACKGROUND};
}}

QPushButton:disabled {{
    background-color: {DISABLED_BG};
    color: {DISABLED_FG};
}}

QComboBox {{
    background-color: {CURRENT_LINE};
    color: {FOREGROUND};
    border: 1px solid {COMMENT};
    padding: 1px 4px;
    min-height: 14px;
}}

QComboBox QAbstractItemView {{
    background-color: {CURRENT_LINE};
    color: {FOREGROUND};
    selection-background-color: {PURPLE};
}}

QProgressBar {{
    border: 1px solid {CURRENT_LINE};
    background-color: {CURRENT_LINE};
    color: {FOREGROUND};
}}

QProgressBar::chunk {{
    background-color: {GREEN};
}}

QTextEdit {{
    background-color: {BACKGROUND};
    color: {FOREGROUND};
    border: 1px solid {CURRENT_LINE};
}}

QMenu {{
    background-color: {BACKGROUND};
    color: {FOREGROUND};
}}

QMenu::item:selected {{
    background-color: {CURRENT_LINE};
}}

QMenu::item:disabled {{
    color: {COMMENT};
}}

QMenu::separator {{
    height: 1px;
    background-color: {CURRENT_LINE};
}}
"""
