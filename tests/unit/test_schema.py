"""Tests that pipeline output validates against schema/backend.schema.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qscrape.models import BackendRecord, F
from qscrape.normalize import finalize

ROOT = Path(__file__).parent.parent.parent


class TestSchemaValidates:
    def test_output_matches_schema(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        with (ROOT / "schema" / "backend.schema.json").open(encoding="utf-8") as fh:
            schema = json.load(fh)
        rec = BackendRecord("H2-1", "quantinuum")
        rec.set("fidelity.2q_avg", F(0.9987, "http://x", "2026-07-06", "average", "vendor"))
        rec.set("qpu_topology.qubits", F(56, "http://x", "2026-07-06", "vendor-spec", "vendor"))
        finalize(rec)
        jsonschema.Draft7Validator(schema).validate(rec.to_dict())
