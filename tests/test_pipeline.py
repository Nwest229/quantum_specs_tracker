"""Offline unit tests for the deterministic parts of the pipeline."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qscrape.models import BackendRecord, F, UNKNOWN
from qscrape.normalize import (
    parse_percent_as_fraction, parse_number, fidelity_stats,
    apply_fidelity_stats, finalize,
)
from qscrape.adapters.html_spec import HtmlSpecAdapter, _coerce, _json_path


class FakeHttp:
    """Serves a canned page for any URL so tests never touch the network."""
    def __init__(self, text, status=200, content_type="text/html", retrieved="2026-07-06T00:00:00+00:00"):
        self._text, self._status, self._ct, self._ret = text, status, content_type, retrieved

    def get(self, url, headers=None, force=False):
        from qscrape.httpcache import Response
        return Response(url=url, status=self._status, text=self._text,
                        retrieved=self._ret, from_cache=False, content_type=self._ct)


class TestParsing(unittest.TestCase):
    def test_percent_to_fraction(self):
        self.assertAlmostEqual(parse_percent_as_fraction("99.7%"), 0.997)
        self.assertAlmostEqual(parse_percent_as_fraction("99.7"), 0.997)
        self.assertAlmostEqual(parse_percent_as_fraction("0.997"), 0.997)
        self.assertIsNone(parse_percent_as_fraction("n/a"))

    def test_parse_number(self):
        self.assertEqual(parse_number("133 qubits"), 133.0)
        self.assertEqual(parse_number("2^20"), 2.0)  # first number only
        self.assertIsNone(parse_number(None))

    def test_coerce(self):
        self.assertEqual(_coerce("133 qubits", "int"), 133)
        self.assertAlmostEqual(_coerce("99.9%", "fraction"), 0.999)


class TestFidelityStats(unittest.TestCase):
    def test_stats(self):
        s = fidelity_stats([0.99, 0.98, 0.995])
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["max"], 0.995)
        self.assertEqual(s["min"], 0.98)
        self.assertAlmostEqual(s["median"], 0.99)

    def test_apply(self):
        rec = BackendRecord("X", "vend")
        apply_fidelity_stats(rec, [0.99, 0.98, 0.995], one_q=[0.999, 0.9995],
                             source="http://x", retrieved="2026-07-06")
        self.assertEqual(rec.fidelity["2q_max"].value, 0.995)
        self.assertEqual(rec.fidelity["2q_min"].value, 0.98)
        self.assertTrue(rec.fidelity["2q_avg"].known)
        self.assertEqual(rec.fidelity["1q_max"].value, 0.9995)


class TestTheoreticalMax(unittest.TestCase):
    def test_computes_with_inputs(self):
        rec = BackendRecord("X", "vend")
        rec.set("qpu_topology.qubits", F(100, "u", "t"))
        rec.set("fidelity.2q_avg", F(0.99, "u", "t"))
        finalize(rec)
        tm = rec.derived_metrics["theoretical_max"]
        # eps=0.01 -> 1/eps=100 == N -> exponent 100
        self.assertAlmostEqual(tm["inputs"]["eps_2q"], 0.01)
        self.assertAlmostEqual(tm["log2_value"], 100.0)
        self.assertIn("caveat", tm)
        self.assertTrue(rec.theoretical_max.known)

    def test_missing_inputs(self):
        rec = BackendRecord("X", "vend")
        finalize(rec)
        self.assertEqual(rec.derived_metrics["theoretical_max"]["value"], UNKNOWN)


class TestSerialization(unittest.TestCase):
    def test_to_dict_shape_and_sources(self):
        rec = BackendRecord("Q System One", "ibm", system_name="ibm_x")
        rec.set("fidelity.2q_avg", F(0.995, "http://ibm/cal", "2026-07-06",
                                     "average", "calibration-api"))
        d = rec.to_dict()
        self.assertEqual(d["backend_name"], "Q System One")
        self.assertEqual(d["fidelity"]["2q_avg"], 0.995)
        self.assertEqual(d["quantum_volume"]["value"], UNKNOWN)
        srcs = {(s["field"], s["url"]) for s in d["sources"]}
        self.assertIn(("fidelity.2q_avg", "http://ibm/cal"), srcs)

    def test_unknown_default(self):
        d = BackendRecord("A", "v").to_dict()
        self.assertEqual(d["model"], UNKNOWN)
        self.assertEqual(d["fidelity"]["2q_avg"], UNKNOWN)
        self.assertEqual(d["sources"], [])


class TestHtmlSpecAdapter(unittest.TestCase):
    def test_regex_and_const_extraction(self):
        cfg = {
            "vendor": "quantinuum", "tier": "vendor",
            "backend_name": "Quantinuum H2-1",
            "url": "http://example/h2",
            "fields": {
                "qpu_topology.qubits": {"regex": r"(\d+)\s+qubits", "as": "int"},
                "qpu_topology.type": {"const": "linear-trap"},
                "fidelity.2q_avg": {"regex": r"two-qubit[^0-9]*(\d\d\.\d+)\s*%",
                                    "as": "fraction", "method": "average"},
            },
        }
        page = "The H2-1 system has 56 qubits. Typical two-qubit fidelity is 99.87%."
        adapter = HtmlSpecAdapter(FakeHttp(page), cfg)
        rec = list(adapter.fetch())[0]
        self.assertEqual(rec.qpu_topology["qubits"].value, 56)
        self.assertEqual(rec.qpu_topology["type"].value, "linear-trap")
        self.assertAlmostEqual(rec.fidelity["2q_avg"].value, 0.9987)
        self.assertEqual(rec.fidelity["2q_avg"].source, "http://example/h2")

    def test_no_invention_on_fetch_failure(self):
        cfg = {"vendor": "v", "backend_name": "B", "url": "http://x",
               "fields": {"qpu_topology.qubits": {"regex": r"(\d+) qubits", "as": "int"}}}
        adapter = HtmlSpecAdapter(FakeHttp("", status=404), cfg)
        rec = list(adapter.fetch())[0]
        self.assertFalse(rec.qpu_topology.get("qubits", F()).known)
        self.assertTrue(rec.meta.get("warnings"))

    def test_json_path(self):
        self.assertEqual(_json_path({"a": [{"b": 5}]}, "a.0.b"), 5)
        self.assertIsNone(_json_path({"a": 1}, "a.0.b"))


class TestCsvSource(unittest.TestCase):
    def test_german_number_and_money_parsing(self):
        from qscrape.csv_source import de_num, de_percent, de_money, de_date, _decimal_str
        self.assertAlmostEqual(de_num("2,5E-08"), 2.5e-08)
        self.assertAlmostEqual(de_num("364,35"), 364.35)
        self.assertIsNone(de_num("#DIV/0!"))
        self.assertAlmostEqual(de_percent("99,95%"), 0.9995)
        self.assertAlmostEqual(de_money("135.000,00 €"), 135000.0)
        self.assertAlmostEqual(de_money("1,60 €"), 1.60)
        self.assertEqual(de_date("31.07.2024"), "2024-07-31")
        self.assertEqual(_decimal_str(0.00003), "0.00003")   # no scientific notation
        self.assertEqual(_decimal_str(135000.0), "135000")

    def test_row_maps_to_record_with_provenance(self):
        from qscrape.csv_source import records_from_csv, rows_from_csv, _record, COL
        # a minimal header + one IonQ-Harmony-like row
        header = ";".join(["ID"] + [f"c{i}" for i in range(1, len(COL))])
        cells = [""] * len(COL)
        cells[COL["id"]] = "44"; cells[COL["type"]] = "IT-l"; cells[COL["vendor"]] = "IonQ"
        cells[COL["model"]] = "Harmony"; cells[COL["b"]] = "9"; cells[COL["qubits"]] = "11"
        cells[COL["2q_avg"]] = "96,54%"; cells[COL["per_1q_gate"]] = "0,003"
        cells[COL["per_shot"]] = "1,00"; cells[COL["price_date"]] = "30.04.2024"
        cells[COL["price_source"]] = "https://aws.amazon.com/braket/pricing/"
        cells[COL["tech_source"]] = "https://arxiv.org/pdf/2203.03816"
        cells[COL["tech_date"]] = "30.03.2022"
        rec = _record(cells, "IonQ")
        self.assertEqual(rec.backend_name, "IonQ Harmony")
        self.assertEqual(rec.black_box.value, 9)                 # 'b' col -> algorithmic qubits
        self.assertEqual(rec.qpu_topology["qubits"].value, 11)
        self.assertAlmostEqual(rec.fidelity["2q_avg"].value, 0.9654)
        self.assertEqual(rec.pricing["per_1q_gate"].value, "USD 0.00003")   # cents -> USD, plain decimal
        self.assertEqual(rec.pricing["per_shot"].value, "USD 0.01")
        self.assertEqual(rec.pricing["per_shot"].retrieved, "2024-04-30")   # price date, not tech date
        self.assertEqual(rec.fidelity["2q_avg"].retrieved, "2022-03-30")    # tech date

    def test_cloud_device_id_normalised(self):
        from qscrape.csv_source import _backend_name, COL
        row = [""] * len(COL)
        row[COL["vendor"]] = "IonQ"; row[COL["model"]] = "Forte"
        row[COL["system_name"]] = "ionq_forte"
        self.assertEqual(_backend_name(row), "IonQ Forte")  # merges with the scraper's name


class TestIBMAdapter(unittest.TestCase):
    """Verify the IBM extraction logic against a mock of the qiskit-runtime API
    (no token / SDK needed)."""

    class _Gate:
        def __init__(self, gate, qubits): self.gate, self.qubits = gate, qubits

    class _Props:
        def __init__(self):
            self.gates = [
                TestIBMAdapter._Gate("cz", [0, 1]), TestIBMAdapter._Gate("cz", [1, 2]),
                TestIBMAdapter._Gate("sx", [0]), TestIBMAdapter._Gate("sx", [1]),
                TestIBMAdapter._Gate("rz", [0]),  # virtual: must be ignored
            ]
            self._err = {("cz", (0, 1)): 0.005, ("cz", (1, 2)): 0.007,
                         ("sx", (0,)): 0.0002, ("sx", (1,)): 0.0004, ("rz", (0,)): 0.0}
            self._len = {("cz", (0, 1)): 3.0e-7, ("cz", (1, 2)): 3.2e-7,
                         ("sx", (0,)): 3.5e-8, ("sx", (1,)): 3.6e-8}

        def gate_error(self, g, q): return self._err.get((g, tuple(q)))
        def gate_length(self, g, q):
            v = self._len.get((g, tuple(q)))
            if v is None:
                raise ValueError("no length")   # adapter must swallow this
            return v
        def readout_error(self, q): return 0.01 + 0.001 * q
        def readout_length(self, q): return 1.2e-6

    class _Conf:
        processor_type = {"family": "Heron", "revision": "r2"}
        quantum_volume = 512

    class _Backend:
        name = "ibm_test"
        num_qubits = 3
        coupling_map = [[0, 1], [1, 0], [1, 2], [2, 1]]  # undirected: {0-1, 1-2}
        def configuration(self): return TestIBMAdapter._Conf()
        def properties(self): return TestIBMAdapter._Props()

    def _rec(self):
        from qscrape.adapters.ibm import IBMAdapter
        return IBMAdapter(None, {})._to_record(self._Backend())

    def test_topology_and_headline(self):
        rec = self._rec()
        self.assertEqual(rec.backend_name, "IBM Test")   # cloud id normalised to merge with CSV
        self.assertEqual(rec.system_name, "ibm_test")    # raw id kept for reference
        self.assertEqual(rec.qpu_topology["qubits"].value, 3)
        self.assertEqual(rec.qpu_topology["edges"].value, 2)
        self.assertEqual(rec.model, "Heron r2")
        self.assertEqual(rec.quantum_volume.value, 512)
        self.assertEqual(rec.quantum_volume.kind, "calibration-api")

    def test_fidelity_stats(self):
        rec = self._rec()
        self.assertAlmostEqual(rec.fidelity["2q_avg"].value, (0.995 + 0.993) / 2, places=6)
        self.assertAlmostEqual(rec.fidelity["2q_max"].value, 0.995, places=6)
        self.assertAlmostEqual(rec.fidelity["1q_max"].value, 0.9998, places=6)  # sx only, rz ignored
        self.assertAlmostEqual(rec.fidelity["1q_min"].value, 0.9996, places=6)

    def test_gate_and_readout_times(self):
        import statistics
        rec = self._rec()
        self.assertAlmostEqual(rec.operation_speed["2q_gate_time_s"].value,
                               statistics.median([3.0e-7, 3.2e-7]))
        self.assertAlmostEqual(rec.operation_speed["1q_gate_time_s"].value,
                               statistics.median([3.5e-8, 3.6e-8]))
        self.assertAlmostEqual(rec.operation_speed["readout_time_s"].value, 1.2e-6)

    def test_no_token_is_clean_skip(self):
        from qscrape.adapters.ibm import IBMAdapter
        import os as _os
        tok = _os.environ.pop("IBM_QUANTUM_TOKEN", None)
        try:
            self.assertEqual(IBMAdapter(None, {}).fetch(), [])          # silent
            recs = IBMAdapter(None, {"emit_skips": True}).fetch()        # warns
            self.assertTrue(recs[0].meta.get("skipped"))
        finally:
            if tok is not None:
                _os.environ["IBM_QUANTUM_TOKEN"] = tok


class TestSchemaValidates(unittest.TestCase):
    def test_output_matches_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "schema", "backend.schema.json")) as fh:
            schema = json.load(fh)
        rec = BackendRecord("H2-1", "quantinuum")
        rec.set("fidelity.2q_avg", F(0.9987, "http://x", "2026-07-06", "average", "vendor"))
        rec.set("qpu_topology.qubits", F(56, "http://x", "2026-07-06", "vendor-spec", "vendor"))
        finalize(rec)
        jsonschema.Draft7Validator(schema).validate(rec.to_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
