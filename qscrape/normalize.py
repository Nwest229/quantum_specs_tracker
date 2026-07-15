"""Normalisation helpers and derived-metric computation."""
from __future__ import annotations

import math
import re
import statistics
from typing import Iterable, Optional

from .models import UNKNOWN, BackendRecord, Field, F


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def parse_number(text) -> Optional[float]:
    """Pull the first number out of a string; returns None if none present."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM_RE.search(str(text))
    return float(m.group()) if m else None


def parse_percent_as_fraction(text) -> Optional[float]:
    """'99.7%' -> 0.997 ; '0.997' -> 0.997 ; '99.7' -> 0.997."""
    n = parse_number(text)
    if n is None:
        return None
    if "%" in str(text) or n > 1.0:
        return n / 100.0
    return n


# ---------------------------------------------------------------------------
# Fidelity statistics from a per-gate calibration dump
# ---------------------------------------------------------------------------
def fidelity_stats(fidelities: Iterable[float]) -> dict:
    vals = [v for v in fidelities if v is not None]
    if not vals:
        return {}
    return {
        "n": len(vals),
        "max": max(vals),
        "min": min(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
    }


def apply_fidelity_stats(record: BackendRecord, two_q: Iterable[float],
                         one_q: Optional[Iterable[float]] = None,
                         spam: Optional[Iterable[float]] = None,
                         source: str = "", retrieved: str = "",
                         kind: str = "calibration-api") -> None:
    """Populate fidelity.* leaves from raw per-gate calibration lists.

    Only used when a vendor exposes every gate (IBM, Rigetti, cloud APIs).
    """
    def put(path, value, method):
        if value is not None:
            record.set(path, F(round(value, 6), source, retrieved, method, kind))

    tq = fidelity_stats(two_q)
    if tq:
        put("fidelity.2q_max", tq["max"], "maximum")
        put("fidelity.2q_min", tq["min"], "minimum")
        put("fidelity.2q_avg", tq["mean"], "average")
        put("fidelity.2q_median", tq["median"], "median")
    oq = fidelity_stats(one_q or [])
    if oq:
        put("fidelity.1q_max", oq["max"], "maximum")
        put("fidelity.1q_min", oq["min"], "minimum")
        put("fidelity.1q_avg", oq["mean"], "average")
    sp = fidelity_stats(spam or [])
    if sp:
        put("fidelity.spam_avg", sp["mean"], "average")

    record.derived_metrics.setdefault("fidelity_stats", {})
    if tq:
        record.derived_metrics["fidelity_stats"]["2q"] = tq
    if oq:
        record.derived_metrics["fidelity_stats"]["1q"] = oq


# ---------------------------------------------------------------------------
# Derived: "theoretical maximum" headroom metric
# ---------------------------------------------------------------------------
def compute_theoretical_max(record: BackendRecord) -> None:
    """Compute 2^min(N, 1/eps_2q) as specified in the source prompt.

    NOTE: this is a rough headroom proxy, NOT a rigorously defined benchmark.
    The exponent 1/eps_2q approximates the number of two-qubit gates before an
    error is expected (circuit depth), while N is the qubit count (width);
    capping one by the other conflates depth and width. We compute it because
    the spec asks for it, but store the caveat and the inputs so the number is
    reproducible and clearly labelled. Prefer measured Quantum Volume where
    available.
    """
    n_field: Field = record.qpu_topology.get("qubits", Field())
    # Prefer average; fall back to median then min so vendors that only publish
    # a summary statistic still get a (clearly-labelled) headroom estimate.
    f2q_field = next((record.fidelity[k] for k in ("2q_avg", "2q_median", "2q_min")
                      if record.fidelity.get(k, Field()).known), Field())

    N = parse_number(n_field.value) if n_field.known else None
    f2q = parse_percent_as_fraction(f2q_field.value) if f2q_field.known else None

    if N is None or f2q is None or f2q >= 1.0:
        record.derived_metrics["theoretical_max"] = {
            "value": UNKNOWN,
            "formula": "2 ** min(N, 1/eps_2q)",
            "inputs": {"N": N, "F_2q_avg": f2q},
            "caveat": "Insufficient inputs (need qubit count and 2q average fidelity).",
        }
        return

    eps = 1.0 - f2q
    exponent = min(N, 1.0 / eps)
    value = 2.0 ** exponent

    record.derived_metrics["theoretical_max"] = {
        "value": value,
        "log2_value": exponent,
        "formula": "2 ** min(N, 1/eps_2q); eps_2q = 1 - F_2q",
        "inputs": {"N": N, "F_2q": f2q, "F_2q_stat": f2q_field.method or "unknown",
                   "eps_2q": eps, "exponent": exponent},
        "caveat": ("Rough headroom proxy only: conflates circuit width (N) with "
                   "depth (1/eps). Not a substitute for measured Quantum Volume."),
    }
    # Mirror into the top-level provValue field.
    record.theoretical_max = F(
        value=value,
        source=f2q_field.source or n_field.source,
        retrieved=f2q_field.retrieved or n_field.retrieved,
        method="theoretical", kind="computed",
    )


def finalize(record: BackendRecord) -> BackendRecord:
    """Run all derived-metric computations. Idempotent."""
    compute_theoretical_max(record)
    return record
