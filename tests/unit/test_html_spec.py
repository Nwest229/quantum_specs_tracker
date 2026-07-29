"""Tests for qscrape.adapters.html_spec: regex/const field extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from qscrape.adapters.html_spec import HtmlSpecAdapter, _coerce, _json_path
from qscrape.models import F

if TYPE_CHECKING:
    from tests.conftest import FakeHttp


class TestHtmlSpecAdapter:
    def test_regex_and_const_extraction(self, fake_http_factory: type[FakeHttp]) -> None:
        cfg: dict[str, Any] = {
            "vendor": "quantinuum",
            "tier": "vendor",
            "backend_name": "Quantinuum H2-1",
            "url": "http://example/h2",
            "fields": {
                "qpu_topology.qubits": {"regex": r"(\d+)\s+qubits", "as": "int"},
                "qpu_topology.type": {"const": "linear-trap"},
                "fidelity.2q_avg": {
                    "regex": r"two-qubit[^0-9]*(\d\d\.\d+)\s*%",
                    "as": "fraction",
                    "method": "average",
                },
            },
        }
        page = "The H2-1 system has 56 qubits. Typical two-qubit fidelity is 99.87%."
        adapter = HtmlSpecAdapter(fake_http_factory(page), cfg)
        rec = next(iter(adapter.fetch()))
        assert rec.qpu_topology["qubits"].value == 56
        assert rec.qpu_topology["type"].value == "linear-trap"
        assert rec.fidelity["2q_avg"].value == pytest.approx(0.9987)
        assert rec.fidelity["2q_avg"].source == "http://example/h2"

    def test_no_invention_on_fetch_failure(self, fake_http_factory: type[FakeHttp]) -> None:
        cfg: dict[str, Any] = {
            "vendor": "v",
            "backend_name": "B",
            "url": "http://x",
            "fields": {"qpu_topology.qubits": {"regex": r"(\d+) qubits", "as": "int"}},
        }
        adapter = HtmlSpecAdapter(fake_http_factory("", status=404), cfg)
        rec = next(iter(adapter.fetch()))
        assert not rec.qpu_topology.get("qubits", F()).known
        assert rec.meta.get("warnings")

    def test_json_path(self) -> None:
        assert _json_path({"a": [{"b": 5}]}, "a.0.b") == 5
        assert _json_path({"a": 1}, "a.0.b") is None

    def test_coerce(self) -> None:
        assert _coerce("133 qubits", "int") == 133
        assert _coerce("99.9%", "fraction") == pytest.approx(0.999)
