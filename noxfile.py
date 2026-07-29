"""Nox sessions: lint, typecheck, tests -- all run against uv-managed environments."""

from __future__ import annotations

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True

PYTHON_VERSION = "3.12"
SRC = "src"


def _sync_dev(session: nox.Session) -> None:
    env = {"UV_PROJECT_ENVIRONMENT": session.virtualenv.location}
    session.run_install("uv", "sync", "--frozen", "--group", "dev", env=env)


@nox.session(python=PYTHON_VERSION)
def lint(session: nox.Session) -> None:
    """Run ruff lint checks."""
    _sync_dev(session)
    session.run("ruff", "check", ".")


@nox.session(python=PYTHON_VERSION)
def fmt(session: nox.Session) -> None:
    """Check formatting with ruff format."""
    _sync_dev(session)
    session.run("ruff", "format", "--check", ".")


@nox.session(python=PYTHON_VERSION)
def typecheck(session: nox.Session) -> None:
    """Run mypy in strict mode against src/."""
    _sync_dev(session)
    session.run("mypy", SRC)


@nox.session(python=PYTHON_VERSION)
def tests(session: nox.Session) -> None:
    """Run the pytest suite with coverage."""
    _sync_dev(session)
    session.run("pytest", *session.posargs)
