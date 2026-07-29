"""Tests for qscrape.adapters.ibm against a mock of the qiskit-runtime API
(no token / SDK needed)."""

from __future__ import annotations

import os
import statistics
from typing import Any, ClassVar

import pytest

from qscrape.adapters.ibm import IBMAdapter


class _Gate:
    def __init__(self, gate: str, qubits: list[int]) -> None:
        self.gate, self.qubits = gate, qubits


class _Props:
    def __init__(self) -> None:
        self.gates = [
            _Gate("cz", [0, 1]),
            _Gate("cz", [1, 2]),
            _Gate("sx", [0]),
            _Gate("sx", [1]),
            _Gate("rz", [0]),  # virtual: must be ignored
        ]
        self._err = {
            ("cz", (0, 1)): 0.005,
            ("cz", (1, 2)): 0.007,
            ("sx", (0,)): 0.0002,
            ("sx", (1,)): 0.0004,
            ("rz", (0,)): 0.0,
        }
        self._len = {
            ("cz", (0, 1)): 3.0e-7,
            ("cz", (1, 2)): 3.2e-7,
            ("sx", (0,)): 3.5e-8,
            ("sx", (1,)): 3.6e-8,
        }

    def gate_error(self, g: str, q: list[int]) -> float | None:
        return self._err.get((g, tuple(q)))

    def gate_length(self, g: str, q: list[int]) -> float:
        v = self._len.get((g, tuple(q)))
        if v is None:
            raise ValueError("no length")  # adapter must swallow this
        return v

    def readout_error(self, q: int) -> float:
        return 0.01 + 0.001 * q

    def readout_length(self, q: int) -> float:
        return 1.2e-6


class _Conf:
    processor_type: ClassVar[dict[str, str]] = {"family": "Heron", "revision": "r2"}
    quantum_volume = 512


class _Backend:
    name = "ibm_test"
    num_qubits = 3
    # undirected: {0-1, 1-2}
    coupling_map: ClassVar[list[list[int]]] = [[0, 1], [1, 0], [1, 2], [2, 1]]

    def configuration(self) -> _Conf:
        return _Conf()

    def properties(self) -> _Props:
        return _Props()


def _rec() -> Any:
    return IBMAdapter(None, {})._to_record(_Backend())  # type: ignore[arg-type]


class TestIBMAdapter:
    def test_topology_and_headline(self) -> None:
        rec = _rec()
        assert rec.backend_name == "IBM Test"  # cloud id normalised to merge with CSV
        assert rec.system_name == "ibm_test"  # raw id kept for reference
        assert rec.qpu_topology["qubits"].value == 3
        assert rec.qpu_topology["edges"].value == 2
        assert rec.model == "Heron r2"
        assert rec.quantum_volume.value == 512
        assert rec.quantum_volume.kind == "calibration-api"

    def test_fidelity_stats(self) -> None:
        rec = _rec()
        assert rec.fidelity["2q_avg"].value == pytest.approx((0.995 + 0.993) / 2, abs=1e-6)
        assert rec.fidelity["2q_max"].value == pytest.approx(0.995, abs=1e-6)
        # sx only, rz ignored
        assert rec.fidelity["1q_max"].value == pytest.approx(0.9998, abs=1e-6)
        assert rec.fidelity["1q_min"].value == pytest.approx(0.9996, abs=1e-6)

    def test_gate_and_readout_times(self) -> None:
        rec = _rec()
        assert rec.operation_speed["2q_gate_time_s"].value == pytest.approx(
            statistics.median([3.0e-7, 3.2e-7])
        )
        assert rec.operation_speed["1q_gate_time_s"].value == pytest.approx(
            statistics.median([3.5e-8, 3.6e-8])
        )
        assert rec.operation_speed["readout_time_s"].value == pytest.approx(1.2e-6)

    def test_no_token_is_clean_skip(self) -> None:
        tok = os.environ.pop("IBM_QUANTUM_TOKEN", None)
        try:
            assert IBMAdapter(None, {}).fetch() == []  # silent  # type: ignore[arg-type]
            recs = IBMAdapter(None, {"emit_skips": True}).fetch()  # warns  # type: ignore[arg-type]
            assert recs[0].meta.get("skipped")
        finally:
            if tok is not None:
                os.environ["IBM_QUANTUM_TOKEN"] = tok
