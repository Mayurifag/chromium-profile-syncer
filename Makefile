.PHONY: install install-release ci e2e

install:
	uv run python build.py --install

install-release:
	uv run python scripts/install_release.py

ci:
	uv run ruff check src tests scripts build.py
	uv run pytest

e2e:
	uv run pytest tests/test_lifecycle.py
