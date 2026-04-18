.PHONY: install ci e2e e2e2 e2e2-clean

install:
	uv run python build.py --install

ci:
	uv run ruff check src tests --fix
	uv run pytest

e2e:
	uv run python scripts/e2e.py

e2e2:
	uv run python scripts/e2e2.py

e2e2-clean:
	uv run python scripts/e2e2.py --clean
