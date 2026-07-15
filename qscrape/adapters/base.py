"""Adapter contract.

An adapter knows how to turn one *source tier* for one vendor into
``BackendRecord`` objects. Adapters must NEVER invent values: if a datum is
absent, leave the Field at its UNKNOWN default.
"""
from __future__ import annotations

import abc
from typing import Iterable

from ..httpcache import HttpCache
from ..models import BackendRecord


class Adapter(abc.ABC):
    #: short vendor key, e.g. "ibm", "ionq"
    vendor: str = ""
    #: source tier: "cloud-api" | "calibration-api" | "vendor" | "publication"
    tier: str = "vendor"

    def __init__(self, http: HttpCache, config: dict | None = None):
        self.http = http
        self.config = config or {}

    @abc.abstractmethod
    def fetch(self) -> Iterable[BackendRecord]:
        """Yield zero or more backend records. Must not raise on network error;
        return what it can and attach failures to record.meta['warnings']."""
        raise NotImplementedError

    # convenience for adapters that need to note a non-fatal problem
    @staticmethod
    def warn(record: BackendRecord, msg: str) -> None:
        record.meta.setdefault("warnings", []).append(msg)
