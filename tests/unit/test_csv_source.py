"""Tests for qscrape.csv_source: German-locale parsing and CSV row mapping."""

from __future__ import annotations

import pytest

from qscrape.csv_source import (
    COL,
    _backend_name,
    _decimal_str,
    _record,
    de_date,
    de_money,
    de_num,
    de_percent,
)


class TestCsvSource:
    def test_german_number_and_money_parsing(self) -> None:
        assert de_num("2,5E-08") == pytest.approx(2.5e-08)
        assert de_num("364,35") == pytest.approx(364.35)
        assert de_num("#DIV/0!") is None
        assert de_percent("99,95%") == pytest.approx(0.9995)
        assert de_money("135.000,00 \u20ac") == pytest.approx(135000.0)
        assert de_money("1,60 \u20ac") == pytest.approx(1.60)
        assert de_date("31.07.2024") == "2024-07-31"
        assert _decimal_str(0.00003) == "0.00003"  # no scientific notation
        assert _decimal_str(135000.0) == "135000"

    def test_row_maps_to_record_with_provenance(self) -> None:
        # a minimal header + one IonQ-Harmony-like row
        cells = [""] * len(COL)
        cells[COL["id"]] = "44"
        cells[COL["type"]] = "IT-l"
        cells[COL["vendor"]] = "IonQ"
        cells[COL["model"]] = "Harmony"
        cells[COL["b"]] = "9"
        cells[COL["qubits"]] = "11"
        cells[COL["2q_avg"]] = "96,54%"
        cells[COL["per_1q_gate"]] = "0,003"
        cells[COL["per_shot"]] = "1,00"
        cells[COL["price_date"]] = "30.04.2024"
        cells[COL["price_source"]] = "https://aws.amazon.com/braket/pricing/"
        cells[COL["tech_source"]] = "https://arxiv.org/pdf/2203.03816"
        cells[COL["tech_date"]] = "30.03.2022"
        rec = _record(cells, "IonQ")
        assert rec.backend_name == "IonQ Harmony"
        assert rec.black_box.value == 9  # 'b' col -> algorithmic qubits
        assert rec.qpu_topology["qubits"].value == 11
        assert rec.fidelity["2q_avg"].value == pytest.approx(0.9654)
        assert rec.pricing["per_1q_gate"].value == "USD 0.00003"  # cents -> USD, plain decimal
        assert rec.pricing["per_shot"].value == "USD 0.01"
        assert rec.pricing["per_shot"].retrieved == "2024-04-30"  # price date, not tech date
        assert rec.fidelity["2q_avg"].retrieved == "2022-03-30"  # tech date

    def test_cloud_device_id_normalised(self) -> None:
        row = [""] * len(COL)
        row[COL["vendor"]] = "IonQ"
        row[COL["model"]] = "Forte"
        row[COL["system_name"]] = "ionq_forte"
        assert _backend_name(row) == "IonQ Forte"  # merges with the scraper's name
