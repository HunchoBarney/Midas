PYTHON_VERSION ?= 3.12

.PHONY: bootstrap fmt lint test

bootstrap:
	uv python install $(PYTHON_VERSION)
	uv sync --dev

fmt:
	uv run ruff format .

lint:
	uv run ruff format --check .
	uv run ruff check .

test:
	uv run pytest

