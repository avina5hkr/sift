test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check . && uv run mypy

web:
	npm --prefix web ci && npm --prefix web run build

web-test:
	npm --prefix web run lint && npm --prefix web run typecheck && npm --prefix web test -- --run

dev:
	./scripts/dev.sh

e2e: web
	cd web && npm exec playwright test

.PHONY: test lint web web-test dev e2e
