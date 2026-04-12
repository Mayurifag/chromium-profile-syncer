# Project Conventions

## Dependency Management

Never hand-edit `uv.lock`. Use `uv add` / `uv remove` to manage dependencies.

## Running Commands

Always prefix invocations with `uv run`:

~~~sh
uv run pytest
uv run ruff check src tests
uv run python -m src.main
uv run python build.py
~~~

## After Fixes

Always reinstall and relaunch after code changes:

~~~sh
make install
~~~

This builds the app, kills the running instance, installs, and launches automatically.

## Technology Constraints

- **PySide6** — do not substitute PyQt6 or any other binding.
- **Python 3.12+** — do not use syntax or stdlib features unavailable in 3.12.
- **rclone** — required runtime dependency for fast backup rotation (must be installed via system package manager).

## Project Structure

- `src/` — application source package
- `tests/` — test files
- `build.py` — PyInstaller build script
- `pyproject.toml` — project metadata and tool configuration

## Code Style

- Line length: 100 characters
- Ruff rules: E, F, I, UP
- Target: Python 3.12
- **No obvious comments** — do not add comments that merely restate what the code does. Comments should only explain *why*, not *what*. The code itself should be self-explanatory through clear naming and structure.

## Key Implementation Details

### Smart Extension Syncing

**Detection:** Web Store extensions have `_metadata/verified_contents.json` (Google signature)
- **Web Store extensions:** Only save extension ID to `webstore_extensions.json` manifest
- **Unpacked extensions:** Sync full code (can't be re-downloaded)
- **Auto-installation:** Generate External Extensions JSON stubs from manifest

### Trash File Exclusion

Files excluded from sync:
- `._*` — macOS metadata on exFAT/FAT32 drives

**Implementation:**
- rclone: `--exclude "._*"`
- shutil.copytree: `ignore=shutil.ignore_patterns("._*")`

### Backup Rotation

Uses rclone for fast parallel backup rotation:
- Parses `--stats-one-line` output for progress percentages
- Runs with `--transfers 8 --checkers 16` for parallelism
- Reports progress via callback to show in UI

### First-Sync Detection

- Checks if `metadata.json` exists before syncing
- If missing → first-time setup (shows "Initial setup complete")
- If exists → regular sync (shows "Last sync: [timestamp]")
- Prevents "sync complete" spam on clean slate

### Clean Button (Settings Window)

- Visible only when sync folder contains data (`current/` directory exists)
- Deletes all synced data: `current/`, `backup-1/`, `backup-2/`, `metadata.json`
- Clears enabled profiles and browsers from config
- Shows confirmation dialog before deletion
- After cleaning, triggers initial upload dialog to start fresh
