"""Runtime configuration, loaded from environment variables / .env (12-factor: config).

Replaces the previous module-level ``ROOT`` path arithmetic and scattered
``os.environ.get(...)`` calls (``IBM_QUANTUM_TOKEN`` in adapters/ibm.py,
``cache_max_age``/``request_delay`` defaults duplicated in cli.py and
pipeline.py) with a single typed, testable settings object.

CLI flags in ``cli.py`` always take precedence over these env-derived
defaults -- ``Settings`` only supplies the value when a flag was not
explicitly passed on the command line.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root (three levels up from this file: src/qscrape/settings.py).
ROOT = Path(__file__).parent.parent.parent


class Settings(BaseSettings):
    """Central settings object. All values are overridable via environment
    variables (prefixed ``QSCRAPE_``, except the IBM token which also accepts
    the historical unprefixed ``IBM_QUANTUM_TOKEN`` name for backward
    compatibility) or a ``.env`` file; see ``.env.example``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="QSCRAPE_",
        extra="ignore",
    )

    # -- logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    # -- paths ---------------------------------------------------------------
    config_path: Path = ROOT / "config" / "sources.json"
    out_path: Path = ROOT / "data" / "backends.json"
    xlsx_path: Path = ROOT / "data" / "quantum_tracker.xlsx"
    cache_dir: Path = ROOT / ".cache"

    # -- fetch behaviour (also settable per-source in config/sources.json;
    #    these are the CLI/env-level fallback defaults) ----------------------
    cache_max_age: float = 86400.0
    request_delay: float = 1.0

    # -- IBM Quantum Runtime credentials -------------------------------------
    ibm_quantum_token: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("QSCRAPE_IBM_QUANTUM_TOKEN", "IBM_QUANTUM_TOKEN"),
    )

    # -- AWS credentials for the Braket adapter ------------------------------
    # The amazon-braket-sdk / boto3 resolve credentials via the standard AWS
    # credential chain (env vars, ~/.aws/credentials, instance profile, ...)
    # on their own; qscrape does not read these directly today. They are
    # surfaced here so operators have one place to see/set them, and so a
    # future adapter change can consume them without another settings pass.
    aws_access_key_id: str | None = Field(default=None, repr=False)
    aws_secret_access_key: str | None = Field(default=None, repr=False)
    aws_region: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()
