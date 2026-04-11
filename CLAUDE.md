# Project Conventions

## Dependency Management

Never hand-edit `uv.lock`. Use `uv add` / `uv remove` to manage dependencies.

~~~sh
uv add somepackage
uv remove somepackage
~~~

## Running Commands

Always prefix Python and tool invocations with `uv run` to use the project-managed virtual environment:

~~~sh
# Run tests
uv run pytest

# Lint (never run ruff directly)
uv run ruff check src tests

# Run the app
uv run python -m src.main
# or
uv run python src/main.py

# Build executable
uv run python build.py
~~~

Do **not** run `pytest` or `ruff` directly — use `uv run` to ensure the correct environment.

## Technology Constraints

- **PySide6** — the Qt binding used throughout. Do not substitute PyQt6 or any other binding.
- **Python 3.12+** — required. Do not use syntax or stdlib features unavailable in 3.12.

## Project Structure

- `src/` — application source package
- `tests/` — test files (place all new tests here)
- `build.py` — PyInstaller build script (cross-platform)
- `pyproject.toml` — project metadata and tool configuration

## Code Style

- Line length: 100 characters (configured in `pyproject.toml`)
- Ruff rules: E, F, I, UP
- Target: Python 3.12

## UV Config Note

If bare `uv` commands fail with config errors, prefix with `UV_NO_CONFIG=1`:

~~~sh
UV_NO_CONFIG=1 uv sync --group dev
~~~

This works around any global `uv.toml` settings that may conflict.
