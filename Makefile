.PHONY: run test lint fmt fmt-check typecheck clean

run:
	uv run python -m qscrape

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run mypy src

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
