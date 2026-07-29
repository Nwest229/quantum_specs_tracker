# Contributing

## Setup

```bash
./bootstrap.sh
```

This installs `uv` if needed, syncs dependencies (`uv sync --group dev`),
installs the pre-commit hooks, and runs lint/typecheck/tests once so you know
the checkout is healthy.

## Workflow

1. Create a branch from `main`.
2. Make changes.
3. Run checks locally before pushing:

   ```bash
   make lint         # uv run ruff check .
   make fmt-check     # uv run ruff format --check .
   make typecheck     # uv run mypy src
   make test          # uv run pytest
   ```

   or run everything the CI runs in one go:

   ```bash
   uv run nox -s lint typecheck tests
   ```

4. If you're adding a new data source, verify the end-to-end pipeline still
   produces valid, schema-checked output:

   ```bash
   uv run python -m qscrape --only <vendor>   # test just the new vendor
   uv run python -m qscrape                    # full run
   ```

5. Open a pull request. CI runs lint, `mypy --strict`, and pytest across
   Python 3.12 and 3.13 (see `.github/workflows/ci.yml`).

## Commit style

Use concise, imperative commit messages (e.g. `Add IQM Garnet spec source`).
Conventional-commit-style prefixes (`feat:`, `fix:`, `chore:`, `docs:`,
`test:`) are welcome but not required.

## Code style

- Formatting and linting are enforced by `ruff` (see `pyproject.toml`).
- Type checking is `mypy --strict`; all new code in `src/qscrape` must be
  fully typed.
- Every data value the pipeline emits must carry a `source` (URL) and
  `method`; never hardcode a number without provenance. See the "Adding a
  new backend" section in `README.md`.
- Settings/config belong in `src/qscrape/settings.py`; don't reintroduce
  scattered `os.environ.get(...)` calls.
- Internal diagnostics (warnings, validation errors, adapter skip notices)
  go through `src/qscrape/logging.py`'s structlog loggers; the CLI's
  plain-text run summary stays plain `print()`.

## Data changes

`data/backends.json` is intentionally tracked in git (the GitHub Pages viewer
fetches it directly), so if your change affects the pipeline output, re-run
`uv run python -m qscrape` and commit the refreshed file alongside your code
change.
