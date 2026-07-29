#!/usr/bin/env bash
# One-command zero-to-dev bootstrap: installs uv if missing, syncs deps,
# installs pre-commit hooks, and runs lint/typecheck/tests.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v uv >/dev/null 2>&1; then
  echo "==> uv not found; installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> uv version: $(uv --version)"

echo "==> Syncing dependencies (uv sync)..."
uv sync --group dev

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
fi

echo "==> Installing pre-commit hooks..."
uv run pre-commit install

echo "==> Running nox (lint, typecheck, tests)..."
uv run nox -s lint typecheck tests

cat <<'EOF'

==============================================================
Bootstrap complete.

Next steps:
  uv run python -m qscrape --help
  uv run python -m qscrape --out data/backends.json

Other useful commands:
  make test        # run pytest
  make lint        # ruff check
  make fmt          # ruff format
  make typecheck    # mypy --strict
==============================================================
EOF
