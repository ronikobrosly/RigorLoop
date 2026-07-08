# Development entry points; each mirrors a CI job exactly.

default: check

lint:
    uv run ruff check .
    uv run ruff format --check .

fmt:
    uv run ruff check --fix .
    uv run ruff format .

typecheck:
    uv run mypy

test:
    uv run pytest --cov --cov-report=term-missing
    uv run coverage report --include='*/rigorloop/core/*' --fail-under=95

check: lint typecheck test

build:
    uv build
