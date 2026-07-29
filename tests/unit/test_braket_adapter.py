"""Tests for qscrape.adapters.braket: device -> record name normalisation and
real per-provider calibration parsing."""

from __future__ import annotations

from typing import Any

import pytest

from qscrape.adapters.braket import BraketAdapter

# IQM provider block shaped exactly like the live AWS Braket dump
IQM_PROVIDER: dict[str, Any] = {
    "braketSchemaHeader": {"name": "braket.device_schema.iqm.iqm_provider_properties"},
    "properties": {
        "one_qubit": {
            "1": {"f1Q_simultaneous_RB": 0.9996, "fRO": 0.9916},
            "2": {"f1Q_simultaneous_RB": 0.9998, "fRO": 0.9906},
        },
        "two_qubit": {
            "1-2": {"f2Q_simultaneous_RB": 0.990},
            "2-3": {"f2Q_simultaneous_RB": 0.992},
        },
    },
}
# Rigetti provider block shaped like the live dump (benchmarks carry fidelity)
RIGETTI_PROVIDER: dict[str, Any] = {
    "specs": {
        "architecture": {
            "edges": [{"node_ids": [0, 1]}, {"node_ids": [1, 2]}, {"node_ids": [2, 3]}]
        },
        "benchmarks": [
            {
                "name": "randomized_benchmark_1q",
                "node_count": 1,
                "sites": [
                    {"characteristics": [{"name": "fRB", "value": 0.996}], "node_ids": [63]},
                    {"characteristics": [{"name": "fRB", "value": 0.999}], "node_ids": [91]},
                ],
            },
            {
                "name": "randomized_benchmark_2q",
                "node_count": 2,
                "sites": [
                    {"characteristics": [{"name": "fCZ", "value": 0.98}], "node_ids": [0, 1]},
                    {"characteristics": [{"name": "fCZ", "value": 0.97}], "node_ids": [1, 2]},
                ],
            },
        ],
    }
}
# IonQ provider block shaped exactly like the live AWS Braket dump
IONQ_PROVIDER: dict[str, Any] = {
    "braketSchemaHeader": {"name": "braket.device_schema.ionq.ionq_provider_properties"},
    "fidelity": {"1Q": {"mean": 0.9998}, "2Q": {"mean": 0.9941}, "spam": {"mean": 0.9962}},
    "timing": {"T1": 100.0, "T2": 1.0, "1Q": 0.00013, "2Q": 0.00097, "readout": 0.00015},
}


class _Props:
    def __init__(self, provider: dict[str, Any]) -> None:
        self._p = provider

    def dict(self) -> dict[str, Any]:
        return {
            "paradigm": {"qubitCount": 20, "connectivity": {"fullyConnected": True}},
            "provider": self._p,
        }


class _Dev:
    def __init__(self, name: str, provider_name: str, provider: dict[str, Any]) -> None:
        self.name = name
        self.provider_name = provider_name
        self.arn = f"arn:aws:braket:::device/qpu/{provider_name.lower()}/{name}"
        self.properties = _Props(provider)


def _rec(name: str, provider_name: str, provider: dict[str, Any]) -> Any:
    return BraketAdapter(None, {})._to_record(  # type: ignore[arg-type]
        _Dev(name, provider_name, provider)
    )


class TestBraketAdapter:
    def test_name_normalised_to_merge(self) -> None:
        rec = _rec("Garnet", "IQM", IQM_PROVIDER)
        assert rec.backend_name == "IQM Garnet"  # merges with existing row
        assert rec.vendor == "iqm"
        assert rec.system_name == "Garnet"  # raw Braket id kept
        assert rec.qpu_topology["qubits"].value == 20
        assert rec.qpu_topology["type"].value == "all-to-all"

    def test_iqm_live_fidelity_parsed(self) -> None:
        rec = _rec("Garnet", "IQM", IQM_PROVIDER)
        assert rec.fidelity["1q_avg"].value == pytest.approx((0.9996 + 0.9998) / 2, abs=1e-6)
        assert rec.fidelity["spam_avg"].value == pytest.approx((0.9916 + 0.9906) / 2, abs=1e-6)
        assert rec.fidelity["2q_avg"].value == pytest.approx((0.990 + 0.992) / 2, abs=1e-6)
        assert rec.fidelity["2q_min"].value == pytest.approx(0.990, abs=1e-6)

    def test_ionq_live_fidelity_and_timing(self) -> None:
        rec = _rec("Forte 1", "IonQ", IONQ_PROVIDER)
        assert rec.backend_name == "IonQ Forte 1"
        assert rec.fidelity["1q_avg"].value == pytest.approx(0.9998)
        assert rec.fidelity["2q_avg"].value == pytest.approx(0.9941)
        assert rec.fidelity["spam_avg"].value == pytest.approx(0.9962)
        assert rec.operation_speed["1q_gate_time_s"].value == pytest.approx(0.00013)
        assert rec.operation_speed["2q_gate_time_s"].value == pytest.approx(0.00097)
        assert rec.operation_speed["readout_time_s"].value == pytest.approx(0.00015)

    def test_rigetti_edges_and_benchmarks(self) -> None:
        rec = _rec("Cepheus-1-108Q", "Rigetti", RIGETTI_PROVIDER)
        assert rec.backend_name == "Rigetti Cepheus-1-108Q"  # new device, not merged
        assert rec.qpu_topology["edges"].value == 3
        assert rec.fidelity["1q_avg"].value == pytest.approx((0.996 + 0.999) / 2, abs=1e-6)
        assert rec.fidelity["2q_avg"].value == pytest.approx((0.98 + 0.97) / 2, abs=1e-6)
        assert rec.fidelity["2q_min"].value == pytest.approx(0.97, abs=1e-6)

    def test_empty_provider_is_safe(self) -> None:
        rec = _rec("Aquila", "QuEra", {})  # analog: no calibration
        assert not rec.fidelity.get("2q_avg")
