.PHONY: install ci e2e

install:
	uv run python build.py --install

ci:
	uv run ruff check src tests --fix
	uv run pytest

e2e:
	uv run python scripts/e2e.py
