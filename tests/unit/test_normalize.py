"""Tests for qscrape.normalize: parsing, fidelity stats, and theoretical_max."""

from __future__ import annotations

import pytest

from qscrape.models import UNKNOWN, BackendRecord, F
from qscrape.normalize import (
    apply_fidelity_stats,
    fidelity_stats,
    finalize,
    parse_number,
    parse_percent_as_fraction,
)


class TestParsing:
    def test_percent_to_fraction(self) -> None:
        assert parse_percent_as_fraction("99.7%") == pytest.approx(0.997)
        assert parse_percent_as_fraction("99.7") == pytest.approx(0.997)
        assert parse_percent_as_fraction("0.997") == pytest.approx(0.997)
        assert parse_percent_as_fraction("n/a") is None

    def test_parse_number(self) -> None:
        assert parse_number("133 qubits") == 133.0
        assert parse_number("2^20") == 2.0  # first number only
        assert parse_number(None) is None


class TestFidelityStats:
    def test_stats(self) -> None:
        s = fidelity_stats([0.99, 0.98, 0.995])
        assert s["n"] == 3
        assert s["max"] == 0.995
        assert s["min"] == 0.98
        assert s["median"] == pytest.approx(0.99)

    def test_apply(self) -> None:
        rec = BackendRecord("X", "vend")
        apply_fidelity_stats(
            rec,
            [0.99, 0.98, 0.995],
            one_q=[0.999, 0.9995],
            source="http://x",
            retrieved="2026-07-06",
        )
        assert rec.fidelity["2q_max"].value == 0.995
        assert rec.fidelity["2q_min"].value == 0.98
        assert rec.fidelity["2q_avg"].known
        assert rec.fidelity["1q_max"].value == 0.9995


class TestTheoreticalMax:
    def test_computes_with_inputs(self) -> None:
        rec = BackendRecord("X", "vend")
        rec.set("qpu_topology.qubits", F(100, "u", "t"))
        rec.set("fidelity.2q_avg", F(0.99, "u", "t"))
        finalize(rec)
        tm = rec.derived_metrics["theoretical_max"]
        # eps=0.01 -> 1/eps=100 == N -> exponent 100
        assert tm["inputs"]["eps_2q"] == pytest.approx(0.01)
        assert tm["log2_value"] == pytest.approx(100.0)
        assert "caveat" in tm
        assert rec.theoretical_max.known

    def test_missing_inputs(self) -> None:
        rec = BackendRecord("X", "vend")
        finalize(rec)
        assert rec.derived_metrics["theoretical_max"]["value"] == UNKNOWN
