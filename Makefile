.PHONY: install ci

install:
	uv run python build.py --install

ci:
	uv run ruff check src tests --fix
	uv run pytest
