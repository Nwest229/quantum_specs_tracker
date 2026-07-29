"""Orchestration: run adapters -> normalise -> validate -> merge -> write array.

Produces a *single combined JSON array* of backend records (per the chosen
output format), plus a run report of warnings and validation errors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import adapters as adp
from .httpcache import HttpCache
from .models import _SCALAR_META, BackendRecord, now_iso
from .normalize import finalize

ROOT = Path(__file__).parent.parent.parent
SCHEMA_PATH = ROOT / "schema" / "backend.schema.json"

try:
    import jsonschema

    _HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAVE_JSONSCHEMA = False


def _record_key(name: str, vendor: str) -> str:
    return f"{vendor}::{name}".lower()


def _is_skip(rec: BackendRecord) -> bool:
    return bool(rec.meta.get("skipped")) or rec.backend_name.startswith("__")


class Pipeline:
    def __init__(self, config: dict[str, Any], http: HttpCache | None = None) -> None:
        self.config = config
        self.http = http or HttpCache(
            max_age=config.get("cache_max_age", 86400),
            delay=config.get("request_delay", 1.0),
        )
        self.records: dict[str, BackendRecord] = {}
        self.report: dict[str, Any] = {
            "generated": now_iso(),
            "warnings": [],
            "validation_errors": [],
            "counts": {},
        }

    # -- collection -------------------------------------------------------
    def run(self) -> list[dict[str, Any]]:
        self._run_api_adapters()
        self._run_spec_adapters()
        self._run_csv_sources()  # baseline: added last so live values win on merge
        for rec in self.records.values():
            finalize(rec)
        docs = [rec.to_dict() for rec in self.records.values()]
        self._validate(docs)
        self.report["counts"] = {
            "backends": len(docs),
            "vendors": len({d["vendor"] for d in docs}),
        }
        return docs

    def _add(self, rec: BackendRecord) -> None:
        if _is_skip(rec):
            for w in rec.meta.get("warnings", []):
                self.report["warnings"].append(f"[{rec.vendor}] {w}")
            return
        key = _record_key(rec.backend_name, rec.vendor)
        if key in self.records:
            self._merge_into(self.records[key], rec)
        else:
            self.records[key] = rec
        for w in rec.meta.get("warnings", []):
            self.report["warnings"].append(f"[{rec.backend_name}] {w}")

    def _run_api_adapters(self) -> None:
        for name, cfg in self.config.get("api_sources", {}).items():
            if not cfg.get("enabled", True):
                continue
            cls = adp.API_ADAPTERS.get(name)
            if cls is None:
                self.report["warnings"].append(f"unknown api adapter '{name}'")
                continue
            for rec in cls(self.http, cfg).fetch():
                self._add(rec)

    def _run_spec_adapters(self) -> None:
        for entry in self.config.get("spec_sources", []):
            for rec in adp.SPEC_ADAPTER(self.http, entry).fetch():
                self._add(rec)

    def _run_csv_sources(self) -> None:
        from .csv_source import records_from_csv

        for entry in self.config.get("csv_sources", []):
            if not entry.get("enabled", True):
                continue
            path_str = entry.get("path", "")
            path = Path(path_str)
            if not path.is_absolute():
                path = ROOT / path_str
            n = 0
            for rec in records_from_csv(path):
                self._add(rec)
                n += 1
            if n == 0:
                self.report["warnings"].append(f"csv source '{path}' yielded no rows")
            else:
                self.report.setdefault("counts", {})["csv_rows"] = (
                    self.report.get("counts", {}).get("csv_rows", 0) + n
                )

    @staticmethod
    def _merge_into(base: BackendRecord, new: BackendRecord) -> None:
        """Combine two records for the same backend, preferring existing known
        values (adapters are ordered by source priority in config)."""
        for grp in ("qpu_topology", "fidelity", "operation_speed", "features", "pricing"):
            bmap = getattr(base, grp)
            for leaf, fld in getattr(new, grp).items():
                if fld.known and not bmap.get(leaf, type(fld)()).known:
                    bmap[leaf] = fld
        for singleton in ("quantum_volume", "black_box", "argmax", "vendor_metric"):
            if getattr(new, singleton).known and not getattr(base, singleton).known:
                setattr(base, singleton, getattr(new, singleton))
        for k in _SCALAR_META:
            if not getattr(base, k) and getattr(new, k):
                setattr(base, k, getattr(new, k))

    # -- validation -------------------------------------------------------
    def _validate(self, docs: list[dict[str, Any]]) -> None:
        if not _HAVE_JSONSCHEMA:
            self.report["warnings"].append("jsonschema not installed; skipped validation")
            return
        with SCHEMA_PATH.open(encoding="utf-8") as fh:
            schema = json.load(fh)
        validator = jsonschema.Draft7Validator(schema)
        for doc in docs:
            for err in validator.iter_errors(doc):
                self.report["validation_errors"].append(
                    {
                        "backend": doc.get("backend_name"),
                        "path": "/".join(str(p) for p in err.absolute_path),
                        "message": err.message,
                    }
                )

    # -- output -----------------------------------------------------------
    def write(self, docs: list[dict[str, Any]], out_path: str) -> None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(docs, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        tmp.replace(out)
        report_path = out.parent / "run_report.json"
        with report_path.open("w", encoding="utf-8") as fh:
            json.dump(self.report, fh, indent=2)
