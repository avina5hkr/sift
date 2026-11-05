test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check . && uv run mypy

web:
	npm --prefix web ci && npm --prefix web run build

web-test:
	npm --prefix web run lint && npm --prefix web run typecheck && npm --prefix web test -- --run

.PHONY: test lint web web-test
