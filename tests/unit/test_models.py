"""Tests for qscrape.models: BackendRecord (de)serialization."""

from __future__ import annotations

from qscrape.models import UNKNOWN, BackendRecord, F


class TestSerialization:
    def test_to_dict_shape_and_sources(self) -> None:
        rec = BackendRecord("Q System One", "ibm", system_name="ibm_x")
        rec.set(
            "fidelity.2q_avg",
            F(0.995, "http://ibm/cal", "2026-07-06", "average", "calibration-api"),
        )
        d = rec.to_dict()
        assert d["backend_name"] == "Q System One"
        assert d["fidelity"]["2q_avg"] == 0.995
        assert d["quantum_volume"]["value"] == UNKNOWN
        srcs = {(s["field"], s["url"]) for s in d["sources"]}
        assert ("fidelity.2q_avg", "http://ibm/cal") in srcs

    def test_unknown_default(self) -> None:
        d = BackendRecord("A", "v").to_dict()
        assert d["model"] == UNKNOWN
        assert d["fidelity"]["2q_avg"] == UNKNOWN
        assert d["sources"] == []
