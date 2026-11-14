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

N ?= 200

fake-phone:
	uv run python scripts/fake_phone.py --files $(N)

e2e: web
	cd web && npm exec playwright test

.PHONY: test lint web web-test dev fake-phone e2e codemap build

codemap:
	uv run python scripts/codemap.py > docs/CODEMAP.md

bench:
	SIFT_TRACKER=off uv run pytest -q -s -m bench tests/m9/test_bench.py

build:
	packaging/build_dmg.sh
